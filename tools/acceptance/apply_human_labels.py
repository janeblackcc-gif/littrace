#!/usr/bin/env python3
"""辅助脚本：将人工评审结果规范化写入 human_labels.jsonl，并执行校验与评分。

确保：
1. 校验必填字段（label, annotator, status="confirmed"）；
2. 执行 build_eval_sheet.py validate-labels；
3. 执行 build_eval_sheet.py score 并更新 metrics.json。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools" / "acceptance"))
import build_eval_sheet as bes


def apply_annotations(eval_dir: Path, annotations: list[dict]) -> dict:
    labels_file = eval_dir / "human_labels.jsonl"
    if not labels_file.is_file():
        raise FileNotFoundError(f"未找到标签文件: {labels_file}")

    original_bytes = labels_file.read_bytes()
    existing = [json.loads(line) for line in original_bytes.decode("utf-8").splitlines() if line.strip()]
    ids = [a["evidence_id"] for a in annotations]
    if len(ids) != len(set(ids)):
        raise ValueError("同一次导入不能重复指定 evidence_id")
    unknown = set(ids) - {row.get("evidence_id") for row in existing}
    if unknown:
        raise ValueError(f"人工标注引用未知 evidence_id: {sorted(unknown)}")
    anno_by_id = {a["evidence_id"]: a for a in annotations}

    updated_count = 0
    for row in existing:
        eid = row.get("evidence_id")
        if eid in anno_by_id:
            patch = anno_by_id[eid]
            row["label"] = patch.get("label")
            row["label_evidence_role"] = patch.get("label_evidence_role", row.get("label_evidence_role"))
            row["label_conditions_comparable"] = patch.get("label_conditions_comparable", row.get("label_conditions_comparable"))
            row["label_locator_correct"] = patch.get("label_locator_correct", row.get("label_locator_correct"))
            row["label_note"] = patch.get("label_note", row.get("label_note"))
            row["annotator"] = patch.get("annotator")
            row["status"] = "confirmed"
            row["needs_user_confirmation"] = False
            updated_count += 1

    # 完整校验和评分均在写入前完成，非法导入不能覆盖既有人工工作。
    evidence_rows = [json.loads(x) for x in (eval_dir / "evidence_links.jsonl").read_text(encoding="utf-8").splitlines() if x]
    claims_file = eval_dir / "claims.jsonl"
    if not claims_file.is_file():
        raise FileNotFoundError(f"未找到完整论断清单: {claims_file}")
    claims = [json.loads(x) for x in claims_file.read_text(encoding="utf-8").splitlines() if x]

    valid, invalid = bes.validate_labels(evidence_rows, existing)
    if invalid:
        raise ValueError(f"人工标签校验未通过，原文件未改写: {invalid}")
    metrics = bes.compute_metrics(evidence_rows, existing, claims=claims)
    if labels_file.read_bytes() != original_bytes:
        raise RuntimeError("人工标签文件发生并发修改，请重新读取后再导入")
    revision_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8]
    history = eval_dir / "label_history"
    history.mkdir(exist_ok=True)
    (history / (revision_id + ".json")).write_text(json.dumps({
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "before_sha256": hashlib.sha256(original_bytes).hexdigest(),
        "before_labels": [json.loads(line) for line in original_bytes.decode("utf-8").splitlines() if line.strip()],
        "submitted_annotations": annotations,
        "note": "Records reviewer-supplied labels; the tool does not establish scientific correctness",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    staged = eval_dir / ("human_labels." + revision_id + ".tmp")
    staged.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in existing) + "\n", encoding="utf-8")
    os.replace(staged, labels_file)
    (eval_dir / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "updated_count": updated_count,
        "valid_confirmed": len(valid),
        "invalid": invalid,
        "metrics_available": metrics.get("available", False),
        "metrics": metrics.get("metrics"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--eval-dir", required=True)
    parser.add_argument("--input-json", required=True)
    args = parser.parse_args()

    eval_dir = Path(args.eval_dir)
    annos = json.loads(Path(args.input_json).read_text(encoding="utf-8"))
    res = apply_annotations(eval_dir, annos)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res["valid_confirmed"] > 0 and not res["invalid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
