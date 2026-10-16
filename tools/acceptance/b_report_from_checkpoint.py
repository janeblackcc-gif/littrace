#!/usr/bin/env python3
"""B. 从检查点导出「状态 × 材料原因」一致性表与失败样例（供代码评审复核真实联调）。

真实联调的通过标准：
  「不是原生 fetch 代替 UI；可以 needs_review，但**状态与材料原因必须一致**」。
本脚本读**真实运行**落库的检查点，逐论断核对：
  - 结论状态（supported/refuted/evidence_limited/insufficient…）
  - 支撑它的材料获取状态（fetched / fetch_failed / …）与实际交付范围
  - 审查记录的理由码（技术受阻 vs 读后不足）
  - 采用/排除边及理由、子命题与候选命题
并给出：被拒/未知日期材料、条件可比性分布、交付材料快照哈希、报告/审核哈希。

用法：
    python tools/acceptance/b_report_from_checkpoint.py --session-id <uuid> --out <json>
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / "backend" / "app"))


def load_state(session_id: str) -> dict:
    from dotenv import load_dotenv
    load_dotenv(BASE / ".env")
    from core.runtime_settings import sanitize_no_proxy
    sanitize_no_proxy()
    from core.database import SessionLocal
    from models import ResearchCheckpoint
    with SessionLocal() as db:
        row = (db.query(ResearchCheckpoint)
               .filter(ResearchCheckpoint.session_id == session_id).first())
        if row is None:
            raise SystemExit(f"没有找到 session_id={session_id} 的检查点")
        return row.state_json or {}, (row.ui_state_json or {})


# 自称 fetched 的材料里，只有真正拿到正文的范围才谈得上「读过材料」——
# 与 `research_integrity.build_claim_assessment` 的判定保持一致，且**独立复核**它。
FULLTEXT_SCOPES = {"fulltext_page", "pdf"}


def fulltext_reviewed(entry: dict) -> bool:
    """该材料是否算「正文已取得、被读过」。"""
    status = entry.get("material_status")
    scope = entry.get("material_scope")
    return status == "fetched" and scope in FULLTEXT_SCOPES


def classify_adopted_evidence(hypothesis: dict) -> tuple[str, list[str], str]:
    """解析 adopted_evidence，明确区分四态（S0-01 合同要求）：
      - 'official_non_empty': 正式 evidence_link_ids 存在且为非空 list
      - 'official_empty': 正式 evidence_link_ids 存在且显式为空 list
      - 'official_type_error': 正式 evidence_link_ids 存在但类型不是 list，或包含非字符串/空元素
      - 'legacy': 正式字段缺失，但包含旧版边对象列表（如 {"supports": [{"edge_id": ...}]}）
      - 'missing': 正式字段缺失且无旧版边对象列表
    返回: (state_kind, ids, source_label)
    """
    adopted = hypothesis.get("adopted_evidence")
    if isinstance(adopted, dict) and "evidence_link_ids" in adopted:
        val = adopted["evidence_link_ids"]
        if not isinstance(val, list):
            return "official_type_error", [], "malformed_evidence_link_ids"
        if len(val) == 0:
            return "official_empty", [], "evidence_link_ids"
        clean_ids = [str(v) for v in val if isinstance(v, str) and v]
        if len(clean_ids) < len(val):
            return "official_type_error", clean_ids, "malformed_evidence_link_ids"
        return "official_non_empty", clean_ids, "evidence_link_ids"

    if isinstance(adopted, dict):
        legacy_ids = []
        for key, value in adopted.items():
            if key in {"excluded", "compatibility", "scope_fields", "comparison_fields"}:
                continue
            if not isinstance(value, list):
                continue
            for item in value:
                if isinstance(item, dict):
                    for field in ("edge_id", "evidence_link_id"):
                        v = item.get(field)
                        if isinstance(v, str) and v:
                            legacy_ids.append(v)
                            break
        if legacy_ids:
            return "legacy", legacy_ids, "legacy_adopted_evidence"

    return "missing", [], "unresolved_or_empty"


def adopted_edge_ids(hypothesis: dict) -> set[str]:
    """该论断**实际采用**的证据边 id。"""
    _, ids, _ = classify_adopted_evidence(hypothesis)
    return set(ids)


def _same_polarity_edges(hypothesis: dict, own_edges: list[dict]) -> list[dict]:
    """正式字段缺失时的保守近似：结论同向的关联边（支持类结论看 supports，反驳类看 refutes）。"""
    status = hypothesis.get("status")
    want = "supports" if status == "supported_in_scope" else "refutes" if status == "refuted_in_scope" else None
    return [e for e in own_edges if want and e.get("stance") == want]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session-id")
    ap.add_argument("--out")
    ap.add_argument("--selftest", action="store_true",
                    help="用合成状态自证不变式有牙（含一个必须失败的负例），不连数据库")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.session_id or not args.out:
        ap.error("--session-id 与 --out 为必填（或用 --selftest）")
    state, ui = load_state(args.session_id)
    payload = build_payload(state, ui, args.session_id)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: payload[k] for k in ("run", "totals", "distributions", "invariants")},
                     ensure_ascii=False, indent=2))
    print(f"\n明细已写入: {out}")
    return 0 if all(payload["invariants"].values()) else 1


def build_payload(state: dict, ui: dict, session_id: str) -> dict:
    sys.path.insert(0, str(BASE / "backend" / "app"))
    from service import research_integrity as R

    facts = state.get("facts", []) or []
    edges = state.get("evidence_links", []) or []
    hyps = state.get("hypotheses", []) or []
    by_fact = {f.get("id"): f for f in facts}
    by_hyp = {h.get("id"): h for h in hyps}

    material_status = Counter(str(e.get("material_status")) for e in edges)
    material_scope = Counter(str(e.get("material_scope")) for e in edges)
    verdicts = Counter(str((e.get("condition_compatibility") or {}).get("verdict")) for e in edges)
    stances = Counter(str(e.get("stance")) for e in edges)
    exclusions = Counter()
    for h in hyps:
        for item in ((h.get("adopted_evidence") or {}).get("excluded") or []):
            exclusions[str(item.get("reason"))] += 1

    consistency = []
    for h in hyps:
        hid = h.get("id")
        own_edges = [e for e in edges if e.get("hypothesis_id") == hid]
        assessment = h.get("assessment") or {}
        statuses = sorted({str(e.get("material_status")) for e in own_edges})
        scopes = sorted({str(e.get("material_scope")) for e in own_edges})
        conclusive = h.get("status") in {"supported_in_scope", "refuted_in_scope"}
        # 不变式：强结论不允许建立在「正文未取得」的材料上（含只抓到摘要页/短页）。
        # S0-01 合同要求：
        #   - 区分正式字段缺失/类型错误/显式空/非空四态
        #   - 正式字段存在时绝不回退 _same_polarity_edges
        #   - 强结论的空集合、任何悬空ID、引用别的claim的边均失败
        #   - 对每个 ID 核对归属与材料条件，不是“至少一个匹配”即通过
        kind, raw_ids, source_label = classify_adopted_evidence(h)
        official_ids = set(raw_ids)

        all_edge_map = {str(e.get("id")): e for e in edges if e.get("id")}
        own_edge_map = {str(e.get("id")): e for e in own_edges if e.get("id")}
        want_stance = "supports" if h.get("status") == "supported_in_scope" else "refutes" if h.get("status") == "refuted_in_scope" else None

        if kind in ("official_non_empty", "official_empty", "official_type_error"):
            if kind == "official_type_error":
                checked_edges = []
                unresolved = conclusive
            elif kind == "official_empty":
                checked_edges = []
                unresolved = conclusive
            else:  # official_non_empty
                has_dangling = any(eid not in all_edge_map for eid in official_ids)
                has_foreign = any(eid in all_edge_map and eid not in own_edge_map for eid in official_ids)
                checked_edges = [own_edge_map[eid] for eid in sorted(official_ids) if eid in own_edge_map]
                stance_mismatch = bool(want_stance and any(e.get("stance") != want_stance for e in checked_edges))
                if conclusive:
                    unresolved = has_dangling or has_foreign or stance_mismatch or (not checked_edges)
                else:
                    unresolved = has_dangling or has_foreign
        elif kind == "legacy":
            has_dangling = any(eid not in all_edge_map for eid in official_ids)
            has_foreign = any(eid in all_edge_map and eid not in own_edge_map for eid in official_ids)
            checked_edges = [own_edge_map[eid] for eid in sorted(official_ids) if eid in own_edge_map]
            stance_mismatch = bool(want_stance and any(e.get("stance") != want_stance for e in checked_edges))
            if conclusive:
                unresolved = has_dangling or has_foreign or stance_mismatch or (not checked_edges)
            else:
                unresolved = has_dangling or has_foreign
        else:  # missing
            if conclusive:
                checked_edges = _same_polarity_edges(h, own_edges)
                unresolved = not checked_edges
            else:
                checked_edges = own_edges
                unresolved = False

        invariant_ok = not (conclusive and (unresolved or any(not fulltext_reviewed(e) for e in checked_edges)))
        # 「读后不足」的科学判断必须有**真的读过正文**的材料支撑；`source_or_attempt_ids`
        # 指向的 fact 必须都是正文级材料，否则审查记录不成立（独立重算，不信任门禁）。
        examined_ids = list(assessment.get("source_or_attempt_ids") or [])
        examined = [by_fact.get(i) or {} for i in examined_ids]
        domain_reasons = sorted(set(assessment.get("reason_codes") or []) & R.DOMAIN_REASONS)
        assessment_supported = (assessment.get("assessment_status") == "completed" and bool(domain_reasons))
        absence_ok = (not assessment_supported) or (
            bool(examined) and all(fulltext_reviewed({"material_status": f.get("source_material_status"),
                                                      "material_scope": f.get("source_material_scope")})
                                   for f in examined))
        consistency.append({
            "claim_id": hid,
            "claim_text": (h.get("content") or h.get("statement") or h.get("question")),
            "status": h.get("status"),
            "disposition": R.claim_disposition(h),
            "decided": R.claim_is_decided(h, state),
            "limitations": h.get("limitations"),
            "material_statuses": statuses,
            "material_scopes": scopes,
            "edges": len(own_edges),
            "adopted_edge_ids": sorted(official_ids),
            "adopted_ids_source": source_label,
            "strong_conclusion_unresolved": bool(unresolved),
            "strong_conclusion_checked_edges": sorted(str(e.get("id")) for e in checked_edges),
            "quote_bound_edges": sum(1 for e in own_edges if e.get("quote_bound")),
            "assessment_status": assessment.get("assessment_status"),
            "assessment_reasons": assessment.get("reason_codes"),
            "assessment_domain_reasons": domain_reasons,
            "assessment_materials_reviewed": (assessment.get("scope_of_review") or {}).get("materials_reviewed"),
            "assessment_examined": [{"fact_id": i, "material_status": (by_fact.get(i) or {}).get("source_material_status"),
                                     "material_scope": (by_fact.get(i) or {}).get("source_material_scope")}
                                    for i in examined_ids],
            "child_claims": len(h.get("child_claims") or []),
            "candidate_propositions": len(h.get("candidate_propositions") or []),
            "strong_conclusion_on_unfetched_material": (conclusive and not invariant_ok),
            "absence_judgement_without_fulltext": (assessment_supported and not absence_ok),
        })

    delivered = state.get("raw_sources", []) or []
    report = state.get("final_report", "") or ""
    unusable_material = [{"url": d.get("url"), "material_status": d.get("material_status"),
                          "material_scope": d.get("material_scope")}
                         for d in delivered if not fulltext_reviewed(d)]
    # 报告哈希必须**重算正文**只比较两个存储字符串会把
    # 「正文被换掉、两个 hash 字段仍是旧值」判成一致。
    recomputed_hash = R.report_hash(report) if report else None
    stored_hash = state.get("report_hash")
    review_hash = state.get("reviewed_report_hash")
    # 逐条事实的原文定位：审阅者据此判断「引用是否真的绑在这份交付文本上」。
    # **按材料身份定位**，不能只按 URL 匹配第一份材料——
    # 同一 URL 可能有多个版本/多个交付片段。优先 source_id，其次 (url, version_id)，最后 (url, excerpt_hash)。
    def _delivered_rows_for(fact: dict) -> list[dict]:
        source_id = fact.get("source_id")
        if source_id:
            rows = [d for d in delivered if str(d.get("source_id") or "") == str(source_id)]
            if rows:
                return rows
        url = fact.get("source_url")
        version = str(fact.get("source_version") or "")
        rows = [d for d in delivered
                if d.get("url") == url and str(d.get("version_id") or "") == version]
        return rows

    fact_quotes = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        rows = _delivered_rows_for(f)
        quote = str(f.get("source_quote") or "")
        fact_quotes.append({
            "id": f.get("id"), "source_id": f.get("source_id"), "source_url": f.get("source_url"),
            "provenance_status": f.get("provenance_status"),
            "source_quote": quote,
            "matched_delivered_rows": len(rows),
            "quote_located_in_delivered_text": bool(quote) and any(
                quote in str(d.get("content") or "") for d in rows),
            "source_locator": f.get("source_locator"), "source_version": f.get("source_version"),
            "material_status": f.get("source_material_status"),
            "material_scope": f.get("source_material_scope"),
            "delivered_blocks": f.get("source_delivered_blocks"),
        })
    payload = {
        "session_id": session_id,
        "run": {
            "run_status": state.get("run_status"), "quality_status": state.get("quality_status"),
            "quality_score": state.get("quality_score"), "next_stage": state.get("next_stage"),
            "iteration": state.get("iteration"), "unresolved_issues": state.get("unresolved_issues"),
            "report_chars": len(report),
            "report_hash": stored_hash,
            "reviewed_report_hash": review_hash,
            "recomputed_report_hash": recomputed_hash,
            "hash_bound": bool(report) and stored_hash == review_hash == recomputed_hash,
            "run_config_hash": state.get("run_config_hash"),
            "evidence_semantics_version": state.get("evidence_semantics_version"),
            "direction_profile_hash": ((state.get("resolved_direction") or {}).get("profile_hash")),
            "direction_origin": ((state.get("resolved_direction") or {}).get("rule_origin")),
            "usage": state.get("usage"),
        },
        "totals": {
            "facts": len(facts), "evidence_links": len(edges), "hypotheses": len(hyps),
            "delivered_sources": len(delivered),
            "delivered_fulltext_sources": sum(1 for d in delivered if fulltext_reviewed(d)),
            "delivered_non_fulltext_sources": len(unusable_material),
            "deferred_sources": len(state.get("date_deferred_sources", []) or []),
            "deferred_reasons": dict(Counter(str(d.get("reason")) for d in
                                             (state.get("date_deferred_sources") or []) if isinstance(d, dict))),
            "evidence_audit": len(state.get("evidence_audit", []) or []),
        },
        "distributions": {
            "hypothesis_status": dict(Counter(str(h.get("status")) for h in hyps)),
            "edge_stance": dict(stances),
            "edge_material_status": dict(material_status),
            "edge_material_scope": dict(material_scope),
            "condition_compatibility": dict(verdicts),
            "exclusion_reasons": dict(exclusions),
        },
        "delivered_material": [{
            "source_id": d.get("source_id"),
            "url": d.get("url"), "version_id": d.get("version_id"),
            "content_hash": d.get("content_hash"), "chars": len(str(d.get("content") or "")),
            "available_blocks": d.get("available_blocks"), "material_status": d.get("material_status"),
            "material_scope": d.get("material_scope"), "material_reasons": d.get("material_reasons"),
            "fulltext_url": d.get("fulltext_url"),
            "excerpt_strategy": d.get("excerpt_strategy"), "excerpt_truncated": d.get("excerpt_truncated"),
            "publication_first_date": d.get("publication_first_date"),
            "version_date": d.get("version_date"), "peer_reviewed": d.get("peer_reviewed"),
            "usable_as_fulltext": fulltext_reviewed(d),
            # 绑定结论与两侧 provenance（必须随交付材料一路保存并导出）
            "identity_binding": d.get("identity_binding"),
            "version_selection": d.get("version_selection"),
            "version_resolution": d.get("version_resolution"),
            "version_date_correction": d.get("version_date_correction"),
            "metadata_provenance": d.get("metadata_provenance"),
            "content_provenance": d.get("content_provenance"),
            "date_source": d.get("date_source"),
            "version_date_source": d.get("version_date_source"),
            # **实际交付给抽取模型的完整文本**：不再截断（
            # 「哈希不能替代被哈希的文本」，独立审查要能复算全文哈希）。
            "content_head": str(d.get("content") or ""),
        } for d in delivered],
        # 逐条事实的原文定位：审阅者据此判断「引用是否真的绑在这份交付文本上」。
        "fact_quotes": fact_quotes,
        "non_fulltext_material": unusable_material,
        "rejected_or_deferred": {
            "deferred": state.get("date_deferred_sources", []) or [],
            "audit": state.get("evidence_audit", []) or [],
        },
        "claims": consistency,
        "invariants": {
            "no_strong_conclusion_on_unfetched_material":
                not any(c["strong_conclusion_on_unfetched_material"] for c in consistency),
            "no_absence_judgement_without_fulltext_material":
                not any(c["absence_judgement_without_fulltext"] for c in consistency),
            "every_claim_has_disposition": all(
                R.claim_disposition(h) in {"decided", "provisional"} for h in hyps),
            # 已绑定引用必须能在**实际交付文本**里逐字找到（可回查性）
            "every_bound_quote_located_in_delivered_text": all(
                q["quote_located_in_delivered_text"] for q in fact_quotes
                if q["provenance_status"] == "quote_bound" and q["source_quote"]),
            # 三个哈希（存储的报告哈希、审核哈希、**重算的正文哈希**）必须一致。
            "report_hash_bound_to_review": bool(report) and stored_hash == review_hash == recomputed_hash,
            # 断言名收窄这项只证明「UI 快照里的报告正文与状态一致」。
            "ui_snapshot_report_matches_state": (ui.get("streaming_report") or "") == report,
            # 状态一致性单独一条：UI 快照若带状态就必须与权威状态一致（缺失记为未携带）。
            "ui_snapshot_statuses_match_state": (
                (ui.get("quality_status") in (None, state.get("quality_status")))
                and (ui.get("run_status") in (None, state.get("run_status")))),
        },
        "ui_snapshot": {"quality_status": ui.get("quality_status"), "run_status": ui.get("run_status"),
                        "carries_status": ui.get("quality_status") is not None or ui.get("run_status") is not None,
                        "search_results": len(ui.get("search_results", []) or []),
                        "references": len(ui.get("references", []) or [])},
    }
    return payload


def _fixture(material_scope: str | None, assessment_status: str) -> dict:
    """合成状态：一条论断、一条正文级/摘要级材料。

    两个变体对应真实联调里同一段代码的两种结果（round2-B1）：
      - 旧行为：抓到 /abs/ 摘要页（material_status=fetched，scope=abstract_page）
        却产出「读后不足」的科学判断 → 不变式**必须**失败；
      - 修复后：同一材料只记技术受限（assessment_status=blocked）→ 不变式通过。

    哈希用**真实正文**算（`report_hash`）——比较器现在会重算正文哈希，
    填充假字符串的 fixture 会立刻暴露成不一致。
    """
    sys.path.insert(0, str(BASE / "backend" / "app"))
    from service import research_integrity as R

    report = "合成报告正文"
    digest = R.report_hash(report)
    return {
        "run_status": "completed", "quality_status": "needs_review",
        "final_report": report, "report_hash": digest, "reviewed_report_hash": digest,
        "evidence_semantics_version": R.EVIDENCE_SEMANTICS_VERSION,
        "facts": [{"id": "f1", "source_material_status": "fetched", "source_material_scope": material_scope}],
        "evidence_links": [{"id": "e1", "hypothesis_id": "H", "fact_id": "f1", "stance": "inconclusive",
                            "quote_bound": True, "material_status": "fetched", "material_scope": material_scope}],
        "hypotheses": [{"id": "H", "status": "insufficient_evidence",
                        "assessment": {"assessment_status": assessment_status, "reason_codes": ["required_block_missing"],
                                       "source_or_attempt_ids": ["f1"] if assessment_status == "completed" else [],
                                       "scope_of_review": {"materials_reviewed": 1 if assessment_status == "completed" else 0}}},
                       {"id": "H2", "status": "evidence_limited"}],
        "raw_sources": [{"url": "https://arxiv.org/abs/2501.12345v1", "content": "摘要",
                         "material_status": "fetched", "material_scope": material_scope}],
        "date_deferred_sources": [], "evidence_audit": [],
    }


def selftest() -> int:
    pos_fixture = _fixture("fulltext_page", "blocked")
    pos_fixture["hypotheses"][0].update(
        status="supported_in_scope",
        adopted_evidence={"evidence_link_ids": ["e1"]}
    )
    pos_fixture["evidence_links"][0].update(stance="supports")

    cases = [
        ("旧行为：摘要页 + 「读后不足」科学判断", _fixture("abstract_page", "completed"), False),
        ("修复后：摘要页只记技术受限", _fixture("abstract_page", "blocked"), True),
        ("正文页 + 「读后不足」科学判断", _fixture("fulltext_page", "completed"), True),
        ("S0-01 正例：正文页 + 强结论 + 正式采用集非空且合规", pos_fixture, True),
    ]
    ok_all = True
    for label, state, expected in cases:
        payload = build_payload(state, {"streaming_report": state["final_report"]}, "selftest")
        got = all(payload["invariants"].values())
        ok = got is expected
        ok_all &= ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {label} → 不变式全通过={got}（期望 {expected}）"
              f" 交付正文级材料={payload['totals']['delivered_fulltext_sources']}/"
              f"{payload['totals']['delivered_sources']}")
    print("B 复核脚本自证: " + ("PASS" if ok_all else "FAIL"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
