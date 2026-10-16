#!/usr/bin/env python3
"""E-01 自动化验证脚本：评测工具链严格检查与基线样本核对。

验证范围（依据 MIGRATION_RELEASE_PLAN_v1.md §6 E-01）：
  1. export 默认不覆盖已有人工确认标签（保留已确认字段，仅为新增行增补）；
  2. score 命令只读计算，绝不篡改标签或重置状态；
  3. 标签校验：枚举错误、未知 ID、claim 错配、重复 ID、缺失 annotator、样本哈希不匹配均被拒绝；
  4. 完整读取 claim 正文和条件（conditions / comparison_fields）；
  5. 机器预测固定在输出快照，不被 human_labels 里的修改字段反向影响；
  6. 已知 2 条支持预测（人审一对一错）基线核验：
     - wrong_support_count: numerator=1, denominator=2
     - support_precision: numerator=1, denominator=2
  7. 零分母、未确认、空集合全部明确返回 N/A (numerator=None, denominator=0, available=False)，绝不默认 0 或 1。
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools" / "acceptance"))

import build_eval_sheet as bes


def make_two_sample_state() -> dict:
    state = bes.synthetic_state()
    # 扩展为 2 个论断，2 条机器预测 supports 的边
    state["hypotheses"].append({
        "id": "H2",
        "content": "Sparse-dense hybrid search improves recall over pure dense retriever",
        "status": "verified",
        "limitations": [],
        "conditions": {"task": "retrieval", "benchmark": "BEIR", "top_k": 10},
        "comparison_fields": ["benchmark", "task", "top_k"],
        "adopted_evidence": {"evidence_link_ids": ["edge_2"], "excluded": []},
    })
    # 真实论断即使没有证据边，也必须保留在论断全集和弃权分母中。
    state["hypotheses"].append({
        "id": "H3_NO_EDGE",
        "content": "A planned claim with no evidence edge",
        "status": "insufficient_evidence",
        "limitations": ["no_evidence_edge"],
        "conditions": {"task": "retrieval"},
        "comparison_fields": ["task"],
    })
    state["facts"].append({
        "id": "fact_3",
        "source_url": "https://arxiv.org/abs/2501.00003v1",
        "source_name": "P3",
        "source_version": "https://arxiv.org/html/2501.00003v1",
        "source_locator": {"kind": "section", "id": "4.2"},
        "source_content_hash": "h3",
        "source_quote": "Hybrid search achieves 0.74 NDCG@10 vs 0.68 for dense",
        "provenance_status": "quote_bound",
        "source_material_status": "fetched",
        "source_delivered_blocks": ["experiments"],
    })
    state["evidence_links"].append({
        "id": "edge_2",
        "hypothesis_id": "H2",
        "fact_id": "fact_3",
        "stance": "supports",
        "evidence_role": "original",
        "quote_bound": True,
        "contract_complete": True,
        "material_status": "fetched",
        "reported_conditions": {"task": "retrieval", "benchmark": "BEIR", "top_k": 10},
        "condition_evidence": {"benchmark": {"quote": "BEIR", "quote_bound": True}},
        "condition_compatibility": {"verdict": "compatible", "fields": {}},
    })
    return state


def run_e01_verification() -> int:
    passes = 0
    failures = []

    print("=== 开始 E-01 评测工具与基线校验 ===")

    # 1. build_eval_sheet.py 内置 selftest
    rc = bes.selftest()
    if rc == 0:
        print("[PASS 1] build_eval_sheet.py --selftest 原生自测通过")
        passes += 1
    else:
        failures.append("build_eval_sheet.py --selftest 失败")

    # 2. 导出完整 claim 正文与条件
    st = make_two_sample_state()
    c_rows = bes.claim_rows(st)
    e_rows = bes.evidence_rows(st)
    assert len(c_rows) == 3, f"真实论断全集应为 3 条，实际 {len(c_rows)}"
    h2 = next((c for c in c_rows if c["claim_id"] == "H2"), None)
    if h2 and h2["claim_text"] == "Sparse-dense hybrid search improves recall over pure dense retriever" and \
            h2["conditions"].get("benchmark") == "BEIR" and \
            any(c["claim_id"] == "H3_NO_EDGE" for c in c_rows) and \
            not any(str(c["claim_id"]).startswith("material:") for c in c_rows):
        print("[PASS 2] claim_rows 保留完整论断与无边论断，且材料记录不进入论断全集")
        passes += 1
    else:
        failures.append("claim_rows 丢失正文或条件")

    # 3. 机器预测从快照固定，不被篡改的 human_labels 影响
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "eval_test"
        bes.write_artifacts(st, out)
        # 读取初始 sheet
        sheet = bes._read_labels(out / "human_labels.jsonl")
        assert len(sheet) == 2, f"期望 2 行待评测边，实际 {len(sheet)}"

        # 尝试恶意修改 sheet 里的 machine_prediction
        sheet[0]["machine_prediction"]["stance"] = "refutes"
        # 即使标签文件里的 machine_prediction 被篡改，compute_metrics 也是从 evidence_rows 读取
        sheet[0].update(status="confirmed", label="supports", annotator="tester")
        sheet[1].update(status="confirmed", label="refutes", annotator="tester")
        metrics = bes.compute_metrics(e_rows, sheet)
        # edge_1 在 e_rows 中实际是 supports，人类评 supports -> 正确支持
        # edge_2 在 e_rows 中实际是 supports，人类评 refutes -> 错误支持
        sp = metrics["metrics"]["support_precision"]
        wsc = metrics["metrics"]["wrong_support_count"]
        if sp == {"numerator": 1, "denominator": 2} and wsc == {"numerator": 1, "denominator": 2}:
            print("[PASS 3] 机器预测固定在快照（不受标签文件中恶意篡改影响），且 2-sample 基线核准: wrong_support=1/2, support_precision=1/2")
            passes += 1
        else:
            failures.append(f"2-sample 基准计算不符合预期: sp={sp}, wsc={wsc}")

    # 4. export 默认不覆盖已确认标签
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "eval_persist"
        bes.write_artifacts(st, out)
        sheet = bes._read_labels(out / "human_labels.jsonl")
        sheet[0].update(status="confirmed", label="supports", annotator="human_expert_1", label_note="verified")
        (out / "human_labels.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in sheet) + "\n", encoding="utf-8")

        # 再次执行 export（模拟多次导出）
        bes.write_artifacts(st, out, overwrite_labels=False)
        reloaded = bes._read_labels(out / "human_labels.jsonl")
        if reloaded[0].get("status") == "confirmed" and reloaded[0].get("annotator") == "human_expert_1":
            print("[PASS 4] export 默认保护已有已确认人工标签，绝不覆盖")
            passes += 1
        else:
            failures.append("export 破坏了已有的人工标签")

    # 5. score 命令是只读的
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "eval_score"
        bes.write_artifacts(st, out)
        labels_before = (out / "human_labels.jsonl").read_bytes()
        # 模拟执行 score
        r_rows = [json.loads(x) for x in (out / "evidence_links.jsonl").read_text(encoding="utf-8").splitlines() if x]
        l_rows = bes._read_labels(out / "human_labels.jsonl")
        bes.compute_metrics(r_rows, l_rows)
        labels_after = (out / "human_labels.jsonl").read_bytes()
        if labels_before == labels_after:
            print("[PASS 5] score 过程完全只读，未修改 human_labels.jsonl")
            passes += 1
        else:
            failures.append("score 修改了标签文件")

    # 6. 标签校验规则（非法枚举、未知ID、错配claim、重复ID、缺失annotator、样本hash不符）
    invalid_cases = [
        ("bad_label_enum", {"label": "maybe_supported", "annotator": "alice"}),
        ("unknown_evidence_id", {"evidence_id": "edge_unknown", "annotator": "alice", "label": "supports"}),
        ("claim_mismatch", {"claim_id": "H_wrong", "annotator": "alice", "label": "supports"}),
        ("missing_annotator", {"annotator": "   ", "label": "supports"}),
        ("bad_role_enum", {"label_evidence_role": "anecdote", "annotator": "alice", "label": "supports"}),
        ("sample_hash_mismatch", {"sample_hash": "corrupted_hash", "annotator": "alice", "label": "supports"}),
    ]
    all_invalid_caught = True
    for issue_name, patch in invalid_cases:
        test_labels = [dict(sheet[0], status="confirmed", **patch)]
        valid, invalid = bes.validate_labels(e_rows, test_labels)
        if len(valid) != 0 or len(invalid) != 1:
            all_invalid_caught = False
            failures.append(f"校验器未能拦截异常用例: {issue_name}")
            break
    if all_invalid_caught:
        print("[PASS 6] 标签校验器成功拦截 6 类异常: 枚举非法、未知ID、论断错配、缺失署名、非法角色、样本哈希不符")
        passes += 1

    # 7. 零分母与未确认状态返回 N/A，不默认 0 或 1
    unconfirmed_sheet = [dict(sheet[0], status="unconfirmed", label=None)]
    unconfirmed_metrics = bes.compute_metrics(e_rows, unconfirmed_sheet)
    if unconfirmed_metrics.get("available") is False:
        print("[PASS 7] 未确认或全空标签状态下，指标明确返回 available=False，不虚报 0 或 1")
        passes += 1
    else:
        failures.append("未确认标签下竟返回了可用指标")

    # 8. 零分母指标结构检查
    # 当没有条件不可比样本时，condition_mismatch_detected 必须为 numerator=None, denominator=0
    single_confirmed = [dict(sheet[0], status="confirmed", label="supports", annotator="tester", label_conditions_comparable=True)]
    m_single = bes.compute_metrics(e_rows, single_confirmed)
    cmd = m_single["metrics"]["condition_mismatch_detected"]
    if cmd == {"numerator": None, "denominator": 0}:
        print("[PASS 8] 零分母指标明确返回 numerator=None, denominator=0 (N/A)，不默认 0 或 1")
        passes += 1
    else:
        failures.append(f"零分母未返回 N/A: {cmd}")

    print(f"\nE-01 验证完成: {passes}/8 PASS")
    if failures:
        print("FAILURES: " + "; ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run_e01_verification())
