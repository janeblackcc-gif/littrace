#!/usr/bin/env python3
"""C-04 Judge 标注协议块：用真实冻结交付正文验证生产检测与入证。"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / "backend" / "app"))


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--source-url", default="https://arxiv.org/abs/2503.13102")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    from service import research_integrity as integrity
    from service import research_sources as sources

    source = next((row for row in read_jsonl(args.bundle / "delivered_sources.jsonl")
                   if row.get("url") == args.source_url), None)
    if source is None:
        raise AssertionError(f"source not found: {args.source_url}")
    text = str(source.get("content") or "")
    locators = sources.locate_blocks(text)
    packets = integrity.source_packets([dict(source)], limit=None)
    packet = packets[0]
    protocol_locators = locators.get("annotation_protocol") or []
    body_terms = {
        "annotators": "Three in-house annotators" in text,
        "label_set": "four labels" in text,
        "independent_annotation": "annotated independently by all annotators" in text,
        "majority_vote": "majority vote" in text,
    }
    assertions = {
        "explicit_version": str(source.get("version_id") or "").endswith("v2"),
        "inside_window": source.get("window_verdict") == "inside_window",
        "identity_verified": source.get("identity_binding") == "verified",
        "protocol_locator_nonempty": bool(protocol_locators),
        "production_packet_certifies_protocol": "annotation_protocol" in packet.get("available_blocks", []),
        "protocol_body_is_substantive": all(body_terms.values()),
    }
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    frozen = dict(source)
    frozen["content_sha256_recomputed"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    (out / "frozen_source.json").write_text(
        json.dumps(frozen, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )
    summary = {
        "case_id": "c04_judge_annotation_protocol_positive",
        "source_url": args.source_url,
        "version_id": source.get("version_id"),
        "publication_first_date": source.get("publication_first_date"),
        "version_date": source.get("version_date"),
        "content_sha256": frozen["content_sha256_recomputed"],
        "protocol_locators": protocol_locators,
        "body_terms": body_terms,
        "available_blocks_before_saved_run": source.get("available_blocks"),
        "available_blocks_recomputed": packet.get("available_blocks"),
        "assertions": assertions,
        "passed": all(assertions.values()),
    }
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
