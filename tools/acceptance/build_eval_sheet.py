#!/usr/bin/env python3
"""PR5 最小人工评测工具链：冻结语料清单 + 证据边 + **待人工确认**标签 + 指标（分子/分母）。

设计约束：
  - **不给自己判分**：只产出候选标签（`label: null`、`status="unconfirmed"`），
    人确认后才计入指标；`machine_suggestion` 只作参考，且指标一律从**冻结输出**读取机器预测；
  - **export / validate-labels / score 三段分离**：export 默认**不覆盖**已有标签；
  - 指标分母用**机器预测 supports** 的集合，而不是人工判为 supports 的集合；
  - 标签必须通过校验（枚举、证据 ID 归属、查重、标注者非空、样本哈希绑定）才生效；
  - 冻结语料按 (url, version, content_hash) 分条，保留实际交付文本；policy/代码哈希取自真实对象。

用法：
    python tools/acceptance/build_eval_sheet.py export --from-checkpoint <session_id> --out <dir>
    python tools/acceptance/build_eval_sheet.py validate-labels --out <dir>
    python tools/acceptance/build_eval_sheet.py score --out <dir>
    python tools/acceptance/build_eval_sheet.py --selftest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _find_repo_root(start: Path) -> Path:
    """从脚本位置向上找到真正的仓库根（含 backend/app/service/research_integrity.py）。

    旧实现固定 `parents[2] / "industry_information_assistant"`，
    在打包布局里会多拼一层，导致代码哈希与检查点导入路径找错。
    """
    for candidate in [start, *start.parents]:
        if (candidate / "backend" / "app" / "service" / "research_integrity.py").is_file():
            return candidate
    nested = start / "industry_information_assistant"
    if (nested / "backend" / "app" / "service" / "research_integrity.py").is_file():
        return nested
    return start


_SCRIPT = Path(__file__).resolve()
REPO = _find_repo_root(_SCRIPT.parents[2])
sys.path.insert(0, str(REPO / "backend" / "app"))

LABEL_ENUM = ("supports", "refutes", "neutral", "inconclusive")
ROLE_ENUM = ("original", "replication", "secondary", "unknown")
ABSTAIN_STATUSES = {"insufficient_evidence", "evidence_limited"}

METRIC_SPEC = {
    "wrong_support_count": "机器预测为 supports 的边中，人工未确认支持的条数 / 机器预测 supports 的已标注边数",
    "support_precision": "机器预测 supports 且人工确认 supports / 机器预测 supports 的已标注边数",
    "evidence_type_accuracy": "人工确认的证据角色与机器判断一致 / 有角色标签的边数",
    "citation_coverage": "人工确认定位正确 / 有定位核对标签的边数（**不使用机器 quote_bound**）",
    "abstention_rate": "结论为 insufficient_evidence 或 evidence_limited 的论断 / 全部论断（含没有证据边的）",
    "condition_mismatch_detected": "人工判为条件不可比且机器标记 incompatible / 人工判为条件不可比的边数",
}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def code_hashes() -> dict:
    """固定代码与配置：提交号 + 关键模块哈希（找不到就如实记 None，不编造）。"""
    out: dict = {}
    for rel in ("service/research_integrity.py", "service/research_sources.py",
                "service/research_runtime.py", "service/direction_contracts.py",
                "service/deep_research_v2/agents/data_analyst.py"):
        path = REPO / "backend" / "app" / rel
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    try:
        out["git_commit"] = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO,
                                           capture_output=True, check=True).stdout.decode().strip()
    except Exception:  # noqa: BLE001
        out["git_commit"] = None
    out["repo_root"] = str(REPO)
    return out


def policy_hash(state: dict) -> str:
    """对**实际生效的策略**取哈希（round2 / N10：旧实现哈希的是固定字符串）。"""
    direction = state.get("resolved_direction") or {}
    profile = (direction.get("profile") or {}) if isinstance(direction, dict) else {}
    payload = {
        "source_policy": profile.get("source_policy"),
        "claim_types": profile.get("claim_types"),
        "profile_hash": direction.get("profile_hash") if isinstance(direction, dict) else None,
        "global_policy": profile.get("global_policy") or profile.get("global_policy_ref"),
        "research_config": state.get("research_config"),
    }
    return _sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str))


def corpus_manifest(state: dict, out_dir: Path | None = None) -> dict:
    """冻结语料清单。

    
      - 以 **(url, version_id, content_hash)** 为键，**不得**把同一 URL 的不同版本折成一条；
      - 累积每个条目的全部内容哈希；
      - 保存**实际交付文本**（完整快照写文件），不只是 hash 与前 1000 字；
      - 登记被拒/未知日期/没有证据边的材料，避免只评「系统愿意输出的部分」。
    """
    entries: dict[tuple, dict] = {}
    for fact in state.get("facts", []):
        url = fact.get("source_url")
        if not url:
            continue
        version = fact.get("source_version")
        content_hash = fact.get("source_content_hash")
        key = (url, str(version), str(content_hash))
        entry = entries.setdefault(key, {
            "url": url, "version_id": version, "source_name": fact.get("source_name"),
            "locator": fact.get("source_locator"),
            "content_hashes": [], "fact_ids": [],
            "quote_bound_facts": 0, "source_only_facts": 0,
            "material_status": fact.get("source_material_status"),
            "delivered_blocks": fact.get("source_delivered_blocks"),
            "publication_first_date": fact.get("publication_first_date"),
            "version_date": fact.get("version_date"),
        })
        entry["fact_ids"].append(fact.get("id"))
        if content_hash and content_hash not in entry["content_hashes"]:
            entry["content_hashes"].append(content_hash)
        if fact.get("provenance_status") == "quote_bound":
            entry["quote_bound_facts"] += 1
        else:
            entry["source_only_facts"] += 1
    for packet in state.get("date_deferred_sources", []) or []:
        key = (packet.get("url"), str(packet.get("version_id")), "deferred")
        entries.setdefault(key, {"url": packet.get("url"), "version_id": packet.get("version_id"),
                                 "status": "deferred", "deferred_reason": packet.get("reason")})
    for packet in state.get("evidence_audit", []) or []:
        if packet.get("reason") == "outside_time_window":
            for item in packet.get("items", []) or []:
                key = (item.get("url"), str(item.get("version_id")), "rejected")
                entries.setdefault(key, {"url": item.get("url"), "status": "rejected_out_of_window",
                                         "reason": item.get("reason")})
    # 交付文本快照：整篇写入文件（可重放），不是只保留头部
    written_snapshot_digests: set[str] = set()
    for source in state.get("raw_sources", []) or []:
        url = source.get("url")
        content = str(source.get("content") or "")
        if not url or not content:
            continue
        digest = _sha256(content)
        matched = [e for key, e in entries.items() if key[0] == url]
        if not matched:
            continue
        for entry in matched:
            entry["delivered_text_sha256"] = digest
            entry["delivered_text_chars"] = len(content)
            if out_dir is not None:
                snap = out_dir / "snapshots"
                snap.mkdir(parents=True, exist_ok=True)
                if digest not in written_snapshot_digests:
                    (snap / f"{digest}.txt").write_text(content, encoding="utf-8")
                    written_snapshot_digests.add(digest)
                entry["snapshot_path"] = f"snapshots/{digest}.txt"
    snapshot_records = 0
    snapshot_missing = 0
    for entry in entries.values():
        if "delivered_text_sha256" not in entry:
            entry["snapshot_available"] = False
            snapshot_missing += 1
        else:
            entry["snapshot_available"] = True
            snapshot_records += 1
    counts = {"snapshot_records": snapshot_records,
              "snapshot_files": len(written_snapshot_digests),
              "snapshot_missing": snapshot_missing}
    return {"generated_from": "run checkpoint", "entry_count": len(entries),
            "counts": counts,
            "entries": [entries[k] for k in sorted(entries, key=lambda k: (str(k[0]), str(k[1]), str(k[2])))]}


def claim_rows(state: dict) -> list[dict]:
    """导出**全部真实计划论断**，包括没有证据边的论断。

    被延后或拒绝的材料只属于 ``corpus_manifest.json`` 的材料/审计清单，
    不是研究命题，不能进入论断数量或弃权率分母。
    """
    rows = []
    for hypothesis in state.get("hypotheses", []) or []:
        edges = [e for e in (state.get("evidence_links") or [])
                 if e.get("hypothesis_id") == hypothesis.get("id")]
        rows.append({
            "claim_id": hypothesis.get("id"),
            "claim_text": hypothesis.get("content") or hypothesis.get("statement") or hypothesis.get("question"),
            "claim_type_id": hypothesis.get("claim_type_id"),
            "conditions": hypothesis.get("conditions") or {},
            "comparison_mode": hypothesis.get("comparison_mode"),
            "comparison_fields": hypothesis.get("comparison_fields"),
            "status": hypothesis.get("status"),
            "limitations": hypothesis.get("limitations"),
            "evidence_edge_count": len(edges),
            "child_claims": hypothesis.get("child_claims") or [],
            "candidate_propositions": hypothesis.get("candidate_propositions") or [],
            "assessment": hypothesis.get("assessment"),
        })
    return rows


def evidence_rows(state: dict) -> list[dict]:
    """导出证据边（含机器预测、条件可比性、采用/排除理由、以及**完整论断文本与条件**）。"""
    facts = {f.get("id"): f for f in state.get("facts", [])}
    hypotheses = {h.get("id"): h for h in state.get("hypotheses", [])}
    rows = []
    for edge in state.get("evidence_links", []):
        fact = facts.get(edge.get("fact_id"), {})
        hyp = hypotheses.get(edge.get("hypothesis_id"), {})
        adopted = hyp.get("adopted_evidence") or {}
        excluded = {e.get("evidence_link_id"): e.get("reason") for e in (adopted.get("excluded") or [])}
        rows.append({
            "evidence_id": edge.get("id"),
            "claim_id": edge.get("hypothesis_id"),
            "claim_text": hyp.get("content") or hyp.get("statement") or hyp.get("question"),
            "claim_conditions": hyp.get("conditions") or {},
            "claim_comparison_fields": hyp.get("comparison_fields"),
            "claim_status": hyp.get("status"),
            "claim_limitations": hyp.get("limitations"),
            "fact_id": edge.get("fact_id"),
            "source": {"url": fact.get("source_url"), "version": fact.get("source_version"),
                       "locator": fact.get("source_locator"), "name": fact.get("source_name"),
                       "material_status": edge.get("material_status")},
            "quote": fact.get("source_quote"),
            "stance": edge.get("stance"),
            "evidence_role": edge.get("evidence_role"),
            "quote_bound": edge.get("quote_bound"),
            "contract_complete": edge.get("contract_complete"),
            "conditions": edge.get("reported_conditions") or {},
            "condition_evidence": edge.get("condition_evidence") or {},
            "condition_compatibility": edge.get("condition_compatibility") or {},
            "adopted": str(edge.get("id")) in set(adopted.get("evidence_link_ids") or []),
            "exclusion_reason": excluded.get(str(edge.get("id"))),
        })
    return rows


def label_sheet(rows: list[dict]) -> list[dict]:
    """候选标签：**留空待人工确认**。附样本哈希以便绑定本次冻结样本。"""
    sample_hash = _sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False, default=str))
    sheet = []
    for row in rows:
        sheet.append({
            "evidence_id": row["evidence_id"],
            "claim_id": row["claim_id"],
            "sample_hash": sample_hash,
            "source_url": row["source"]["url"],
            "source_version": row["source"]["version"],
            "locator": row["source"]["locator"],
            "quote": row["quote"],
            # 机器预测**冻结**在此：评分时从这里读，不信任标签文件里被改过的字段
            "machine_prediction": {
                "stance": row["stance"],
                "evidence_role": row["evidence_role"],
                "condition_verdict": (row["condition_compatibility"] or {}).get("verdict"),
                "quote_bound": row["quote_bound"],
            },
            # ↓↓↓ 以下由**人工**填写；完成后把 status 改成 confirmed 并署名
            "label": None,                      # supports | refutes | neutral | inconclusive
            "label_evidence_role": None,        # original | replication | secondary | unknown
            "label_conditions_comparable": None,  # true | false | unknown
            "label_locator_correct": None,      # true | false —— 引用是否真的指向该材料
            "label_note": None,
            "status": "unconfirmed",
            "needs_user_confirmation": True,
            "annotator": None,
        })
    return sheet


def validate_labels(rows: list[dict], labels: list[dict]) -> tuple[list[dict], list[dict]]:
    """返回 (有效标签, 无效明细)。

    校验枚举、证据 ID 归属、论断 ID 一致、
    查重、标注者非空；若标签带 sample_hash 且与本次样本不符 → 无效。
    **机器预测从 rows 读取**，不使用标签文件里的 machine_suggestion 计算正确率。
    """
    by_id = {row["evidence_id"]: row for row in rows}
    sample_hash = _sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False, default=str))
    valid, invalid, seen = [], [], set()
    for label in labels:
        eid = label.get("evidence_id")
        if label.get("status") != "confirmed":
            continue
        problems = []
        if eid not in by_id:
            problems.append("unknown_evidence_id")
        if label.get("claim_id") != (by_id.get(eid, {}) or {}).get("claim_id"):
            problems.append("claim_mismatch")
        if label.get("label") not in LABEL_ENUM:
            problems.append("bad_label_enum")
        if not str(label.get("annotator") or "").strip():
            problems.append("missing_annotator")
        if eid in seen:
            problems.append("duplicate_evidence_id")
        if label.get("sample_hash") and label["sample_hash"] != sample_hash:
            problems.append("sample_hash_mismatch")
        if label.get("label_evidence_role") not in (None, *ROLE_ENUM):
            problems.append("bad_role_enum")
        comparable = label.get("label_conditions_comparable")
        if comparable is not None and not isinstance(comparable, bool) and comparable != "unknown":
            problems.append("bad_conditions_comparable_enum")
        locator = label.get("label_locator_correct")
        if locator is not None and not isinstance(locator, bool):
            problems.append("bad_locator_correct_type")
        if problems:
            invalid.append({"evidence_id": eid, "problems": problems})
            continue
        seen.add(eid)
        valid.append(label)
    return valid, invalid


def compute_metrics(rows: list[dict], labels: list[dict], claims: list[dict] | None = None) -> dict:
    """只统计**有效且已确认**的标签；给出分子/分母；分母为 0 时该项为 null。

    
      - 分母是**机器预测 supports** 的已标注边集合 P，而不是「人工判为 supports」的集合；
      - 错误支持数从同一个 P 统计，不依赖自由文本备注；
      - 无效/未绑定/未署名标签不得启用任何指标。
    """
    valid, invalid = validate_labels(rows, labels)
    if not valid:
        return {"available": False, "reason": "no_valid_confirmed_labels",
                "invalid": invalid, "spec": METRIC_SPEC,
                "note": "人未确认或标签无效前不产出任何指标——不用系统自评分冒充评测结果"}
    by_id = {row["evidence_id"]: row for row in rows}
    predicted_support = [by_id[v["evidence_id"]] for v in valid
                         if by_id[v["evidence_id"]].get("stance") == "supports"]
    support_labels = [v for v in valid if by_id[v["evidence_id"]].get("stance") == "supports"]
    correct_support = [v for v in support_labels if v.get("label") == "supports"]
    wrong_support = [v for v in support_labels if v.get("label") != "supports"]

    role_pairs = [(v, by_id[v["evidence_id"]]) for v in valid if v.get("label_evidence_role")]
    locator_pairs = [(v, by_id[v["evidence_id"]]) for v in valid if v.get("label_locator_correct") is not None]
    not_comparable = [v for v in valid if v.get("label_conditions_comparable") is False]
    mismatch_detected = [v for v in not_comparable
                         if (by_id[v["evidence_id"]].get("condition_compatibility") or {}).get("verdict")
                         == "incompatible"]

    if claims is None:
        universe = sorted({v["claim_id"] for v in valid if v.get("claim_id")})
        universe_note = "claims_with_labelled_edges（调用方未提供全部论断清单）"
    else:
        universe = sorted({str(c.get("claim_id") or c.get("id")) for c in claims
                           if c.get("claim_id") or c.get("id")})
        universe_note = "all_planned_claims"
    status_by_claim = {}
    for row in rows:
        status_by_claim.setdefault(row["claim_id"], row.get("claim_status"))
    for claim in (claims or []):
        claim_id = claim.get("claim_id") or claim.get("id")
        if claim_id:
            status_by_claim[str(claim_id)] = claim.get("status")
    abstained = [c for c in universe if status_by_claim.get(c) in ABSTAIN_STATUSES]

    def metric(numerator: int, denominator: int) -> dict:
        return {"numerator": numerator, "denominator": denominator} if denominator else \
               {"numerator": None, "denominator": 0}

    metrics = {
        "wrong_support_count": metric(len(wrong_support), len(support_labels)),
        "support_precision": metric(len(correct_support), len(support_labels)),
        "evidence_type_accuracy": metric(
            len([1 for v, row in role_pairs if v.get("label_evidence_role") == row.get("evidence_role")]),
            len(role_pairs)),
        "citation_coverage": metric(
            len([1 for v, _row in locator_pairs if v.get("label_locator_correct") is True]),
            len(locator_pairs)),
        "abstention_rate": metric(len(abstained), len(universe)),
        "condition_mismatch_detected": metric(len(mismatch_detected), len(not_comparable)),
    }
    return {"available": True, "confirmed_valid_count": len(valid),
            "unconfirmed_count": sum(1 for l in labels if l.get("status") != "confirmed"),
            "invalid": invalid, "metrics": metrics, "spec": METRIC_SPEC,
            "samples": {
                "predicted_support_denominator": len(support_labels),
                "evidence_type_denominator": len(role_pairs),
                "citation_coverage_denominator": len(locator_pairs),
                "abstention_universe": len(universe), "abstention_universe_note": universe_note,
                "condition_mismatch_denominator": len(not_comparable),
            },
            "caveat": "样本量小，不得据此宣称泛化；分母为 0 的指标为 null（不可用），不得填漂亮分数。"}


def _read_labels(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_artifacts(state: dict, out: Path, *, overwrite_labels: bool = False) -> dict:
    """导出评测材料。

    **默认不覆盖**已有 `human_labels.jsonl`——
    已确认的人工标签必须保留；只为新增证据补行。要重建请显式 `overwrite_labels=True`。
    """
    out.mkdir(parents=True, exist_ok=True)
    rows = evidence_rows(state)
    claims = claim_rows(state)
    manifest = corpus_manifest(state, out_dir=out)
    new_sheet = label_sheet(rows)
    labels_path = out / "human_labels.jsonl"
    existing = [] if overwrite_labels else _read_labels(labels_path)
    if existing:
        by_id = {row.get("evidence_id"): row for row in new_sheet}
        merged = []
        for label in existing:
            fresh = by_id.pop(label.get("evidence_id"), None)
            if fresh is None:
                merged.append(label)                 # 历史标签保留（对应证据已不在本次样本中）
                continue
            merged.append({**fresh, **{k: v for k, v in label.items() if v is not None}})
        merged.extend(by_id.values())                # 新增证据补行
        sheet = merged
        preserved = sum(1 for label in existing if label.get("status") == "confirmed")
    else:
        sheet = new_sheet
        preserved = 0
    metrics = compute_metrics(rows, sheet, claims=claims)
    (out / "corpus_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "evidence_links.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    (out / "claims.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in claims) + "\n", encoding="utf-8")
    labels_path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in sheet) + "\n", encoding="utf-8")
    (out / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "config_and_code_hashes.json").write_text(json.dumps(
        {"code": code_hashes(), "run_config_hash": state.get("run_config_hash"),
         "run_models": state.get("run_models"), "run_flags": state.get("run_flags"),
         "evidence_semantics_version": state.get("evidence_semantics_version"),
         "policy_hash": policy_hash(state)}, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "README_如何标注.md").write_text(_LABEL_README, encoding="utf-8")
    return {"rows": len(rows), "claims": len(claims), "sheet": len(sheet),
            "preserved_confirmed_labels": preserved,
            "corpus_entries": manifest["entry_count"], "metrics_available": metrics["available"]}


_LABEL_README = """# 人工标注说明（必须由人完成）

