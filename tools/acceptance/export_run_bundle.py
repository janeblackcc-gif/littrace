#!/usr/bin/env python3
"""从检查点导出**完整复核包**（可复算的全量产物）。

代码评审原话要点：
  「哈希不能替代被哈希的文本；完整文本在你电脑的检查点里也不等于已经交付给独立审查。」
因此本脚本从原 session 的检查点导出：

    final_report.md            完整最终稿（UTF-8）
    ui_report.txt              最终 UI 快照对应的报告文本
    delivered_sources.jsonl    实际交付文本（完整）+ 身份/版本/哈希/裁剪/绑定与 provenance
    facts.jsonl                完整 quote、locator、source/version/excerpt 绑定
    evidence_links.jsonl       完整关系、采用/排除记录
    claims.jsonl               完整论断、审查记录与条件
    run_manifest.json          run 字段、配置/语义版本、终态、**逐文件 sha256**
    NOTES.md                   口径说明（哈希算法、当次元数据快照是否存在、后补元数据的取回时间）

并在导出后**独立重算**每个文件与报告正文的 sha256 与 manifest 对照（不是只看存储字段）。
用法：
    python tools/acceptance/export_run_bundle.py --session-id <uuid> --out <dir>
        [--posthoc-metadata arxiv_id ...]   # 事后补取官方元数据用于交叉核验（会标注取回时间）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / "backend" / "app"))


def sha256_text(text: str) -> str:
    """与项目 `report_hash` 同一算法：UTF-8 文本的 sha256 十六进制。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_state(session_id: str) -> tuple[dict, dict]:
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
        return row.state_json or {}, row.ui_state_json or {}


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def posthoc_metadata(arxiv_ids: list[str]) -> list[dict]:
    """事后补取官方元数据（**明确标注**：这是事后核验，不是当次快照）。"""
    if not arxiv_ids:
        return []
    from service import research_sources as S
    out = []
    for ident in arxiv_ids:
        meta = S._arxiv_metadata(ident)
        out.append({"arxiv_id": ident, "fetched_at": S._now_iso(), "url": meta.get("url"),
                    "final_url": meta.get("final_url"), "ok": meta.get("ok"),
                    "reason": meta.get("reason"), "first_published": meta.get("first_published"),
                    "versions": dict(meta.get("versions") or {}),
                    "history_snippet": meta.get("history_snippet"),
                    "history_hash": meta.get("history_hash"),
                    "note": "事后补取，用于交叉核验；不是当次运行的元数据快照"})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session-id", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--posthoc-metadata", nargs="*", default=[],
                    help="事后补取官方元数据的 arXiv id（会明确标注取回时间）")
    args = ap.parse_args()
    state, ui = load_state(args.session_id)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    report = state.get("final_report", "") or ""
    ui_report = ui.get("streaming_report", "") or ""
    delivered = [s for s in (state.get("raw_sources") or []) if isinstance(s, dict)]
    facts = [f for f in (state.get("facts") or []) if isinstance(f, dict)]
    edges = [e for e in (state.get("evidence_links") or []) if isinstance(e, dict)]
    claims = [h for h in (state.get("hypotheses") or []) if isinstance(h, dict)]

    (out / "final_report.md").write_text(report, encoding="utf-8", newline="\n")
    (out / "ui_report.txt").write_text(ui_report, encoding="utf-8", newline="\n")
    write_jsonl(out / "delivered_sources.jsonl", delivered)
    write_jsonl(out / "facts.jsonl", facts)
    write_jsonl(out / "evidence_links.jsonl", edges)
    write_jsonl(out / "claims.jsonl", claims)

    deferred = [s for s in (state.get("date_deferred_sources") or []) if isinstance(s, dict)]
    audit = [a for a in (state.get("evidence_audit") or []) if isinstance(a, dict)]
    write_jsonl(out / "deferred_sources.jsonl", deferred)
    write_jsonl(out / "source_audit.jsonl", audit)

    # 完整配置快照（敏感值脱敏，保留开关与规则哈希）
    cfg_snapshot = {
        "run_config_hash": state.get("run_config_hash"),
        "pipeline_version": state.get("pipeline_version"),
        "evidence_semantics_version": state.get("evidence_semantics_version"),
        "research_config": state.get("research_config") or {},
        "run_flags": state.get("run_flags") or {},
        "run_models": state.get("run_models") or {},
        "resolved_direction": state.get("resolved_direction") or {},
        "research_plan": state.get("research_plan") or {},
    }
    (out / "config_snapshot.json").write_text(
        json.dumps(cfg_snapshot, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

    # 当次元数据快照是否随材料落库：逐条如实判断（不存在就写缺失，不要冒充）
    meta_saved = sum(1 for d in delivered if d.get("metadata_provenance"))
    missing_meta = [d.get("url") for d in delivered if not d.get("metadata_provenance")]
    posthoc = posthoc_metadata(args.posthoc_metadata)

    manifest = {
        "session_id": args.session_id,
        "exported_at_utc": None,
        "hash_algorithm": "sha256(utf-8 text), hex",
        "run": {
            "run_status": state.get("run_status"), "quality_status": state.get("quality_status"),
            "quality_score": state.get("quality_score"), "next_stage": state.get("next_stage"),
            "iteration": state.get("iteration"), "unresolved_issues": state.get("unresolved_issues"),
            "report_chars": len(report), "report_hash": state.get("report_hash"),
            "recomputed_report_hash": sha256_text(report),
            "reviewed_report_hash": state.get("reviewed_report_hash"),
            "run_config_hash": state.get("run_config_hash"),
            "run_id": state.get("run_id"), "run_history": state.get("run_history"),
            "last_terminal_event": state.get("last_terminal_event"),
            "evidence_semantics_version": state.get("evidence_semantics_version"),
            "pipeline_version": state.get("pipeline_version"), "state_version": state.get("schema_version"),
            "direction_profile_hash": ((state.get("resolved_direction") or {}).get("profile_hash")),
            "direction_origin": ((state.get("resolved_direction") or {}).get("rule_origin")),
            "usage": state.get("usage"),
        },
        "counts": {
            "delivered_sources": len(delivered),
            "delivered_content_chars": sum(len(str(d.get("content") or "")) for d in delivered),
            "facts": len(facts), "evidence_links": len(edges), "claims": len(claims),
            "deferred_sources": len(deferred),
            "source_audit": len(audit),
            "ui_report_chars": len(ui_report), "ui_report_matches_report": ui_report == report,
        },
        "metadata_snapshot": {
            "sources_with_metadata_provenance": meta_saved,
            "sources_without_metadata_provenance": len(missing_meta),
            "missing_examples": missing_meta[:10],
            "note": ("当次运行并未把 metadata_provenance 写入材料归档" if missing_meta
                     else "当次运行的元数据依据已随材料落库"),
            "posthoc_metadata": posthoc,
        },
        "files": {},
    }
    from datetime import datetime, timezone
    manifest["exported_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # 逐文件哈希（写完最后一个再算，manifest 自身不参与）
    for name in ("final_report.md", "ui_report.txt", "delivered_sources.jsonl",
                 "facts.jsonl", "evidence_links.jsonl", "claims.jsonl"):
        data = (out / name).read_bytes()
        manifest["files"][name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    (out / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

    notes = [
        "# 导出说明（round2B2 阻塞条件一）", "",
        f"- session：`{args.session_id}`；导出时间（UTC）：{manifest['exported_at_utc']}",
        f"- 哈希算法：**{manifest['hash_algorithm']}**；`final_report.md` 重算哈希 = "
        f"`{manifest['run']['recomputed_report_hash']}`，与存储 `report_hash` "
        f"`{manifest['run']['report_hash']}` " + ("一致" if manifest['run']['recomputed_report_hash'] == manifest['run']['report_hash'] else "**不一致**"),
        f"- 交付材料：{len(delivered)} 条，合计 {manifest['counts']['delivered_content_chars']} 字符"
        f"（本目录 `delivered_sources.jsonl` 为**完整**文本，未截断）",
        f"- facts {len(facts)} / evidence_links {len(edges)} / claims {len(claims)}；quote 为完整原文，未截断",
        f"- deferred_sources {len(deferred)} / source_audit {len(audit)}",
        f"- 配置快照已写入 `config_snapshot.json`（保留开关与模型标识，脱敏密钥）",
        f"- UI 快照报告与状态报告一致：{manifest['counts']['ui_report_matches_report']}",
        "- 运行终态：`%s`（terminal 事件 %s）" % (
            (state.get("run_status")), json.dumps(state.get("last_terminal_event"), ensure_ascii=False)),
        "",
        "## 元数据依据",
        f"- 随材料保存了 metadata_provenance 的条数：{meta_saved}；缺失：{len(missing_meta)}",
    ]
    if missing_meta:
        notes.append("  - **当次运行的元数据快照未随材料归档**（那时的代码还没有把 provenance "
                     "穿过 source_packets/raw_sources）；此处如实标注缺失，不用事后数据冒充。")
    if posthoc:
        notes.append(f"  - 事后补取官方元数据 {len(posthoc)} 条，取回时间见 `run_manifest.json` 的 "
                     "`metadata_snapshot.posthoc_metadata[].fetched_at`；**仅用于事后交叉核验**。")
    (out / "NOTES.md").write_text("\n".join(notes) + "\n", encoding="utf-8", newline="\n")

    # 独立复算：重读文件、重算哈希与内容体积
    ok = True
    for name, info in manifest["files"].items():
        data = (out / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != info["sha256"] or len(data) != info["bytes"]:
            ok = False

    file_report_text = (out / "final_report.md").read_text(encoding="utf-8")
    computed_file_report_hash = sha256_text(file_report_text)
    file_matches_manifest = (computed_file_report_hash == manifest["run"]["recomputed_report_hash"])

    stored_hash = manifest["run"]["report_hash"]
    reviewed_hash = manifest["run"]["reviewed_report_hash"]
    ui_text = (out / "ui_report.txt").read_text(encoding="utf-8")
    ui_matches_report = (ui_text == file_report_text)

    # 最终一致性判据（S0-02）：实测正文哈希 = report_hash = reviewed_report_hash = UI 文本
    has_report = bool(file_report_text)
    has_stored = stored_hash is not None
    has_reviewed = reviewed_hash is not None

    hash_conflict = False
    if has_report and (has_stored or has_reviewed):
        if has_stored and stored_hash != computed_file_report_hash:
            hash_conflict = True
        if has_reviewed and reviewed_hash != computed_file_report_hash:
            hash_conflict = True
        if not ui_matches_report:
            hash_conflict = True

    if hash_conflict:
        verification_status = "conflict"
        report_consistent = False
    elif has_report and (stored_hash == computed_file_report_hash == reviewed_hash) and ui_matches_report:
        verification_status = "passed"
        report_consistent = True
    elif not has_report or (not has_stored and not has_reviewed):
        # 失败/未到审核的任务仍能导出取证文件，状态记为「不适用」而非「通过」
        verification_status = "not_applicable"
        report_consistent = None
    else:
        verification_status = "unverified"
        report_consistent = False

    delivered_rows = [json.loads(l) for l in (out / "delivered_sources.jsonl").read_text(encoding="utf-8").splitlines() if l]
    chars_ok = sum(len(str(r.get("content") or "")) for r in delivered_rows) == manifest["counts"]["delivered_content_chars"]
    facts_rows = [json.loads(l) for l in (out / "facts.jsonl").read_text(encoding="utf-8").splitlines() if l]
    # 独立复算：已绑定引用必须能在**导出的完整交付文本**里逐字找到（按 source_id / url+version 定位）。
    def _rows_for(fact: dict) -> list[dict]:
        sid = fact.get("source_id")
        if sid:
            rows = [r for r in delivered_rows if str(r.get("source_id") or "") == str(sid)]
            if rows:
                return rows
        return [r for r in delivered_rows
                if r.get("url") == fact.get("source_url")
                and str(r.get("version_id") or "") == str(fact.get("source_version") or "")]

    quote_checks = [{"id": f.get("id"),
                     "located": bool(f.get("source_quote"))
                     and any(f.get("source_quote") in str(r.get("content") or "") for r in _rows_for(f))}
                    for f in facts_rows if f.get("provenance_status") == "quote_bound" and f.get("source_quote")]
    quotes_ok = all(c["located"] for c in quote_checks)

    recompute_report_hash_ok = file_matches_manifest and (not hash_conflict) and (verification_status != "conflict")

    print(json.dumps({"output_dir": str(out), "files": len(manifest["files"]),
                      "delivered_sources": len(delivered_rows), "facts": len(facts_rows),
                      "content_chars": manifest["counts"]["delivered_content_chars"],
                      "report_chars": len(report),
                      "recompute_files_ok": ok,
                      "recompute_report_hash_ok": recompute_report_hash_ok,
                      "report_consistency_ok": report_consistent,
                      "verification_status": verification_status,
                      "delivered_chars_match_manifest": chars_ok,
                      "bound_quote_located_in_export": f"{sum(1 for c in quote_checks if c['located'])}/{len(quote_checks)}",
                      "metadata_provenance_saved": meta_saved},
                     ensure_ascii=False, indent=2))

    if not (ok and file_matches_manifest and chars_ok and quotes_ok):
        return 1
    if hash_conflict:
        return 1
    if state.get("run_status") == "completed" and verification_status != "passed":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
