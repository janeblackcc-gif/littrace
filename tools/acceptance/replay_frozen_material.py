#!/usr/bin/env python3
"""冻结材料**删块重放**：在真实交付文本的冻结副本上做声明式删除，重跑确定性判定并对比。

代码评审的原话（C 阶段负例口径）：
  「删块测试可以使用**冻结的真实材料副本**离线重放，不必每次付费检索。必须明确标为注入负例，
    不把删掉后的内容当成真实论文原貌。」
  「**不要只修改状态里的 `available_blocks` 来做删块测试。**要修改冻结文本，让实际结构检测重新运行，
    再走入证、审查和输出，才证明规则真的连接到了材料。」

本工具因此只做一件事：**改文本 → 重跑真实函数**。它不修改任何已保存的运行产物，
输出到单独的负例目录，并在结果里显式标注 `injection: true`。

重放链路（全部调用生产函数，不重新实现）：
  1. `research_sources.detect_blocks` / `select_excerpt`（结构检测与块判定，重新跑）
  2. `research_integrity.source_packets`（入证门与交付文本装配，重新跑）
  3. 对删后材料逐条重算事实的**引用绑定**（quote 是否仍在交付文本里）与
     **条件值定位**（`condition_evidence` 的 value/quote 是否仍可定位）
  4. `research_integrity.ingest_analysis` → `reduce_hypotheses` → `quality_verdict`
     （审查记录、论断状态与门禁结论全部重算）

用法（在**完整证据包**目录上运行）：
    python tools/acceptance/replay_frozen_material.py \
        --bundle <export_run_bundle 输出目录> \
        --edit "delete_block:table" --edit "delete_pattern:context budget" \
        --out <负例输出目录> [--source-url <只对某个来源生效>]
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / "backend" / "app"))


def load_modules():
    from dotenv import load_dotenv
    load_dotenv(BASE / ".env")
    from core.runtime_settings import sanitize_no_proxy
    sanitize_no_proxy()
    from service import research_integrity as R
    from service import research_sources as S
    return R, S


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def apply_edits(text: str, edits: list[str], S) -> tuple[str, list[dict]]:
    """执行声明式删除，返回 (新文本, 删除记录)。删除是**注入**，逐条记明删了什么。"""
    applied: list[dict] = []
    current = text
    for spec in edits:
        kind, _, arg = spec.partition(":")
        if kind == "delete_block":
            # 删除某个结构块的区段（标题行到下一个块标题之前）
            blocks = S.detect_blocks(current)
            if arg not in blocks:
                applied.append({"edit": spec, "applied": False, "reason": "block_not_detected"})
                continue
            before = current
            if arg == "table":
                # 代码评审的 RAG 负例：**保留表头、删除数据行**。
                current = _remove_table_rows(current)
            else:
                current = _remove_block_section(current, arg)
            applied.append({"edit": spec, "applied": current != before,
                            "removed_chars": len(before) - len(current)})
        elif kind == "delete_table_all":
            before = current
            current = _remove_table_rows(current, keep_header=0)
            applied.append({"edit": spec, "applied": current != before,
                            "removed_chars": len(before) - len(current)})
        elif kind == "delete_pattern":
            before = current
            current = re.sub(re.escape(arg), "", current, flags=re.I)
            applied.append({"edit": spec, "applied": current != before,
                            "removed_chars": len(before) - len(current)})
        elif kind == "delete_regex":
            before = current
            current = re.sub(arg, "", current)
            applied.append({"edit": spec, "applied": current != before,
                            "removed_chars": len(before) - len(current)})
        elif kind == "delete_paragraph_containing":
            before = current
            current = _delete_paragraph_containing(current, arg)
            applied.append({"edit": spec, "applied": current != before,
                            "removed_chars": len(before) - len(current)})
        elif kind in {"strip_block_body", "reference_only"}:
            before = current
            replacement = "" if kind == "strip_block_body" else "\n(see supplementary material; not delivered)\n"
            current = _replace_block_body(current, arg, replacement)
            applied.append({"edit": spec, "applied": current != before,
                            "removed_chars": len(before) - len(current)})
        else:
            applied.append({"edit": spec, "applied": False, "reason": "unknown_edit_kind"})
    return current, applied


_TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$")


def _remove_table_rows(text: str, keep_header: int = 2) -> str:
    """删除**结构性表格的数据行**（默认保留前两行：表头与分隔行）。

    为什么不是「删掉带 Table N 标题的段落」：本项目的 `table` 块是**结构判定**
    （PR5 / R10：标题 + 分隔行 + 数据行，正文里出现 "table" 这个词不算），
    冻结文本里的表格就是连续的 `| … |` 行。因此删块必须作用在这些结构行上，
    否则「改了文本但结构检测没变」就证明不了规则连接到材料。

    除了 `| … |` 行，还会删除表题行及其后紧邻的数据行（有些来源的表格不是管道表）。
    """
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    index = 0
    caption = re.compile(r"(?i)^\s*#{0,6}\s*(?:table|tab\.)\s*\d+\s*[\.:：]")
    while index < len(lines):
        line = lines[index]
        if _TABLE_LINE.match(line.rstrip("\n")):
            # 收集连续的表格行
            run_start = index
            while index < len(lines) and _TABLE_LINE.match(lines[index].rstrip("\n")):
                index += 1
            run = lines[run_start:index]
            out.extend(run[:keep_header] if keep_header else [])
            continue
        if caption.match(line):
            # 表题：连同其后紧邻的数据行一起删除，直到空行或下一个标题
            index += 1
            removed = 0
            while index < len(lines) and removed < 12:
                nxt = lines[index]
                if not nxt.strip() or caption.match(nxt) or re.match(r"^\s*#{1,6}\s", nxt):
                    break
                index += 1
                removed += 1
            continue
        out.append(line)
        index += 1
    return "".join(out)


def _remove_block_section(text: str, block: str) -> str:
    """按结构块的标题行删除该块所在区段（标题 + 其后的正文，直到下一个块标题或 1500 字符上限）。"""
    patterns = {
        "table": r"(?im)^\s*(table\s*\d+[^\n]*)$",
        "results": r"(?im)^\s*(?:\d+\.?\s*)?(results?|evaluation)\b[^\n]*$",
        "method": r"(?im)^\s*(?:\d+\.?\s*)?(method(?:s|ology)?|approach)\b[^\n]*$",
        "experiment_setup": r"(?im)^\s*(?:\d+\.?\s*)?(experimental setup|experiment setup|setup|datasets?)\b[^\n]*$",
        "annotation_protocol": r"(?im)^\s*(?:\d+\.?\s*)?(annotation protocol|annotation process|labeling guidelines|annotation guidelines)\b[^\n]*$",
    }
    pattern = patterns.get(block)
    if not pattern:
        return text
    match = re.search(pattern, text)
    if not match:
        return text
    start = match.start()
    tail = text[match.end():]
    nxt = re.search(r"(?im)^\s*(?:\d+\.?\s*)?(abstract|introduction|related work|method(?:s|ology)?|"
                    r"approach|experimental setup|experiment setup|results?|evaluation|discussion|"
                    r"conclusions?|references|table\s*\d+)\b[^\n]*$", tail)
    end = match.end() + (nxt.start() if nxt else min(len(tail), 1500))
    return text[:start] + text[end:]


def _replace_block_body(text: str, block: str, replacement: str) -> str:
    """保留目标标题，把该节正文替换为空或“补充材料未取得”占位。"""
    patterns = {
        "annotation_protocol": r"(?im)^\s*(?:\d+\.?\s*)?(?:annotation protocol|annotation process|"
                               r"labeling guidelines|annotation guidelines)\b[^\n]*$",
    }
    pattern = patterns.get(block)
    if not pattern:
        return text
    match = re.search(pattern, text)
    if not match:
        return text
    tail = text[match.end():]
    nxt = re.search(r"(?im)^\s*(?:\d+(?:\.\d+)*\.?\s+|#{1,6}\s+)[A-Z][^\n]*$", tail)
    end = match.end() + (nxt.start() if nxt else min(len(tail), 2000))
    return text[:match.end()] + replacement + text[end:]


def _delete_paragraph_containing(text: str, literal: str) -> str:
    """删除抓取正文中包含目标字面量的完整段落行，保留相邻段落。"""
    return "".join(line for line in text.splitlines(keepends=True)
                   if literal.lower() not in line.lower())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", required=True, help="export_run_bundle.py 的输出目录")
    ap.add_argument("--edit", action="append", default=[],
                    help="删块/删句声明：delete_block:<name> / delete_pattern:<literal> / delete_regex:<re>")
    ap.add_argument("--out", required=True)
    ap.add_argument("--source-url", default=None, help="只对该来源生效（默认全部交付来源）")
    ap.add_argument("--direction", required=True,
                    help="方向 preset id；强制重算比较合同、审查记录和下游门禁")
    ap.add_argument("--case-id", required=True)
    ap.add_argument("--expect-block-lost", action="append", default=[])
    ap.add_argument("--expect-text-lost", action="append", default=[],
                    help="目标文本：删前必须存在，删后必须不存在")
    ap.add_argument("--expect-quote-lost", action="store_true")
    ap.add_argument("--expect-condition-lost", action="store_true")
    ap.add_argument("--expect-condition-text", action="append", default=[], metavar="FIELD=LITERAL",
                    help="目标条件定位：删前 literal 必须绑定到正文，删后必须不可绑定")
    args = ap.parse_args()
    R, S = load_modules()
    bundle = Path(args.bundle)
    sources = read_jsonl(bundle / "delivered_sources.jsonl")
    facts = read_jsonl(bundle / "facts.jsonl")
    edges = read_jsonl(bundle / "evidence_links.jsonl")
    claims = read_jsonl(bundle / "claims.jsonl")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    edited_sources, edit_log = [], {}
    for src in sources:
        if args.source_url and src.get("url") != args.source_url:
            edited_sources.append(src)
            continue
        new_text, applied = apply_edits(str(src.get("content") or ""), args.edit, S)
        edited = dict(src)
        edited["content"] = new_text
        edited_sources.append(edited)
        edit_log[src.get("url")] = {"applied": applied, "chars_before": len(str(src.get("content") or "")),
                                    "chars_after": len(new_text)}

    source_mutations = {}
    for before, after in zip(sources, edited_sources):
        url = before.get("url")
        before_text = str(before.get("content") or "")
        after_text = str(after.get("content") or "")
        ranges = []
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
                a=before_text, b=after_text, autojunk=False).get_opcodes():
            if tag == "equal":
                continue
            removed = before_text[i1:i2]
            ranges.append({
                "operation": tag,
                "original_start": i1,
                "original_end": i2,
                "edited_start": j1,
                "edited_end": j2,
                "removed_chars": len(removed),
                "replacement_chars": j2 - j1,
                "removed_sha256": hashlib.sha256(removed.encode("utf-8")).hexdigest(),
                "removed_preview": removed[:160],
            })
        source_mutations[url] = {
            "original_sha256": hashlib.sha256(before_text.encode("utf-8")).hexdigest(),
            "edited_sha256": hashlib.sha256(after_text.encode("utf-8")).hexdigest(),
            "original_chars": len(before_text),
            "edited_chars": len(after_text),
            "changed_ranges": ranges,
        }

    # 1) 结构检测与入证门重新跑（不是改 available_blocks）
    before_packets = R.source_packets([{**s} for s in sources], limit=None)
    after_packets = R.source_packets([{**s} for s in edited_sources], limit=None)
    before_blocks = {p["url"]: sorted(p["available_blocks"]) for p in before_packets}
    after_blocks = {p["url"]: sorted(p["available_blocks"]) for p in after_packets}

    # 2) 逐条事实/证据边重算定位：**比较的是「删前重放」与「删后重放」**，
    #    而不是「冻结记录 vs 删后」——后者会把重新装配摘录带来的漂移也算成删除效果。
    after_by_url = {p["url"]: p for p in after_packets}
    before_by_url = {p["url"]: p for p in before_packets}

    def _locatable(packets_by_url: dict, url: str | None, needle: str) -> bool:
        packet = packets_by_url.get(url)
        return bool(needle) and needle in str(packet["text"]) if packet else False

    quote_changes, condition_changes = [], []
    for f in facts:
        quote = str(f.get("source_quote") or "")
        if not quote:
            continue
        if _locatable(before_by_url, f.get("source_url"), quote) and not _locatable(after_by_url, f.get("source_url"), quote):
            quote_changes.append({"fact_id": f.get("id"), "url": f.get("source_url"),
                                  "quote": quote[:80], "was": "locatable", "now": "not_locatable_after_edit"})
    for edge in edges:
        fact = next((f for f in facts if f.get("id") == edge.get("fact_id")), None)
        if not fact:
            continue
        for field, ev in (edge.get("condition_evidence") or {}).items():
            if not isinstance(ev, dict):
                continue
            quote = str(ev.get("quote") or "")
            if not quote:
                continue
            if _locatable(before_by_url, fact.get("source_url"), quote) and not _locatable(after_by_url, fact.get("source_url"), quote):
                condition_changes.append({"edge_id": edge.get("id"), "field": field,
                                          "quote": quote[:80], "was": "locatable",
                                          "now": "not_locatable_after_edit"})
    table_before = sum(1 for p in before_packets if "table" in (p.get("available_blocks") or []))
    table_after = sum(1 for p in after_packets if "table" in (p.get("available_blocks") or []))
    protocol_before = sum(1 for p in before_packets if "annotation_protocol" in (p.get("available_blocks") or []))
    protocol_after = sum(1 for p in after_packets if "annotation_protocol" in (p.get("available_blocks") or []))
    guarantee_changes = {
        "table_block_materials": {"before": table_before, "after": table_after},
        "annotation_protocol_materials": {"before": protocol_before, "after": protocol_after},
        "quotes_lost": len(quote_changes),
        "condition_values_lost": len(condition_changes),
    }

    # 3) 走一遍入证 + 归约 + 门禁：**同一批抽取声明**，材料分别用原件与删后文本
    gate = {
        "baseline_material": _replay_gate(R, S, args.direction, sources, facts, edges, claims),
        "edited_material": _replay_gate(R, S, args.direction, edited_sources, facts, edges, claims),
    }

    selected_urls = [args.source_url] if args.source_url else [s.get("url") for s in sources]
    selected_edits = [item for url in selected_urls for item in edit_log.get(url, {}).get("applied", [])]
    lost_blocks = {block: any(block in values for values in
                               ({url: sorted(set(before_blocks.get(url, [])) - set(after_blocks.get(url, [])))
                                 for url in before_blocks}).values())
                   for block in args.expect_block_lost}
    text_expectations = {}
    for target in args.expect_text_lost:
        before_has = any(target.lower() in str(s.get("content") or "").lower()
                         for s in sources if s.get("url") in selected_urls)
        after_has = any(target.lower() in str(s.get("content") or "").lower()
                        for s in edited_sources if s.get("url") in selected_urls)
        text_expectations[target] = {"present_before": before_has, "absent_after": not after_has}
    controlled_condition_binding = {}
    for spec in args.expect_condition_text:
        field, sep, literal = spec.partition("=")
        if not sep or not field or not literal:
            raise ValueError(f"invalid --expect-condition-text (expected FIELD=LITERAL): {spec}")
        before_binding = _replay_controlled_condition_binding(
            R, args.direction, sources, field, literal, selected_urls)
        after_binding = _replay_controlled_condition_binding(
            R, args.direction, edited_sources, field, literal, selected_urls)
        controlled_condition_binding[field] = {
            "literal": literal,
            "fixture_origin": "controlled_extraction_fixture_not_historical_llm_output",
            "fixture_note": ("同一条明确声明通过生产 ingest_analysis 的 condition_quotes 绑定；"
                             "仅正文从冻结原件变为受控删段副本"),
            "claim_type_id": before_binding.get("claim_type_id"),
            "bound_before": before_binding.get("quote_bound") is True,
            "before_condition_evidence": before_binding.get("condition_evidence"),
            "bound_after": after_binding.get("quote_bound") is True,
            "after_condition_evidence": after_binding.get("condition_evidence"),
            "loss_reason": ("production_ingest_condition_quote_unbound_after_text_mutation"
                            if before_binding.get("quote_bound") is True
                            and after_binding.get("quote_bound") is False else None),
        }

    strong = {"supported", "supported_in_scope", "refuted", "refuted_in_scope"}
    before_status = {h.get("id"): h.get("status") for h in gate["baseline_material"]["hypotheses"]}
    after_status = {h.get("id"): h.get("status") for h in gate["edited_material"]["hypotheses"]}
    erroneous_upgrades = [claim_id for claim_id, status in after_status.items()
                          if status in strong and before_status.get(claim_id) not in strong]
    assertions = {
        "selected_source_exists": all(url in edit_log for url in selected_urls),
        "every_requested_edit_hit_selected_source": bool(selected_edits) and all(x.get("applied") for x in selected_edits),
        "original_and_edited_hashes_differ": all(
            source_mutations[url]["original_sha256"] != source_mutations[url]["edited_sha256"]
            for url in selected_urls if url in source_mutations),
        "deletion_ranges_recorded": all(source_mutations[url]["changed_ranges"]
                                        for url in selected_urls if url in source_mutations),
        "expected_blocks_lost": all(lost_blocks.values()),
        "expected_text_lost": all(v["present_before"] and v["absent_after"]
                                  for v in text_expectations.values()),
        "expected_quote_binding_lost": (not args.expect_quote_lost) or bool(quote_changes),
        "expected_condition_value_lost": (not args.expect_condition_lost) or bool(condition_changes),
        "expected_controlled_condition_binding_lost": all(
            item["bound_before"] and not item["bound_after"] and item["loss_reason"]
            for item in controlled_condition_binding.values()),
        "downstream_gate_recomputed": bool(gate),
        "no_erroneous_claim_upgrade": not erroneous_upgrades,
        "gate_not_upgraded": not (gate["edited_material"]["gate"]["ok"]
                                    and not gate["baseline_material"]["gate"]["ok"]),
    }
    result = {
        "case_id": args.case_id,
        "injection": True,
        "note": "注入负例：删除后的文本不是真实论文原貌，只用于检验规则是否真的连接到材料",
        "replay_scope_note": ("这是**受控对比**，不是重跑整条流水线：抽取声明沿用冻结运行，"
                              "结构检测、入证绑定、条件定位、审查记录与门禁由生产函数重算；"
                              "因此「删前 → 删后」的差异可以归因到文本编辑本身。"),
        "edits": args.edit,
        "edit_log": edit_log,
        "source_mutations": source_mutations,
        "blocks_before": before_blocks,
        "blocks_after": after_blocks,
        "blocks_lost": {url: sorted(set(before_blocks.get(url, [])) - set(after_blocks.get(url, [])))
                        for url in before_blocks},
        "guarantee_changes": guarantee_changes,
        "quote_binding_lost": quote_changes,
        "condition_values_lost": condition_changes,
        "claims_before": [{"claim_id": c.get("claim_id") or c.get("id"), "status": c.get("status"),
                           "assessment": (c.get("assessment") or {}).get("assessment_status")} for c in claims],
        "gate_recompute": gate,
        "expected": {
            "blocks_lost": args.expect_block_lost,
            "text_lost": args.expect_text_lost,
            "quote_binding_lost": args.expect_quote_lost,
            "condition_value_lost": args.expect_condition_lost,
            "condition_text": args.expect_condition_text,
        },
        "expectation_results": {"blocks_lost": lost_blocks, "text_lost": text_expectations},
        "controlled_condition_binding": controlled_condition_binding,
        "erroneous_claim_upgrades": erroneous_upgrades,
        "assertions": assertions,
        "analysis_rebuild_note": ("删后重放使用与冻结运行相同的事实/证据边声明；入证、绑定、条件定位、"
                                  "审查记录与门禁由生产函数重算，因此可以直接观察「改文本」的影响"),
    }
    (out / "replay_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                            encoding="utf-8", newline="\n")
    (out / "original_sources.jsonl").write_text(
        "\n".join(json.dumps(s, ensure_ascii=False) for s in sources) + "\n",
        encoding="utf-8", newline="\n")
    (out / "edited_sources.jsonl").write_text(
        "\n".join(json.dumps(s, ensure_ascii=False) for s in edited_sources) + "\n",
        encoding="utf-8", newline="\n")
    print(json.dumps({k: result[k] for k in ("edits", "blocks_lost", "quote_binding_lost",
                                             "condition_values_lost")}, ensure_ascii=False, indent=2))
    result["passed"] = all(assertions.values())
    # passed 要进入已写出的结果，而不是只出现在控制台。
    (out / "replay_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                            encoding="utf-8", newline="\n")
    print("目标删除与下游门禁断言：" + ("PASS" if result["passed"] else "FAIL"))
    return 0 if result["passed"] else 1


def _replay_gate(R, S, direction_id: str, sources: list[dict], facts: list[dict],
                 edges: list[dict], claims: list[dict]) -> dict:
    """用给定材料重跑「入证绑定 → 审查记录 → 论断状态 → 质量门」（全部生产函数）。

    把冻结运行记录的事实/证据边当成**抽取声明**，材料换成删后文本：
    `ingest_analysis` 会按删后文本重新判定引用绑定与条件定位，`reduce_hypotheses` 与
    `quality_verdict` 据此重算结论。这样「改文本 → 保证变化」就是可观察的因果链，
    而不是改一个字段名。
    """
    from service.direction_contracts import load_preset
    profile = load_preset(direction_id).model_dump()
    section_ids = sorted({s for f in facts for s in (f.get("related_sections") or [])}) or ["sec1"]
    state = {
        "facts": [], "evidence_links": [],
        "hypotheses": [{"id": c.get("id"), "content": c.get("content"), "scope_id": c.get("scope_id"),
                        "conditions": c.get("conditions") or {}, "claim_type_id": c.get("claim_type_id"),
                        "status": "pending"} for c in claims],
        "raw_sources": [], "evidence_audit": [], "date_deferred_sources": [], "provenance_rejections": [],
        "completed_stages": [], "next_stage": "write", "run_status": "completed",
        "quality_status": "needs_review", "research_config": {"quality_threshold": 6},
        "direction_request": {"start_year": 2022, "end_year": 2025},
        "resolved_direction": {"profile": profile, "global_policy": {}, "rule_origin": "preset",
                               "field_origins": {}, "profile_hash": "replay", "prompt_version": "replay",
                               "input_hash": "replay",
                               "validation_status": "contract_valid_not_expert_validated"},
        "final_report": "replay report", "report_hash": R.report_hash("replay report"),
        "reviewed_report_hash": R.report_hash("replay report"), "review_complete": True,
        "review_verdict": "pass", "quality_score": 9, "unresolved_issues": 0,
        "iteration": 0, "max_iterations": 3,
        "evidence_semantics_version": R.EVIDENCE_SEMANTICS_VERSION,
        "schema_version": R.STATE_VERSION, "pipeline_version": R.PIPELINE_VERSION,
    }
    analysis = {"extracted_facts": [{
        "source_id": e.get("source_id") or next((f.get("source_id") for f in facts
                                                 if f.get("id") == e.get("fact_id")), None),
        "quote": next((f.get("source_quote") for f in facts if f.get("id") == e.get("fact_id")), ""),
        "content": next((f.get("content") for f in facts if f.get("id") == e.get("fact_id")), ""),
        "related_hypothesis": e.get("hypothesis_id"), "hypothesis_support": e.get("stance"),
        "scope_id": e.get("scope_id"),
        "conditions": e.get("reported_conditions") or e.get("conditions") or {},
        "condition_quotes": {k: (v or {}).get("quote") for k, v in (e.get("condition_evidence") or {}).items()
                             if isinstance(v, dict)},
    } for e in edges]}
    R.ingest_analysis(state, analysis, sources, section_ids)
    R.reduce_hypotheses(state)
    verdict = R.quality_verdict(state)
    return {
        "direction": direction_id,
        "hypotheses": [{"id": h.get("id"), "status": h.get("status"),
                        "assessment_status": (h.get("assessment") or {}).get("assessment_status"),
                        "reason_codes": (h.get("assessment") or {}).get("reason_codes"),
                        "limitations": h.get("limitations")} for h in state["hypotheses"]],
        "bound_facts": sum(1 for f in state["facts"] if f.get("provenance_status") == "quote_bound"),
        "unbound_facts": sum(1 for f in state["facts"] if f.get("provenance_status") != "quote_bound"),
        "edges": len(state["evidence_links"]),
        "gate": {"ok": verdict["ok"], "reasons": verdict["reasons"]},
    }


def _replay_controlled_condition_binding(R, direction_id: str, sources: list[dict],
                                         field: str, literal: str,
                                         selected_urls: list[str]) -> dict:
    """用生产 ingest_analysis 绑定一条明确标注的受控条件声明。

    该声明不是历史模型输出；它只固定 `conditions[field]` 与逐字 `condition_quotes[field]`，
    以验证同一生产绑定函数在冻结原文和删段副本上的 before/after 行为。
    """
    from service.direction_contracts import load_preset

    profile = load_preset(direction_id).model_dump()
    rule = next((item for item in profile["claim_types"]
                 if field in item.get("required_conditions", [])), None)
    if rule is None:
        raise ValueError(f"{field!r} is not a required condition in direction {direction_id!r}")
    selected = [item for item in sources if item.get("url") in selected_urls]
    if len(selected) != 1:
        raise ValueError(f"controlled condition fixture needs exactly one selected source, got {len(selected)}")
    hypothesis_id = f"controlled_condition_{field}"
    state = {
        "facts": [], "evidence_links": [],
        "hypotheses": [{"id": hypothesis_id, "content": f"Controlled fixture for {field}",
                         "scope_id": "controlled_scope", "conditions": {field: literal},
                         "claim_type_id": rule["id"], "status": "pending"}],
        "raw_sources": [], "evidence_audit": [], "date_deferred_sources": [],
        "provenance_rejections": [], "claim_assessments": [],
        "direction_request": {"start_year": 2022, "end_year": 2025},
        "resolved_direction": {"profile": profile, "global_policy": {}, "rule_origin": "preset",
                               "field_origins": {}, "profile_hash": "controlled-condition-replay",
                               "prompt_version": "controlled-condition-replay", "input_hash": "controlled",
                               "validation_status": "contract_valid_not_expert_validated"},
        "research_config": {"quality_threshold": 6},
    }
    source = selected[0]
    analysis = {"extracted_facts": [{
        "source_id": source.get("source_id"),
        "source_url": source.get("url"),
        "quote": literal,
        "content": f"Controlled extraction fixture for {field}",
        "related_hypothesis": hypothesis_id,
        "hypothesis_support": "inconclusive",
        "scope_id": "controlled_scope",
        "conditions": {field: literal},
        "condition_quotes": {field: literal},
        "evidence_role": "controlled_fixture",
    }]}
    R.ingest_analysis(state, analysis, selected, ["controlled_section"])
    if len(state["evidence_links"]) != 1:
        raise AssertionError(f"controlled condition fixture produced {len(state['evidence_links'])} edges")
    edge = state["evidence_links"][0]
    condition_evidence = (edge.get("condition_evidence") or {}).get(field) or {}
    return {
        "claim_type_id": rule["id"],
        "quote_bound": condition_evidence.get("quote_bound") is True,
        "condition_evidence": condition_evidence,
        "fact_quote_bound": edge.get("quote_bound") is True,
        "conditions_verified": edge.get("conditions_verified") is True,
    }


def _replay_state(R, sources: list[dict]) -> dict:
    """构造归约器需要的最小状态（材料来自冻结副本，不联网、不调用模型）。"""
    return {"facts": [], "evidence_links": [], "hypotheses": [], "raw_sources": list(sources),
            "evidence_audit": [], "date_deferred_sources": [], "provenance_rejections": [],
            "completed_stages": [], "next_stage": "write", "run_status": "completed",
            "research_config": {"quality_threshold": 6}, "direction_request": {}}


if __name__ == "__main__":
    raise SystemExit(main())