1. `evidence_links.jsonl`：每条证据边（含机器预测、条件可比性与采用/排除理由）。
2. `human_labels.jsonl`：逐条核对 `quote` 与 `source_url`/`locator` 指向的**实际材料**
   （`corpus_manifest.json` 的 `snapshot_path` 指向冻结的完整交付文本）。
3. 填写：`label`（supports/refutes/neutral/inconclusive）、`label_evidence_role`、
   `label_conditions_comparable`（true/false/unknown）、`label_locator_correct`（true/false）、
   `label_note`；把 `status` 改为 `confirmed`，并在 `annotator` 写上你的标识。
4. 若机器预测 supports 但你认为不成立：`label` 填 refutes/neutral 即可，
   **不需要**依赖备注——指标从机器预测集合统计。
5. 评分：`python build_eval_sheet.py score --out <dir>`（只读标签，不重建）。
   校验标签：`python build_eval_sheet.py validate-labels --out <dir>`。
6. 重新导出不会覆盖已确认标签（除非显式 `--overwrite-labels`）。

> `machine_prediction` 只是**冻结的机器预测**，供对照；请在完成判读后再看它，避免给它盖章。
> 未确认或无效标签一律不计入指标；分母为 0 的指标显示为 null。
"""


def synthetic_state() -> dict:
    """自测用合成 state（明确合成，不代表真实文献）。"""
    setting = {"task": "long-doc QA", "dataset": "NarrativeQA", "model": "M", "metric": "accuracy"}
    return {
        "evidence_semantics_version": "evidence-semantics/3", "run_config_hash": "deadbeef",
        "run_models": {"writer": "m"}, "run_flags": {"LLM_ENABLE_THINKING": False},
        "resolved_direction": {"profile": {"source_policy": {"x": 1}, "claim_types": []}},
        "facts": [
            {"id": "fact_1", "source_url": "https://arxiv.org/abs/2501.00001v1", "source_name": "P1",
             "source_version": "https://arxiv.org/html/2501.00001v1", "source_locator": {"kind": "section", "id": "5"},
             "source_content_hash": "h1", "source_quote": "Our method reaches 62.5%", "provenance_status": "quote_bound",
             "source_material_status": "fetched", "source_delivered_blocks": ["method", "results"]},
            {"id": "fact_2", "source_url": "https://arxiv.org/abs/2501.00002v1", "source_name": "P2",
             "source_version": "https://arxiv.org/html/2501.00002v1", "source_locator": {"kind": "section", "id": "4"},
             "source_content_hash": "h2", "source_quote": "", "provenance_status": "source_only",
             "source_material_status": "fetched"},
        ],
        "raw_sources": [{"url": "https://arxiv.org/abs/2501.00001v1", "content": "x" * 1200}],
        "date_deferred_sources": [{"url": "https://arxiv.org/abs/2602.00003v1", "reason": "outside_window:2026-02-01"}],
        "hypotheses": [{"id": "H1", "content": "Long-context beats RAG on long-doc QA",
                        "status": "evidence_limited", "limitations": ["comparability_unknown"],
                        "conditions": setting, "comparison_fields": sorted(setting),
                        "adopted_evidence": {"evidence_link_ids": [], "excluded": [
                            {"evidence_link_id": "edge_1", "reason": "independence_not_verified"}]}}],
        "evidence_links": [{"id": "edge_1", "hypothesis_id": "H1", "fact_id": "fact_1", "stance": "supports",
                            "evidence_role": "original", "quote_bound": True, "contract_complete": True,
                            "material_status": "fetched", "reported_conditions": setting,
                            "condition_evidence": {"task": {"quote": "long-doc QA", "quote_bound": True}},
                            "condition_compatibility": {"verdict": "compatible", "fields": {}}}],
    }


def selftest() -> int:
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "eval"
        summary = write_artifacts(synthetic_state(), out)
        expected = ["corpus_manifest.json", "evidence_links.jsonl", "claims.jsonl", "human_labels.jsonl",
                    "metrics.json", "config_and_code_hashes.json", "README_如何标注.md"]
        missing = [f for f in expected if not (out / f).exists()]
        if missing:
            failures.append(f"缺少产物: {missing}")
        sheet = _read_labels(out / "human_labels.jsonl")
        if not sheet or any(r["status"] != "unconfirmed" or r["label"] is not None for r in sheet):
            failures.append("标签必须是未确认且 label 为空")
        if json.loads((out / "metrics.json").read_text(encoding="utf-8")).get("available") is not False:
            failures.append("未确认标签时指标必须不可用")
        # 人工确认一条后：指标给出分子/分母，且**重新导出不得覆盖**
        sheet[0].update(status="confirmed", label="supports", label_evidence_role="original",
                        label_conditions_comparable=True, label_locator_correct=True,
                        annotator="self-test")
        (out / "human_labels.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in sheet) + "\n", encoding="utf-8")
        write_artifacts(synthetic_state(), out)
        after = _read_labels(out / "human_labels.jsonl")
        if not after or after[0].get("status") != "confirmed":
            failures.append("重新导出覆盖了已确认标签")
        metrics = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
        if not metrics.get("available") or metrics["metrics"]["support_precision"] != {"numerator": 1, "denominator": 1}:
            failures.append(f"确认后指标不正确: {metrics.get('metrics')}")
        # 材料清单不得混入论断全集；无证据边的真实论断仍须保留在分母。
        state = synthetic_state()
        state["hypotheses"].append({"id": "H_NO_EDGE", "content": "A real claim without an evidence edge",
                                    "status": "insufficient_evidence"})
        actual_claims = claim_rows(state)
        if {c.get("claim_id") for c in actual_claims} != {"H1", "H_NO_EDGE"} or \
                any(str(c.get("claim_id", "")).startswith("material:") for c in actual_claims):
            failures.append(f"论断全集混入材料或漏掉无边论断: {actual_claims}")
        m_claims = compute_metrics(evidence_rows(state), sheet, claims=actual_claims)
        if m_claims.get("metrics", {}).get("abstention_rate") != {"numerator": 2, "denominator": 2}:
            failures.append(f"弃权分母未使用真实论断全集: {m_claims.get('metrics')}")
        # 2-sample 基准用例（人审一对一错）：确认 wrong_support_count=1/2, support_precision=1/2
        two_rows = [{"evidence_id": "e1", "claim_id": "c1", "stance": "supports"},
                    {"evidence_id": "e2", "claim_id": "c2", "stance": "supports"}]
        two_labels = [{"evidence_id": "e1", "claim_id": "c1", "status": "confirmed", "label": "supports", "annotator": "tester"},
                      {"evidence_id": "e2", "claim_id": "c2", "status": "confirmed", "label": "refutes", "annotator": "tester"}]
        m2 = compute_metrics(two_rows, two_labels)
        if m2.get("metrics", {}).get("wrong_support_count") != {"numerator": 1, "denominator": 2} or \
           m2.get("metrics", {}).get("support_precision") != {"numerator": 1, "denominator": 2}:
            failures.append(f"2-sample 基准用例计算错误: {m2.get('metrics')}")
        # 无效标签不得启用指标
        bad = compute_metrics(evidence_rows(synthetic_state()),
                              [{"evidence_id": "ghost", "claim_id": "x", "status": "confirmed",
                                "label": "invented", "annotator": None}])
        if bad.get("available") is not False:
            failures.append("无效标签竟然启用了指标")
        # 两个版本同一 URL 不得折叠
        state = synthetic_state()
        state["facts"].append(dict(state["facts"][0], id="fact_1v2", source_version="content:v2",
                                   source_content_hash="h2"))
        dedup_out = out / "dedup_manifest"
        manifest = corpus_manifest(state, out_dir=dedup_out)
        same_url = [e for e in manifest["entries"] if e.get("url") == state["facts"][0]["source_url"]]
        if len(same_url) != 2:
            failures.append(f"同一 URL 的两个版本被折叠: {len(same_url)}")
        if manifest["counts"].get("snapshot_records") != 2 or \
                manifest["counts"].get("snapshot_files") != 1 or \
                len(list((dedup_out / "snapshots").glob("*.txt"))) != 1:
            failures.append(f"共享内容哈希的快照文件未去重: {manifest['counts']}")
        # policy_hash 必须随策略变化
        a, b = synthetic_state(), synthetic_state()
        b["resolved_direction"]["profile"]["source_policy"] = {"x": 2}
        if policy_hash(a) == policy_hash(b):
            failures.append("policy_hash 未随策略变化")
        print(json.dumps({"summary": summary, "metrics_after_confirm": metrics["metrics"]},
                         ensure_ascii=False, indent=2)[:900])
    if failures:
        print("SELFTEST FAILED: " + "; ".join(failures))
        return 1
    print("SELFTEST PASSED（产物齐全；不覆盖标签；无效标签不启用；指标口径正确；版本不折叠；策略哈希真实）")
    return 0


def _load_state(args) -> dict:
    if args.from_checkpoint:
        from dotenv import load_dotenv
        load_dotenv(REPO / ".env")
        from core.runtime_settings import sanitize_no_proxy
        sanitize_no_proxy()
        from core.database import SessionLocal
        from models import ResearchCheckpoint
        with SessionLocal() as db:
            row = (db.query(ResearchCheckpoint)
                   .filter(ResearchCheckpoint.session_id == args.from_checkpoint).first())
            if row is None:
                raise SystemExit(f"没有找到 session_id={args.from_checkpoint} 的检查点")
            return row.state_json or {}
    if args.state_file:
        return json.loads(Path(args.state_file).read_text(encoding="utf-8"))
    raise SystemExit("需要 --from-checkpoint 或 --state-file")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", nargs="?", default="export",
                    choices=["export", "validate-labels", "score"])
    ap.add_argument("--from-checkpoint")
    ap.add_argument("--state-file")
    ap.add_argument("--out")
    ap.add_argument("--overwrite-labels", action="store_true",
                    help="显式允许重建 human_labels.jsonl（会丢失未提交的人工工作）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    out = Path(args.out or (REPO / "docs" / "littrace" / "eval" / "latest"))
    if args.command == "export":
        summary = write_artifacts(_load_state(args), out, overwrite_labels=args.overwrite_labels)
        print(json.dumps({"out": str(out), **summary}, ensure_ascii=False, indent=2))
        print("提示：human_labels.jsonl 需要**人工**填写并标 confirmed；未确认前 metrics 不可用。")
        return 0
    if args.command == "validate-labels":
        rows = _read_labels(out / "evidence_links.jsonl")
        if not rows and (out / "evidence_links.jsonl").is_file():
            rows = [json.loads(x) for x in (out / "evidence_links.jsonl").read_text(encoding="utf-8").splitlines() if x]
        labels = _read_labels(out / "human_labels.jsonl")
        valid, invalid = validate_labels(rows, labels)
        print(json.dumps({"total": len(labels), "valid_confirmed": len(valid), "invalid": invalid},
                         ensure_ascii=False, indent=2))
        return 0 if not invalid else 1
    # score：只读既有标签计算指标，**不重建**任何文件
    rows = [json.loads(x) for x in (out / "evidence_links.jsonl").read_text(encoding="utf-8").splitlines() if x]
    labels = _read_labels(out / "human_labels.jsonl")
    claims_path = out / "claims.jsonl"
    if not claims_path.is_file():
        raise SystemExit(f"缺少完整论断清单: {claims_path}")
    claims = _read_labels(claims_path)
    metrics = compute_metrics(rows, labels, claims=claims)
    (out / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2)[:1500])
    return 0 if metrics.get("available") else 2


if __name__ == "__main__":
    raise SystemExit(main())
