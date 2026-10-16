"""学术来源识别与全文抓取（LitTrace 文献调研场景迁移新增）。

为什么需要这一层：
  Bocha 是网页搜索 API，arxiv.org/abs/... 的返回只有「元数据 + Abstract」（实测 summary
  被硬截断在 ~800 字符），而本项目的证据合同要求 claim 具备
  method / experiment_setup / results 三块正文证据。只靠搜索摘要时，**没有任何来源能满足
  必要条件**，报告只能全篇输出 insufficient_evidence。
  实测：抓 arxiv.org/html/{id} 可得到 11 万+字符正文，上述证据块关键词全部命中。

设计要点：
  - **只读、无写入副作用**，单次抓取带超时与大小上限。
  - **只对可识别的学术来源抓全文**（arXiv / DOI / OpenAlex / 已知论文站点），
    普通网页不抓，避免把抓取成本摊到所有结果上。
  - 抓取失败**静默降级**回原来的摘要，绝不因为抓不到全文而让整条研究链路失败。
  - 进程内 LRU 缓存 + 同批去重，避免同一篇论文被重复抓取。
  - `available_blocks` 由**抓到的正文**推导，属于可复核的来源事实，**不由 LLM 生成**。
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import threading
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

import requests


def _now_iso() -> str:
    """取回时间戳（UTC，秒级）——元数据/正文 provenance 必须能说明「什么时候取的」。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(text: str) -> str:
    """本模块自带哈希（保持「不导入应用代码」的独立层约束）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

# ---------------------------------------------------------------------------
# 可调参数（均可用环境变量覆盖）
# ---------------------------------------------------------------------------
def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


MAX_CHARS = _int_env("FULLTEXT_MAX_CHARS", 60000)      # 单篇正文上限，防止 prompt 爆炸
FETCH_TIMEOUT = _int_env("FULLTEXT_TIMEOUT", 25)       # 单次抓取超时（秒）
MAX_BYTES = _int_env("FULLTEXT_MAX_BYTES", 4_000_000)  # 响应体上限

_UA = "Mozilla/5.0 (compatible; LitTrace/1.0; academic research assistance)"

# 来源**平台**分类：只描述「这是什么平台」，**不**描述「是否同行评审」。
# 元数据站点、arXiv 预印本、DOI 解析器和 OpenReview 站点
# **不得单凭域名确认为 peer_reviewed**——域名只能说明平台，不能说明评审状态。
# 评审状态另由 peer_review_status() 依**对象自身的 venue 证据**判定，默认 unknown。
SOURCE_KINDS = {
    "arxiv.org": "preprint",
    "openreview.net": "preprint_platform",   # forum 上既有录用的、也有未录用的投稿
    "doi.org": "doi_resolver",               # DOI 解析器，不是出版社正文页
    "openalex.org": "metadata",
    "semanticscholar.org": "metadata",
    "x-mol.com": "metadata",
    "researchgate.net": "repository",
    "nature.com": "publisher",
    "science.org": "publisher",
    "sciencedirect.com": "publisher",
    "link.springer.com": "publisher",
    "springer.com": "publisher",
    "aclanthology.org": "publisher",
    "ieeexplore.ieee.org": "publisher",
    "dl.acm.org": "publisher",
    "openaccess.thecvf.com": "publisher",
    "jmlr.org": "publisher",
    "papers.nips.cc": "publisher",
    "proceedings.mlr.press": "publisher",
    "paper.edu.cn": "publisher",
}

# 值得抓全文的平台（与评审状态无关：预印本也有正文）
_FETCHABLE_KINDS = frozenset({"preprint", "preprint_platform", "publisher", "repository", "doi_resolver"})

# venue 证据：只有**对象自身**给出的会议/期刊/录用信息才能把 peer_reviewed 判为 True。
_VENUE_META_PATTERNS = (
    r'<meta[^>]+name=["\']citation_journal_title["\'][^>]+content=["\']([^"\']+)',
    r'<meta[^>]+name=["\']citation_conference_title["\'][^>]+content=["\']([^"\']+)',
    r'<meta[^>]+name=["\']citation_inbook_title["\'][^>]+content=["\']([^"\']+)',
    r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)',
)
_ACCEPTED_HINTS = re.compile(r"\b(accepted|to appear|published in|camera[- ]ready|proceedings of)\b", re.I)
_REJECTED_HINTS = re.compile(r"\b(submitted to|under review|rejected|withdrawn|desk reject)\b", re.I)

_ARXIV_ID = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")
# 同时捕获版本号：work_id 去 vN，但 version_id / 抓取 URL **必须保留 vN**（PR2 / R04）
_ARXIV_URL = re.compile(r"arxiv\.org/(?:abs|pdf|html)/(\d{4}\.\d{4,5})(v\d+)?")
_OPENREVIEW_FORUM = re.compile(r"openreview\.net/(?:forum|pdf)\?(?:[^#]*&)?id=([A-Za-z0-9_\-\.]+)", re.I)
# DOI：形如 10.1234/abc.def，常出现在 URL 路径里（如 nature.com/articles/10.1038/s41586-...）
_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>()\[\]]+)")
_DOI_TAIL = re.compile(r"[.,;)\]]+$")

# 文献调研的默认检索域白名单。
# 为什么必须限定：实测同一 query 不限定域名时 10 条里 0 条学术来源
# （返回雪球/腾讯网/CSDN/今日头条），限定后 **10/10 全是 arXiv**。
# 这是「让 Bocha 只搜学术站」的关键开关，代价是失去通用网页来源。
ACADEMIC_DOMAINS = (
    "arxiv.org",
    "aclanthology.org",
    "openreview.net",
    "dl.acm.org",
    "ieeexplore.ieee.org",
    "nature.com",
    "science.org",
    "sciencedirect.com",
    "link.springer.com",
    "doi.org",
    "openaccess.thecvf.com",
    "jmlr.org",
    "papers.nips.cc",
    "proceedings.mlr.press",
)


def academic_include() -> str:
    """Bocha `include` 参数值：逗号分隔的域名白名单。可用环境变量覆盖。"""
    raw = os.getenv("ACADEMIC_INCLUDE_DOMAINS", "")
    return raw.strip() if raw.strip() else ",".join(ACADEMIC_DOMAINS)

# 证据块探测：**只认结构信号**（标题行 / 带数据的表题 / 编号小节），
# 不认「正文里出现了 method/results 这些词」。
# 为什么必须收紧旧实现是关键词子串匹配，于是
# 「This is a synthetic abstract about method selection, dataset choice, and results
#  reporting.」这样一段**没有实验章节**的摘要会被判为三块齐备，
# 而下游据此认为「有 method/experiment/results 证据」——把关键词当成了证据块。
_BLOCK_HEADING_PATTERNS: dict[str, tuple[re.Pattern, ...]] = {
    "method": tuple(re.compile(p, re.I) for p in (
        r"^\s*#{0,6}\s*(?:\d+(?:\.\d+)*[\.\)]?\s*)?(?:methods?|methodology|approach|proposed\s+(?:method|approach|model|framework)|our\s+(?:method|approach|model|framework))\s*:?\s*$",
        r"^\s*#{0,6}\s*(?:\d+(?:\.\d+)*[\.\)]?\s*)?(?:methods?|methodology)\b[^\n]{0,40}$",
    )),
    "experiment_setup": tuple(re.compile(p, re.I) for p in (
        r"^\s*#{0,6}\s*(?:\d+(?:\.\d+)*[\.\)]?\s*)?(?:experiments?|experimental\s+(?:setup|settings?|protocol)|evaluation\s+(?:setup|protocol|settings?)|datasets?|data\s+sets?|benchmarks?)\s*:?\s*$",
        r"^\s*#{0,6}\s*(?:table|tab\.)\s*\d+[\.:]",
    )),
    "results": tuple(re.compile(p, re.I) for p in (
        r"^\s*#{0,6}\s*(?:\d+(?:\.\d+)*[\.\)]?\s*)?(?:results?|findings?|analysis|discussion|ablation(?:s| study| studies)?)\s*:?\s*$",
        r"^\s*#{0,6}\s*(?:table|tab\.|figure|fig\.)\s*\d+[\.:]",
    )),
    # PR5 / R10：`table` 是 rag_context 的必需证据块；只有**真正的表格结构**才算
    # （表题 + 分隔行 + 数据行），不认正文里出现 "table" 这个词。
    "table": tuple(re.compile(p, re.I) for p in (
        # Markdown 表：表头行 + 分隔行（|---|:--:|）
        r"^\s*#{0,6}\s*\|[^\n]*\|\s*$",
        r"^\s*#{0,6}\s*(?:table|tab\.)\s*\d+\s*[\.:：]",
    )),
    # PR5 / R10：`annotation_protocol` 是 llm_evaluation 的必需证据块；
    # 需要**标注协议类标题**，而不是正文里出现 "annotator" 就算。
    "annotation_protocol": tuple(re.compile(p, re.I) for p in (
        r"^\s*#{0,6}\s*(?:\d+(?:\.\d+)*[\.\)]?\s*)?(?:annotation\s+(?:protocol|process|guidelines?|scheme|manual)|"
        r"human\s+(?:evaluation\s+)?(?:protocol|guidelines?)|"
        r"labeling\s+(?:protocol|guidelines?|scheme)|coding\s+(?:scheme|manual)|"
        r"标注(?:协议|规范|指南|细则)|人工评测(?:协议|规范|指南))\s*:?\s*$",
    )),
}
# 表格数据行：≥2 个数值 token（用于确认「表有数据」，不只是表头）
_TABLE_ROW = re.compile(r"^\s*\|?[^\n|]{1,40}\|.*\d", re.M)
_SECTION_HEADING = re.compile(r"^\s*(?:#{1,6}\s+\S|\d+(?:\.\d+)*[\.\)]?\s+\S{2,60}\s*:?\s*$)")
# 标注协议必须同时具备「协议内容」信号，避免把只有标题的空章节算成可用证据块。
# 注意：**不含 guideline/指引**——它们常常就出现在标题里（Labeling Guidelines），
# 会被标题自身满足（代码评审的反例）。
_ANNOTATION_BODY = re.compile(
    r"(annotator|rater|标注(?:员|者)|agreement|kappa|一致性|rubric|评分标准|"
    r"sample\s+size|样本量|blind|双盲|随机)", re.I)


def section_body(text: str, heading_index: int, lines: list[str], offsets: list[int],
                 limit_chars: int = 2500) -> str:
    """标题**之后**、**本节范围之内**的正文（到下一个标题为止）。

    不能把标题行本身算作正文，也不能越过下一节去借用内容。
    """
    start_index = heading_index + 1
    end_index = len(lines)
    for i in range(start_index, len(lines)):
        if _SECTION_HEADING.match(lines[i]) and i > start_index:
            end_index = i
            break
    if start_index >= len(lines):
        return ""
    start = offsets[start_index]
    end = offsets[end_index] if end_index < len(offsets) else len(text)
    return text[start:min(end, start + limit_chars)]

# 生产者**实际能产出**的块类型词汇表（评审脚本的 R10
# 用 `set(rs._BLOCK_SIGNALS)` 取生产者词汇，再与各方向档案的 required blocks 对照）。
# 名字沿用旧实现以保证验收脚本可运行；PR2 起值是**结构模式**而不是关键词子串表。
_BLOCK_SIGNALS: dict[str, tuple[re.Pattern, ...]] = _BLOCK_HEADING_PATTERNS


def locate_blocks(text: str) -> dict[str, list[dict]]:
    """返回每个证据块的**字符区间**（可回查定位），没找到的块不出现。

    locator 是交付材料与原文之间的绑定依据（PR2：证据块必须绑定实际交付文本）。
    """
    found: dict[str, list[dict]] = {}
    if not text:
        return found
    lines = text.splitlines(keepends=True)
    offsets, pos = [], 0
    for line in lines:
        offsets.append(pos)
        pos += len(line)
    for name, patterns in _BLOCK_HEADING_PATTERNS.items():
        for index, line in enumerate(lines):
            if any(p.search(line.rstrip("\n")) for p in patterns):
                start = offsets[index]
                end = offsets[index + 1] if index + 1 < len(offsets) else len(text)
                if name == "experiment_setup" and line.lstrip().lower().startswith(("table", "tab.")):
                    # 表题：必须能在附近看到带数据行，否则只是「表格标题」
                    window = text[start:start + 1200]
                    if not _TABLE_ROW.search(window):
                        continue
                if name == "table":
                    # 只有表题/表头还不够：必须真的能读到**数据行**（PR5 / R10）。
                    # 同样只看本节范围（标题之后），不越过下一节借用内容。
                    body_text = section_body(text, index, lines, offsets, limit_chars=2000)
                    body = [ln for ln in body_text.splitlines() if ln.strip()]
                    has_separator = any(re.match(r"^\s*\|?[\s:\-|]+\|[\s:\-|]*$", ln) and "-" in ln
                                        for ln in body[:3])
                    has_data = any(_TABLE_ROW.match(ln) and len(re.findall(r"\d", ln)) >= 2
                                   for ln in body)
                    if not (has_separator or line.strip().startswith("|")) or not has_data:
                        continue
                if name == "annotation_protocol":
                    # 光有标题不算协议：**标题之后、本节之内**必须有标注者/一致性/样本量等内容。
                    # （空章节 "## Labeling Guidelines\n(见附录)" 不得算作协议块。）
                    if not _ANNOTATION_BODY.search(section_body(text, index, lines, offsets)):
                        continue
                found.setdefault(name, []).append({"start": start, "end": end,
                                                   "heading": line.strip()[:80]})
    return found


def detect_blocks(text: str) -> list[str]:
    """从正文推导可用的证据块（**结构性**判定，不是关键词子串判定）。

    返回块名列表（保持旧接口；R05/R09 依赖该形状）。判定依据是标题行/带数据的表题，
    因此「摘要里提到 method/dataset/results」不再算某块存在。
    """
    return sorted(locate_blocks(text).keys())


def select_excerpt(text: str, budget: int) -> dict:
    """按**相关章节**取片，而不是「截前 N 字符然后继承整篇的块标记」。

    交付给模型的文本与「块是否存在」必须绑定。
    策略：
      - 先定位各块所在行；有块时，把含块的区段连同上下文按预算拼起来（保序）；
      - 完全没有块时，退化为头部截断，并如实记录 truncation；
      - 无论哪种方式，返回的 `blocks` 都只由**交付出去的这段文本**重新检测得到。
    """
    if budget <= 0:
        return {"text": "", "strategy": "empty", "blocks": [], "locators": {}, "truncated": bool(text)}
    locators = locate_blocks(text)
    if not locators:
        head = text[:budget]
        return {"text": head, "strategy": "head_truncation", "blocks": detect_blocks(head),
                "locators": locate_blocks(head), "truncated": len(text) > len(head)}
    # 以块所在行为锚点，向后扩展窗口，再按预算裁剪并保持原文顺序
    spans: list[tuple[int, int]] = []
    for entries in locators.values():
        for entry in entries:
            start = entry["start"]
            end = min(len(text), start + max(600, budget // max(1, len(locators))))
            spans.append((start, end))
    spans.sort()
    merged: list[list[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    pieces, spent, truncated = [], 0, False
    for start, end in merged:
        if spent >= budget:
            truncated = True
            break
        piece = text[start:min(end, start + (budget - spent))]
        if len(piece) < (end - start):
            truncated = True
        pieces.append(piece)
        spent += len(piece)
    delivered = "\n\n[...]\n\n".join(pieces)
    return {"text": delivered, "strategy": "section_slices", "blocks": detect_blocks(delivered),
            "locators": locate_blocks(delivered), "truncated": truncated or len(delivered) < len(text)}


def classify_source(url: str) -> tuple[str, str | None]:
    """返回 (platform_kind, None)。kind 见 SOURCE_KINDS；未识别为 ""。

    注意：kind 是**平台**属性，**不是**评审状态（见 peer_review_status）。
    保留该函数名与返回值形状，兼容既有调用方。
    """
    return source_kind(url), None


def source_kind(url: str) -> str:
    """域名 -> 平台分类（"" = 非学术站点）。"""
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return ""
    for frag, kind in SOURCE_KINDS.items():
        if host == frag or host.endswith("." + frag):
            return kind
    return ""


def is_academic(url: str) -> bool:
    """是否是可识别的学术站点（决定要不要抓全文），与评审状态无关。"""
    return bool(source_kind(url))


def fetch_enabled_for(url: str) -> bool:
    return source_kind(url) in _FETCHABLE_KINDS


def extract_openreview_forum_id(url: str) -> str | None:
    """OpenReview 的 forum id 是**作品身份的一部分**：不同 forum 必须是不同作品（R03）。

    旧实现按 host+path 归一，于是 forum?id=paper_A 与 forum?id=paper_B 都变成
    `openreview.net/forum`——把两篇不同作品合成一个，直接破坏作品级去重与计数。
    """
    m = _OPENREVIEW_FORUM.search(url)
    if m:
        return m.group(1)
    parsed = urlsplit(url)
    if (parsed.hostname or "").lower().endswith("openreview.net"):
        # 形如 /forum/paper_A 的路径式写法
        parts = [p for p in (parsed.path or "").split("/") if p]
        if len(parts) >= 2 and parts[0] in {"forum", "pdf"}:
            return parts[1]
    return None


def peer_review_status(result: dict, raw_html: str = "", object_meta: dict | None = None) -> tuple[bool | None, str]:
    """依**对象自身**的 venue 证据判定是否同行评审；无法判定时返回 (None, 原因)。

    为什么不能看域名：`doi.org` 是解析器、`openalex.org`/`semanticscholar.org` 是元数据聚合、
    `arxiv.org` 是预印本、`openreview.net` 上既有录用投稿也有未录用投稿。

    **普通正文不能决定 peer_reviewed**——旧实现在正文前缀里找
    "accepted"，于是「Our parser accepted 100 rows」被判成录用证据。
    现在只接受两类来源：
      1. 对象元数据字段（`venue`/`journal`/`comments`，来自来源对象自身）；
      2. **原始 HTML 的发表元数据**（citation_journal_title / citation_conference_title /
         citation_inbook_title，由抓取层解析并作为 object_meta 传入）。
    正文（text）完全不再参与判定。
    """
    url = str(result.get("url") or "")
    kind = source_kind(url)
    if kind in {"metadata", "doi_resolver"}:
        return None, "metadata_or_resolver_platform: 域名不构成评审证据"
    meta = dict(object_meta or {})
    if raw_html:
        meta.update(parse_object_meta(raw_html))
    venue_fields = " ".join(str(result.get(f) or "") for f in ("venue", "journal", "comments"))
    for key in _VENUE_META_KEYS:
        value = str(meta.get(key) or "").strip()
        if value and "arxiv" not in value.lower():
            if _REJECTED_HINTS.search(venue_fields):
                return False, f"venue_evidence_negative: {value[:60]}"
            return True, f"venue_metadata:{key}: {value[:60]}"
    if venue_fields.strip():
        if _REJECTED_HINTS.search(venue_fields):
            return False, "venue_evidence_negative"
        if _ACCEPTED_HINTS.search(venue_fields):
            return True, "venue_evidence_accepted"
    return None, "no_venue_evidence"


# --- 日期层（PR2 / P0-4）：三个日期分开保存，绝不用抓取时间冒充发表日期 ----------
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_DATE_META_PATTERNS = (
    r'<meta[^>]+name=["\']citation_publication_date["\'][^>]+content=["\']([^"\']+)',
    r'<meta[^>]+name=["\']citation_online_date["\'][^>]+content=["\']([^"\']+)',
    r'<meta[^>]+name=["\']dc\.date["\'][^>]+content=["\']([^"\']+)',
    r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)',
)
_ARXIV_DATES = re.compile(r"\[Submitted on (?P<first>\d{1,2} \w{3} \d{4})(?P<rest>[^\]]*)\]", re.I)
_ARXIV_VERSION_DATE = re.compile(r"last revised (?P<date>\d{1,2} \w{3} \d{4})", re.I)
# `/abs/` 页的提交历史行：`[v1] Tue, 23 Jul 2024 20:51:52 UTC (425 KB)`
_ARXIV_SUBMISSION_LINE = re.compile(r"\[v(\d+)\]\s*(?:[A-Za-z]{3},?\s+)?(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})")
# 每个 work 的元数据只取一次；额外请求/命中/失败/超预算单独记账。
_ARXIV_METADATA_CACHE: dict[str, dict] = {}
# 同一 work 的**进行中请求合并**：并发调用等同一把锁，第二个调用命中缓存而不是再发一次请求。
_ARXIV_METADATA_LOCKS: dict[str, "threading.Lock"] = {}
METADATA_STATS = {"requests": 0, "cache_hits": 0, "failures": 0, "skipped_budget": 0}
# **硬预算**：本次进程内新增的元数据请求上限（可用环境变量调整）。这是**进程累计**上限，
# 不是「每任务限额」，也不等于速率限制；超预算时不再发请求，日期保持未知 → 由既有窗口规则延后。
METADATA_MAX_REQUESTS = _int_env("ARXIV_METADATA_MAX_REQUESTS", 40)


def _iso_date(raw: Any) -> str | None:
    """把常见日期串规范化为 YYYY-MM-DD；认不出返回 None（**不猜**）。"""
    if not raw:
        return None
    text = str(raw).strip()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{4})", text)
    if m and (month := _MONTHS.get(m.group(2).lower())):
        return f"{m.group(3)}-{month:02d}-{int(m.group(1)):02d}"
    m = re.search(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", text)
    if m and (month := _MONTHS.get(m.group(1).lower())):
        return f"{m.group(3)}-{month:02d}-{int(m.group(2)):02d}"
    return None


def parse_dates(result: dict, text: str = "", object_meta: dict | None = None) -> dict:
    """分别解析三个日期，**绝不用抓取时间冒充发表日期**。

    - `publication_first_date`：作品首次公开（对象元数据 / arXiv「Submitted on」）
    - `version_date`：本次读到的这个版本的日期（arXiv「last revised」，或单版本时的提交日期）
    - `retrieved_at`：我们抓取的时间（抓取时写入，与前两者无关）
    搜索接口给的 `dateLastCrawled` 只记为 `provider_crawled_at`，**不参与**时间窗口判定；
    `datePublished` 记为 `provider_published_date`，同样不作为首发日期的依据。

    `object_meta` 是**从原始 HTML 解析出来的**元数据——
    正文抽取会剥掉 `<meta>`，所以必须由抓取层单独传入，不能再指望从 text 里找。
    """
    publication = _iso_date(result.get("publication_first_date"))
    version = _iso_date(result.get("version_date"))
    date_source = "input" if publication else "unknown"
    meta = object_meta or {}
    for key in _DATE_META_KEYS:
        if meta.get(key) and (parsed := _iso_date(meta[key])):
            if not publication:
                publication, date_source = parsed, f"object_meta:{key}"
            break
    m = _ARXIV_DATES.search(text[:20000])
    if m:
        if not publication and (parsed := _iso_date(m.group("first"))):
            publication, date_source = parsed, "arxiv_submitted_on"
        revised = _ARXIV_VERSION_DATE.search(m.group("rest") or "")
        if revised and (parsed := _iso_date(revised.group("date"))):
            version = version or parsed
        elif not version and publication:
            # 只有一次提交（没有 last revised）时，该版本的日期就是提交日期。
            # 这是**同一份对象元数据**的直接读法，不是把抓取时间当发表日期。
            version = publication
    version_source = "arxiv_last_revised" if version and version != publication else (
        "same_as_first_publication" if version else "unknown")
    return {
        "publication_first_date": publication,
        "version_date": version,
        "version_date_source": version_source,
        "retrieved_at": result.get("retrieved_at"),
        "provider_published_date": _iso_date(result.get("provider_published_date")),
        "provider_crawled_at": result.get("provider_crawled_at"),
        "date_source": date_source,
    }


def within_window(result: dict, start_year: int | None, end_year: int | None) -> tuple[bool | None, str]:
    """硬时间窗口判定：True / False / **None（时间未确认）**。

    PR2（P0-4）+ 关键词里的「2022–2025」不是过滤器，必须硬判定；
    而且必须区分三件事：
      - `inside_window`：**该文本版本的时间已确认**且在窗口内；
      - `outside_window:<date>`：已知日期越界；
      - `version_time_unconfirmed`：首发已知但**版本日期未知/版本未明** →
        归入待确认（单列，**不进入严格版本证据集合**）。
    不能因为「首发在范围内」就当作「这个版本属于历史窗口」。
    """
    if start_year is None or end_year is None:
        return True, "no_window"
    first = result.get("publication_first_date")
    version = result.get("version_date")
    for value in (first, version):
        if not value:
            continue
        try:
            year = int(str(value)[:4])
        except (TypeError, ValueError):
            return None, "date_unparsable"
        if year < int(start_year) or year > int(end_year):
            return False, f"outside_window:{value}"
    if not first:
        return None, "publication_date_unknown"
    if not version:
        return None, "version_time_unconfirmed"
    return True, "inside_window"


def extract_arxiv_id(url: str) -> str | None:
    """从 URL 里取 arXiv ID（**去掉**版本号，用于作品级归并）。"""
    m = _ARXIV_URL.search(url)
    if m:
        return m.group(1)
    # 形如 doi.org/10.48550/arXiv.2502.05589
    if "arxiv" in url.lower():
        m = _ARXIV_ID.search(url)
        if m:
            return m.group(1)
    return None


def extract_arxiv_version(url: str) -> str | None:
    """从 URL 里取 arXiv 版本号（如 "v1"）。取不到返回 None——**不猜版本**。"""
    m = _ARXIV_URL.search(url)
    if m and m.group(2):
        return m.group(2)
    return None


def version_identity(result: dict) -> dict:
    """来源的**文本版本**身份（Work 之下的第二层，）。

    三层必须分开且不可互相冒充：
      Work     = 哪一篇作品（`work_identity`，跨 vN 归并）
      Version  = 哪一个确定的文本版本（本函数）
      Study    = 该版本描述的哪一项研究/实验（需要实验级证据，见 independence）
    """
    url = str(result.get("url") or "")
    arxiv_id = extract_arxiv_id(url)
    requested_version = extract_arxiv_version(url)
    fetched_url = str(result.get("fulltext_url") or "")
    resolved_version = extract_arxiv_version(fetched_url)
    if arxiv_id:
        version = resolved_version or requested_version or str(result.get("metadata_version") or "") or None
        if resolved_version:
            source = "fetched_url"
        elif requested_version:
            source = "requested_url"
        elif version:
            # 版本号来自 `/abs/` 元数据的版本列表（无版本号输入 = 最新版）
            source = "arxiv_abs_metadata"
        else:
            source = "unknown"
        return {
            "version_id": f"arxiv:{arxiv_id}{version}" if version else None,
            "version_source": source,
            "version_date": result.get("version_date"),
            "publication_first_date": result.get("publication_first_date"),
            "retrieved_at": result.get("retrieved_at"),
            "requested_url": url,
            "fetched_url": fetched_url or None,
        }
    # 非 arXiv：正文版本 = 实际抓取到的地址（冻结下来，不再随最新版漂移）
    version_id = fetched_url or None
    return {
        "version_id": version_id,
        "version_source": "fetched_url" if fetched_url else "unknown",
        "version_date": result.get("version_date"),
        "publication_first_date": result.get("publication_first_date"),
        "retrieved_at": result.get("retrieved_at"),
        "requested_url": url,
        "fetched_url": fetched_url or None,
    }


def extract_doi(url: str, *extra_text: str) -> str | None:
    """从 URL（其次传递的额外文本）里取 DOI。

    **extra_text 不得用于确定作品身份**：搜索摘要/标题里出现的 DOI 常常是
    「本文讨论了别人的论文」，把该 DOI 绑给当前网页等于把转述者认成原作者
    。作品身份只认来源对象**自身的定位符**。
    """
    m = _DOI.search(url)
    if m:
        return _DOI_TAIL.sub("", m.group(1))
    for text in extra_text:
        if text:
            m = _DOI.search(str(text))
            if m:
                return _DOI_TAIL.sub("", m.group(1))
    return None


def work_identity(result: dict) -> str | None:
    """给一条来源确定**稳定的作品身份**（=哪一篇作品），仅用于作品去重。

    取值优先级：
      1. arXiv ID（跨 v1/v2/v3 归并同一篇作品）
      2. OpenReview **forum id**（不同 forum = 不同作品，R03）
      3. DOI（**只从该来源自身的 URL 解析**，不从摘要/标题/正文里的引用提取）
      4. **已识别的学术站点的规范化 URL**（host + path，去掉 query/fragment）
         —— 没有稳定标识也不能退化成"每次都是新研究"
    非学术来源（博客/聚合站/门户）返回 None：**不算作品**，不得充当独立证据。

    ⚠️ 作品身份 ≠ 研究独立性：拿到 work_id 只说明「这是哪篇作品」，
    不能证明论断来自该论文自己的实验、不是在转述、两篇没复用同一实验结果。
    独立性由 independence_* 字段单独表达（见 annotate_work_identity）。
    """
    url = str(result.get("url") or "")
    arxiv_id = extract_arxiv_id(url)
    if arxiv_id:
        return f"arxiv:{arxiv_id}"
    forum_id = extract_openreview_forum_id(url)
    if forum_id:
        return f"openreview:{forum_id}"
    # 只从对象自身定位符解析 DOI：snippet/summary 里的 DOI 属于被引用的他人作品。
    doi = extract_doi(url)
    if doi:
        return f"doi:{doi.lower()}"
    kind = source_kind(url)
    if kind:
        parsed = urlsplit(url)
        path = (parsed.path or "").rstrip("/")
        if path and path != "":
            return f"{parsed.hostname}{path}"
    return None


# 搜索结果/缓存对象是**不可信输入**：其中的研究身份与独立性断言一律清除。
# `annotate_work_identity` 在没解析出身份时会提前返回，于是输入里携带的
# `study_id` / `independence_verified=True` 会被原样继承——「适配器一律把独立性降为 unknown」
# 因此不是真实保证。可信人工标注将来走独立入口，不与未验证搜索结果混用。
UNTRUSTED_IDENTITY_FIELDS = (
    "study_id", "independence_verified", "independence_status", "independence_basis",
    "independence_evidence", "work_id", "evidence_role", "experiment_id",
)


def strip_untrusted_identity(result: dict) -> dict:
    """清除来源对象中不应受信任的研究身份/独立性字段（在写任何新标注之前）。"""
    cleaned = dict(result)
    for field in UNTRUSTED_IDENTITY_FIELDS:
        cleaned.pop(field, None)
    return cleaned


def annotate_work_identity(enriched: dict, identity: str | None = None) -> None:
    """写作品身份，并把独立性如实标为 unknown（不自动升级为已核查）。

    旧实现直接 `independence_verified = True`：把「找到了作品标识」当成了
    「研究独立性成立」。独立性需要**独立证据**
    （实验级身份、非转述角色、非同源、独立复现记录），在该证据补齐前一律 unknown。
    调用前必须先 `strip_untrusted_identity()`（见 enrich_result / enrich_results）。
    """
    identity = identity or work_identity(enriched)
    if not identity:
        return
    enriched["work_id"] = identity
    enriched["independence_status"] = "unknown"
    enriched["independence_verified"] = False
    enriched["independence_basis"] = "work identity only; no experiment-level or replication evidence"


def candidate_fulltext_urls(url: str) -> list[str]:
    """给定来源 URL，给出可尝试抓全文的候选地址。

    PR2 / R04：**输入里显式给出的版本必须贯穿所有候选地址**。
    旧实现用去掉 vN 的 arXiv ID 拼候选，于是输入 `abs/2501.12345v1` 会去抓
    `html/2501.12345`（=最新版）——「输入 v1 却悄悄读最新版」正是 P0-4 的时间/版本漂移。
    无版本号的输入保持无版本（不猜），抓取后用实际地址冻结 version_id。
    """
    cands: list[str] = []
    arxiv_id = extract_arxiv_id(url)
    if arxiv_id:
        version = extract_arxiv_version(url) or ""
        ident = f"{arxiv_id}{version}"
        # html 版正文最完整（实测 11 万字符），失败再退回 abs 页与 PDF
        cands.append(f"https://arxiv.org/html/{ident}")
        cands.append(f"https://arxiv.org/abs/{ident}")
        cands.append(f"https://arxiv.org/pdf/{ident}")
    else:
        cands.append(url)
    # 去重且保序
    seen: set[str] = set()
    return [u for u in cands if not (u in seen or seen.add(u))]


def _extract_pdf_text(raw: bytes) -> str:
    try:
        from PyPDF2 import PdfReader
    except Exception:  # noqa: BLE001
        return ""
    try:
        reader = PdfReader(io.BytesIO(raw))
        return "\n".join((page.extract_text() or "") for page in reader.pages[:40])
    except Exception:  # noqa: BLE001
        return ""


def _html_to_text(html: str) -> str:
    # 优先 trafilatura（项目已依赖，网页正文抽取效果最好）
    try:
        import trafilatura
        got = trafilatura.extract(html)
        if got and len(got) > 500:
            return got
    except Exception:  # noqa: BLE001
        pass
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n"))
    except Exception:  # noqa: BLE001
        return re.sub(r"<[^>]+>", " ", html)


_HTML_META = re.compile(
    r'<meta[^>]+(?:name|property)=["\']([^"\']+)["\'][^>]+content=["\']([^"\']*)["\']', re.I)
_DATE_META_KEYS = ("citation_publication_date", "citation_online_date", "dc.date", "article:published_time")
_VENUE_META_KEYS = ("citation_journal_title", "citation_conference_title", "citation_inbook_title")


def parse_object_meta(html: str) -> dict:
    """从**原始 HTML** 解析对象自身元数据（日期与发表venue）。

    这些 `<meta>` 在正文抽取时会被剥掉，所以必须在
    `_html_to_text()` **之前**解析；否则「先转文本再找 meta」必然失败。
    """
    meta: dict[str, str] = {}
    for name, content in _HTML_META.findall(html or ""):
        key = name.strip().lower()
        if key in _DATE_META_KEYS or key in _VENUE_META_KEYS:
            meta.setdefault(key, (content or "").strip())
    return meta


def document_scope(final_url: str, content_type: str, text: str) -> str:
    """这次抓到的到底是**正文**、摘要，还是压根不是论文页面。

    三层信息必须分开，不能把「全文入口返回了一些文字」当成「该论文正文已核查」：
      1. **载体类型**：地址/类型看起来是全文入口、摘要页还是 PDF；
      2. **获取与解析结果**：实际拿到的是论文文本，还是错误页/验证页/残缺解析；
      3. **实际交付范围**：由 `available_blocks` / `excerpt_truncated` 另行记录（见 `source_packets`）。

代码评审复核后的原则（不建通用网页分类器，只补已批准的 arXiv 适配）：
      **错误页信号用于排除；论文对象身份与正文结构用于确认；两边都不够时保持未知。**
    因此 `fulltext_page` / `pdf` 只给「有论文结构或对象身份」的正文；否则退到
    `unverified_page`（保守：不算已确认正文，由审查门挡住科学域结论）。

    取值：`fulltext_page` / `pdf` / `abstract_page` / `short_page` / `error_page` /
    `unverified_page` / `none`。
    """
    low = (final_url or "").lower()
    body = text or ""
    chars = len(body)
    if chars == 0:
        return "none"
    # 2) 解析结果优先于载体：HTTP 200 的「服务暂不可用 / 需要人机验证」页面
    #    常常正好落在 /html/ 地址上（代码评审用合成响应复现过），必须识别出来。
    if page_looks_like_error(body):
        return "error_page"
    is_pdf = content_type == "pdf" or "arxiv.org/pdf/" in low or low.endswith(".pdf")
    is_html_entry = "arxiv.org/html/" in low or "arxiv.org/html" in low
    # 1) 载体类型 + **正向确认**：只有看到论文结构或对象身份才认作正文。
    if "arxiv.org/abs/" in low:
        return "abstract_page"
    if is_pdf:
        return "pdf" if chars >= _PAPER_MIN_CHARS else "short_page"
    if is_html_entry or chars >= 8000:
        if has_paper_structure(body):
            return "fulltext_page"
        # 入口/长度像正文，但没有任何论文结构、也看不到对象身份 → 不宣称「已取得正文」。
        return "unverified_page" if chars >= _PAPER_MIN_CHARS else "short_page"
    return "short_page"


# 错误页/验证页的**措辞**信号。刻意保守：只匹配明确的服务/验证/拦截语义，
# 不匹配论文里可能出现的普通英文（例如 "not available" 这种通用说法不收录）。
_ERROR_PAGE_SIGNALS = (
    "service temporarily unavailable", "service unavailable", "please retry later",
    "request verification required", "verify you are human", "enable javascript and cookies",
    "access denied", "just a moment", "checking your browser", "captcha",
    "too many requests", "rate limit exceeded", "bad gateway", "gateway timeout",
)
# 整行式的通用错误标题（**只在标题行本身很短时**才算错误页）。刻意不使用 "error" 这种
# 单词子串——「Error Bounds for Reliable Retrieval」是正常论文标题（代码评审反例）。
_ERROR_TITLE_LINES = (
    "error", "403 forbidden", "404 not found", "service unavailable", "access denied",
    "just a moment", "forbidden", "bad gateway", "gateway timeout", "too many requests",
    "verify you are human", "request verification required", "service temporarily unavailable",
)
_ERROR_TITLE_MAX_CHARS = 60
# 措辞信号密度：长页面里错误措辞**反复出现**同样判为错误页（代码评审的长错误页反例）。
_ERROR_SIGNAL_DENSITY_MIN = 3
_ERROR_DENSITY_MAX_CHARS = 20000
_PAPER_MIN_CHARS = 1500
# 论文结构/对象身份的正向信号（用于确认，不用于否定）
_PAPER_STRUCTURE_PATTERNS = (
    re.compile(r"(?im)^\s*(?:\d+\.?\s*)?(abstract)\b"),
    re.compile(r"(?im)^\s*(?:\d+\.?\s*)?(introduction|related work|method(?:s|ology)?|approach|"
               r"experiments?|experimental setup|results?|discussion|conclusions?|references)\b"),
    re.compile(r"(?im)^\s*(table|figure)\s*\d+"),
    re.compile(r"arxiv:\d{4}\.\d{4,5}", re.I),
)


def has_paper_structure(text: str) -> bool:
    """这段文本是否具备**论文结构或对象身份**的正向证据（确认层，不是排除层）。"""
    body = text or ""
    if len(body) < _PAPER_MIN_CHARS:
        return False
    hits = sum(1 for pattern in _PAPER_STRUCTURE_PATTERNS if pattern.search(body))
    return hits >= 2


def page_looks_like_error(text: str) -> bool:
    """这段交付文本是否更像错误页/验证页，而不是论文正文。

    两条判据（都是**负面启发式**，用于排除）：
      1. **标题行**：首个非空行本身就是一个短的通用错误标题（整行匹配，不做子串匹配）；
      2. **措辞密度**：错误措辞在页面里反复出现（≥3 次且页面 < 20k 字符），
         这样 5k+ 字符的重复错误页也不会因为「太长」而漏判。

    刻意不把长度当唯一判据，也不会因为论文讨论 "error bounds" 而拒绝它。
    """
    body = (text or "").strip()
    if not body:
        return False
    first_line = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    title = first_line.strip("#* \t").lower()
    if title and len(title) <= _ERROR_TITLE_MAX_CHARS and title in _ERROR_TITLE_LINES:
        return True
    if len(body) > _ERROR_DENSITY_MAX_CHARS:
        return bool(has_paper_structure(body)) is False and _error_signal_hits(body) >= 8
    return _error_signal_hits(body) >= _ERROR_SIGNAL_DENSITY_MIN


def _error_signal_hits(text: str) -> int:
    low = (text or "").lower()
    return sum(low.count(signal) for signal in _ERROR_PAGE_SIGNALS)
    if any(hint in head for hint in _ERROR_PAGE_TITLE_HINTS):
        return True
    return any(sig in low for sig in _ERROR_PAGE_SIGNALS)


def _arxiv_metadata(arxiv_id: str) -> dict:
    """同一 work 的**官方 `/abs/` 元数据**：首发日期与各版本日期。

    为什么要单独取一次元数据：正文页（`/html/`、`/pdf/`）**不含**提交历史，而 `/abs/` 页含
    `[v1] Tue, 23 Jul 2024 …`；只抓正文就会让「有正文的没日期、有日期的没正文」。
    这里只补这一条 arXiv 适配，不新接 Semantic Scholar / OpenAlex，不做通用抓取平台。

    返回：
      ok / url / final_url / retrieved_at / first_published / versions / latest /
      history_snippet / history_hash / source
    解析不出日期时 `ok=False`，调用方保持「日期未知」，**不用抓取时间冒充**。

    边界
      - **对象校验**：`_fetch_cached()` 重定向后的最终地址必须属于被请求的 work，
        否则 `ok=False`（`reason=work_mismatch_response`）——不能把别的论文的提交历史记成本 work 的；
      - **并发合并**：同一 work 的进行中请求只发一次（按 work 加锁），并发调用等锁后命中缓存；
      - 记账是**进程累计**（`METADATA_STATS`），不是每任务限额，也不等于速率限制。
    """
    work_id = f"arxiv:{arxiv_id}"
    if work_id in _ARXIV_METADATA_CACHE:
        METADATA_STATS["cache_hits"] += 1
        return _ARXIV_METADATA_CACHE[work_id]
    # 同一 work 的进行中请求合并：第二个线程等锁后直接命中缓存，不会重复发请求。
    lock = _ARXIV_METADATA_LOCKS.setdefault(work_id, threading.Lock())
    with lock:
        if work_id in _ARXIV_METADATA_CACHE:
            METADATA_STATS["cache_hits"] += 1
            return _ARXIV_METADATA_CACHE[work_id]
        result = _fetch_arxiv_metadata(arxiv_id)
        _ARXIV_METADATA_CACHE[work_id] = result
        return result


def _fetch_arxiv_metadata(arxiv_id: str) -> dict:
    """真正发请求并解析的那一步（调用方负责缓存与并发合并）。"""
    url = f"https://arxiv.org/abs/{arxiv_id}"
    # 硬预算：超限就不再请求，并把这个「没取」也缓存下来（避免每次重试又去撞预算）。
    if METADATA_MAX_REQUESTS and METADATA_STATS["requests"] >= METADATA_MAX_REQUESTS:
        METADATA_STATS["skipped_budget"] += 1
        return {"ok": False, "url": url, "final_url": None, "first_published": None,
                "versions": {}, "latest": None, "source": "arxiv_abs_metadata",
                "skipped": "budget", "reason": "budget"}
    METADATA_STATS["requests"] += 1
    result: dict = {"ok": False, "url": url, "final_url": None, "retrieved_at": _now_iso(),
                    "first_published": None, "versions": {}, "latest": None,
                    "history_snippet": None, "history_hash": None,
                    "source": "arxiv_abs_metadata"}
    try:
        doc = _fetch_cached(url)
        text = str(doc.get("text") or "")
        final_url = str(doc.get("final_url") or url)
        result["final_url"] = final_url
        # 对象校验：最终地址必须指向被请求的同一篇作品。
        if extract_arxiv_id(final_url) != arxiv_id:
            result["reason"] = "work_mismatch_response"
            METADATA_STATS["failures"] += 1
            return result
        start = text.find("Submission history")
        snippet = text[start:start + 600] if start >= 0 else text[:600]
        result["history_snippet"] = snippet
        result["history_hash"] = _sha256(snippet)
        for match in _ARXIV_SUBMISSION_LINE.finditer(text):
            version, raw_date = match.group(1), match.group(2)
            iso = _iso_date(raw_date)
            if iso:
                result["versions"][version] = iso
        first = None
        m = _ARXIV_DATES.search(text)
        if m:
            first = _iso_date(m.group("first"))
        if not first and result["versions"]:
            first = result["versions"][min(result["versions"], key=int)]
        result["first_published"] = first
        if result["versions"]:
            result["latest"] = max(result["versions"], key=int)
        result["ok"] = bool(first or result["versions"])
    except Exception as exc:  # noqa: BLE001 — 元数据失败必须如实降级，不能编日期
        result["reason"] = type(exc).__name__
        METADATA_STATS["failures"] += 1
        return result
    if not result["ok"]:
        result.setdefault("reason", "no_submission_history")
        METADATA_STATS["failures"] += 1
    return result


def arxiv_metadata_for(url: str) -> dict | None:
    """按 URL 取该 work 的 arXiv 元数据（非 arXiv / 取不到返回 None）。"""
    arxiv_id = extract_arxiv_id(url)
    if not arxiv_id:
        return None
    meta = _arxiv_metadata(arxiv_id)
    return meta or None


def bind_arxiv_dates(enriched: dict) -> dict:
    """把 `/abs/` 元数据绑到**同一个 work 的具体版本**上。

    分支顺序（复核后收紧；`identity_binding` 是给入证门看的结论）：
      1. 正文对象 ≠ 请求对象 → `work_mismatch`，**不**写任何日期；
      2. 请求显式 vN、正文也是显式 vN：相同 → 绑定；**不同 → `requested_content_version_mismatch`，不写日期**
         （不能因为「返回的版本很明确」就当它是请求指定的版本）；
      3. 请求显式 vN、正文无 vN → `version_unconfirmed_in_content`，不写日期；
      4. 请求无 vN、正文显式 vN → 以正文版本为准（`content_url_version`）；
      5. 请求无 vN、正文也无 vN → `unversioned_content_unverified`，**不写日期**：
         独立缓存的 latest 不能证明这份匿名正文就是那一版（正确做法是先解析 N 再抓显式 vN，
         见 `enrich_result` 的 `version_resolution`）。
      6. 版本日期以**被确认版本的官方提交历史**为准：与已有日期冲突时**校正并记录**
         （`version_date_correction`），不用「非空优先」保留来源冲突的旧值。
    元数据文本**绝不拼进正文**；`metadata_provenance` / `content_provenance` 分开保存，
    并带取回时间与提交历史片段哈希。
    """
    requested_url = str(enriched.get("url") or "")
    content_url = str(enriched.get("fulltext_url") or "")
    arxiv_id = extract_arxiv_id(requested_url) or extract_arxiv_id(content_url)
    if not arxiv_id:
        return enriched
    requested_version = extract_arxiv_version(requested_url)
    content_version = extract_arxiv_version(content_url)
    content_arxiv = extract_arxiv_id(content_url)
    if content_arxiv and content_arxiv != arxiv_id:
        enriched["identity_binding"] = "work_mismatch"
        enriched["version_selection"] = "work_mismatch"
        enriched["metadata_provenance"] = {"source": "arxiv_abs_metadata", "merged": False,
                                           "reason": "work_mismatch",
                                           "requested_work": f"arxiv:{arxiv_id}",
                                           "content_work": f"arxiv:{content_arxiv}"}
        return enriched

    meta = _arxiv_metadata(arxiv_id)
    enriched["metadata_provenance"] = {
        "source": meta.get("source"), "url": meta.get("url"), "final_url": meta.get("final_url"),
        "ok": bool(meta.get("ok")), "reason": meta.get("reason"), "skipped": meta.get("skipped"),
        "retrieved_at": meta.get("retrieved_at"),
        "first_published": meta.get("first_published"),
        "versions": dict(meta.get("versions") or {}),
        "history_snippet": meta.get("history_snippet"), "history_hash": meta.get("history_hash"),
        "requested_version": requested_version, "content_version": content_version,
        "recorded_at": _now_iso(),
    }
    if not meta.get("ok"):
        enriched["identity_binding"] = "unverified_metadata"
        enriched["version_selection"] = ("metadata_budget_exhausted" if meta.get("skipped") == "budget"
                                         else "metadata_unavailable")
        return enriched

    if requested_version:
        if not content_version:
            enriched["identity_binding"] = "unverified_version"
            enriched["version_selection"] = "version_unconfirmed_in_content"
            return enriched
        if content_version != requested_version:
            enriched["identity_binding"] = "version_mismatch"
            enriched["version_selection"] = "requested_content_version_mismatch"
            return enriched
        chosen, selection = content_version, "content_url_version"
    elif content_version:
        chosen, selection = content_version, "content_url_version"
    else:
        enriched["identity_binding"] = "unverified_version"
        enriched["version_selection"] = "unversioned_content_unverified"
        return enriched

    key = chosen.lstrip("v")
    versions = meta.get("versions") or {}
    if key not in versions:
        enriched["identity_binding"] = "unverified_version"
        enriched["version_selection"] = "metadata_missing_requested_version"
        return enriched
    enriched["identity_binding"] = "verified"
    enriched["metadata_version"] = f"v{key}"
    enriched["version_selection"] = selection
    if not enriched.get("publication_first_date") and meta.get("first_published"):
        enriched["publication_first_date"] = meta["first_published"]
        enriched["date_source"] = "arxiv_abs_metadata:first_published"
    official = versions[key]
    existing = enriched.get("version_date")
    if existing and str(existing) != str(official):
        # 冲突不静默保留：以被确认版本的官方提交历史为准，并记录校正来源。
        enriched["version_date_correction"] = {"from": str(existing), "to": str(official),
                                               "basis": "arxiv_abs_metadata:submission_history"}
    enriched["version_date"] = official
    enriched["version_date_source"] = f"arxiv_abs_metadata:{selection}"
    enriched["content_provenance"] = {"requested_url": requested_url, "final_url": content_url,
                                      "version": f"v{key}", "selection": selection,
                                      "version_resolution": enriched.get("version_resolution")}
    return enriched


@lru_cache(maxsize=256)
def _fetch_cached(url: str) -> dict:
    """抓取单个候选地址。

    返回 dict（失败时 text 为空）：
      text / content_type / final_url（**重定向后的真实地址**）/ object_meta（原始 HTML 元数据）
    为什么必须带 final_url：`requests` 允许重定向，`response.url` 才是实际读到的地址；
    只回候选地址会让「无版本号输入 → 抓取后冻结实际版本」永远拿不到 vN。
    """
    empty = {"text": "", "content_type": "", "final_url": url, "object_meta": {}}
    try:
        resp = requests.get(url, timeout=FETCH_TIMEOUT, headers={"User-Agent": _UA},
                            allow_redirects=True, stream=True)
        if resp.status_code != 200:
            return empty
        ctype = (resp.headers.get("Content-Type") or "").lower()
        final_url = str(getattr(resp, "url", "") or url)
        raw = resp.raw.read(MAX_BYTES + 1, decode_content=True) or b""
        if len(raw) > MAX_BYTES:
            raw = raw[:MAX_BYTES]
        if "pdf" in ctype or final_url.lower().endswith(".pdf"):
            return {"text": _extract_pdf_text(raw), "content_type": "pdf",
                    "final_url": final_url, "object_meta": {}}
        html = raw.decode(resp.encoding or "utf-8", errors="replace")
        # 元数据必须在正文抽取**之前**解析（抽取会剥掉 <meta>）
        object_meta = parse_object_meta(html)
        return {"text": _html_to_text(html), "content_type": "html",
                "final_url": final_url, "object_meta": object_meta}
    except Exception:  # noqa: BLE001
        return empty


def fetch_document(url: str) -> dict:
    """抓取来源正文，返回完整结果（含重定向后的最终地址与对象元数据）。

    `fetch_fulltext()` 保留为三元组兼容包装（既有探针按三元组 mock 它）。
    """
    for cand in candidate_fulltext_urls(url):
        got = _fetch_cached(cand)
        text = got.get("text") or ""
        if text and len(text) > 800:  # 太短的多半是错误页/跳转页，视为失败
            return {"text": text[:MAX_CHARS], "final_url": got.get("final_url") or cand,
                    "content_type": got.get("content_type"), "object_meta": got.get("object_meta") or {},
                    "requested_url": cand, "ok": True}
    return {"text": "", "final_url": url, "content_type": "", "object_meta": {},
            "requested_url": url, "ok": False}


def fetch_fulltext(url: str) -> tuple[str, str, str | None]:
    """兼容包装：返回 (text, effective_url, arxiv_id)。

    effective_url 现在是**重定向后的最终地址**（不再等于候选地址）。
    """
    doc = fetch_document(url)
    if not doc["ok"]:
        return "", url, extract_arxiv_id(url)
    return doc["text"], doc["final_url"], extract_arxiv_id(doc["final_url"])


def enrich_result(result: dict, fetch_limit_chars: int = MAX_CHARS, *, allow_fetch: bool = False) -> dict:
    """给单条搜索结果补作品身份标注；**默认不联网**（`allow_fetch=True` 才抓全文）。

    抓全文是网络副作用。旧实现的默认行为是「学术 URL 就静默发 HTTP」，
    于是任何自称离线的测试一旦调用它就真的会联网（实测捕获到 arxiv.org 的两次请求）；
    更糟的是抓取层吞掉异常后测试仍然通过——「网络失败被吞掉，不等于测试没有联网」。
    现在副作用必须由调用方显式开启：`scout` 在 `ENABLE_FULLTEXT_FETCH` 打开时传 True。

    这里是契约里所说的那个 **trusted adapter / annotation** 的实现
    （`research_integrity.source_packets` 注释要求 study_id / independence_verified
    必须来自可信适配器、绝不能来自 LLM 响应）。

    **独立性与作品身份分开**：
      - 有稳定作品身份（arXiv ID / DOI）→ 只写 `work_id`（**哪篇作品**，用于作品级去重），
        `independence_status = "unknown"`、`independence_verified = False`；
      - 取不到稳定身份（普通网页、博客、聚合站）→ 不写 work_id，同样不算研究。
    `study_id`（**哪项实验**）在拿到实验级证据前一律不写：作品身份冒充研究身份，
    正是「多个 work_id 被当成独立验证」的根源。
    上限：同一 work_id 的不同版本仍是同一票（作品级去重保留）。

    输入边界：本入口只接受**原始搜索对象**。进入时先清除其中
    携带的研究身份/独立性字段，再重新标注——否则「没解析出身份就提前返回」会把
    输入里的旧 `study_id`/`independence_verified=True` 原样继承下去。

    PR2 分层**Work / Version / Study 三层分开**。
      - `work_id`（哪篇作品，跨 vN 归并）/ `version_id`（哪个文本版本，保留 vN，抓取后冻结）
      - `study_id` 与 `independence`（哪项实验、独立性证据）——本层只写 unknown 结构，
        **绝不**因为「有作品身份」就填成已验证。
    材料状态（`material_status` + `material_reasons`）如实记录：
      fetched / metadata_only / fetch_not_attempted / fetch_failed / parse_failed，
    抓不到正文就是抓不到，不把它解释成「论文没有报告」。
    """
    url = str(result.get("url") or "")
    if not url:
        return strip_untrusted_identity(result)

    enriched = strip_untrusted_identity(result)
    kind = source_kind(url)
    if kind:
        enriched["source_kind"] = kind
        enriched["document_type"] = kind  # 兼容旧字段名（值是平台分类，不再是评审状态）

    # --- 第 1 层：作品身份 ---
    annotate_work_identity(enriched)
    enriched.setdefault("retrieved_at", None)
    dates = parse_dates(enriched, "")
    enriched.update(dates)

    if not kind:
        enriched["material_status"] = "metadata_only"
        enriched["material_reasons"] = ["non_academic_host"]
        enriched["peer_reviewed"], enriched["peer_review_reason"] = peer_review_status(enriched, "")
        enriched.update(version_identity(enriched))
        return enriched

    if not allow_fetch or not fetch_enabled_for(url):
        enriched["material_status"] = "fetch_not_attempted"
        enriched["material_reasons"] = (["fetch_disabled_by_caller"] if not allow_fetch
                                        else [f"platform_not_fetchable:{kind}"])
        enriched["peer_reviewed"], enriched["peer_review_reason"] = peer_review_status(enriched, "")
        enriched.update(version_identity(enriched))
        return enriched

    # --- 无版本号的 arXiv 输入：先解析出「当前采用的版本 N」，再抓**显式 vN** 正文 ---
    # 不能先抓匿名正文、再用一份独立缓存的 latest 给它贴版本——
    # 两份材料的快照可能不同步。这里把顺序反过来：解析 N → 抓 vN → 核对对象与版本 → 取 versions[N]。
    fetch_url = url
    arxiv_input = extract_arxiv_id(url)
    if arxiv_input and not extract_arxiv_version(url):
        meta = _arxiv_metadata(arxiv_input)
        latest = meta.get("latest")
        if meta.get("ok") and latest:
            fetch_url = f"https://arxiv.org/abs/{arxiv_input}v{latest}"
            enriched["version_resolution"] = "metadata_latest_before_fetch"
            enriched["version_resolution_metadata"] = {
                "url": meta.get("url"), "final_url": meta.get("final_url"),
                "retrieved_at": meta.get("retrieved_at"), "latest": f"v{latest}",
                "history_hash": meta.get("history_hash")}
        else:
            enriched["version_resolution"] = "metadata_unavailable_before_fetch"

    doc = fetch_document(fetch_url)
    if not doc["ok"]:
        enriched["material_status"] = "fetch_failed"
        enriched["material_reasons"] = ["fetch_failed_or_too_short"]
        enriched["peer_reviewed"], enriched["peer_review_reason"] = peer_review_status(enriched, "")
        enriched.update(version_identity(enriched))
        return enriched

    text = doc["text"]
    effective = doc["final_url"]
    fetch_arxiv = extract_arxiv_id(effective)
    enriched["content"] = text[:fetch_limit_chars]
    enriched["fulltext_url"] = effective
    enriched["fulltext_chars"] = len(text)
    # 对象自身的元数据（发表 venue / 日期）来自**原始 HTML**，与正文一起进入来源对象；
    # 只有它们能作为 peer_reviewed 与首发日期的依据。
    enriched["object_meta"] = doc.get("object_meta") or {}
    # **全文级别**的块信息只作诊断：真正交付给模型的是 source_packets 里的摘录，
    # 那里的 available_blocks 只能由**交付出去的文本**重新检测（PR2 / R09）。
    enriched["fulltext_blocks"] = detect_blocks(text)
    enriched["fulltext_block_locators"] = locate_blocks(text)
    enriched["material_status"] = "fetched"
    enriched["material_scope"] = document_scope(effective, str(doc.get("content_type") or ""), text)
    enriched["material_reasons"] = ["fetched_fulltext" if enriched["material_scope"] in {"fulltext_page", "pdf"}
                                    else f"only_{enriched['material_scope']}_available"]
    enriched["corpus_snapshot"] = {"chars": len(text), "sha256": _sha256(text),
                                   "head": text[:1500]}
    # 抓全文后若才发现 arXiv 身份，补齐作品身份（独立性仍保持 unknown）
    if not enriched.get("work_id") and fetch_arxiv:
        annotate_work_identity(enriched, f"arxiv:{fetch_arxiv}")
    # 日期与评审状态：meta（原始 HTML）+ 正文里的 arXiv 行；版本日期以实际抓到的版本为准
    enriched.update(parse_dates(enriched, text, object_meta=enriched["object_meta"]))
    # 正文页**不含**提交历史：只有拿到 `/abs/` 元数据才能同时具备「正文 + 版本日期」。
    # 这一步只补 arXiv 的同对象元数据，且不把元数据文本混进正文
    # PDF 与 HTML 走同一绑定逻辑——候选地址保留了显式 vN 时才能绑定该版本。
    bind_arxiv_dates(enriched)
    enriched["peer_reviewed"], enriched["peer_review_reason"] = peer_review_status(
        enriched, raw_html="", object_meta=enriched["object_meta"])
    enriched.update(version_identity(enriched))
    return enriched


def enrich_results(results: list[dict], per_query_limit: int = 0, *, allow_fetch: bool = False) -> list[dict]:
    """对一批搜索结果补作品身份（默认不联网）；`allow_fetch=True` 时才对学术来源抓全文。

    两件事分开做，互不牵制：
      1. **作品身份标注** 对所有来源执行 —— 即使抓不到全文，
         只要有 arXiv ID / DOI 就能确定"这是哪篇作品"，从而参与**作品级**去重；
      2. **全文抓取**只对可识别学术来源、且受 per_query_limit 限制（控制延迟与成本），
         并且必须由调用方显式开启网络（PR1.1）。

    per_query_limit > 0 时只对前 N 条学术来源实际发起抓取。
    """
    limit = per_query_limit or _int_env("FULLTEXT_PER_QUERY_LIMIT", 3)
    out: list[dict] = []
    fetched = 0
    for result in results:
        url = str(result.get("url") or "")
        # 2) 全文抓取（仅可抓平台、受限额约束、且调用方显式允许联网）；
        #    抓不到的来源也走 enrich_result，从而同样带上材料状态与版本/日期信息。
        if allow_fetch and fetched < limit and fetch_enabled_for(url):
            enriched = enrich_result(result, allow_fetch=True)
            fetched += 1
        else:
            enriched = enrich_result(result, allow_fetch=False)
        out.append(enriched)
    return out
