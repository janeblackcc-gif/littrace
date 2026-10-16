"""B10's specificity control with the explicit stance required by the S0 checker.

The original B10 fixture omits stance. Keep its original failure visible; do not
weaken the current adopted-edge contract or edit the historical probe assertion.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path

import b_report_from_checkpoint as checker


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = "Synthetic B10 specificity control"
    digest = hashlib.sha256(report.encode("utf-8")).hexdigest()
    state = {"hypotheses": [{"id": "H", "status": "supported_in_scope", "adopted_evidence": {"supports": [{"edge_id": "ok"}]}}],
             "facts": [], "evidence_links": [
                 {"id": "ok", "hypothesis_id": "H", "material_status": "fetched", "material_scope": "fulltext_page", "stance": "supports"},
                 {"id": "excluded", "hypothesis_id": "H", "material_status": "fetched", "material_scope": "abstract_page", "stance": "inconclusive"}],
             "raw_sources": [], "final_report": report, "report_hash": digest, "reviewed_report_hash": digest}
    positive = checker.build_payload(state, {"streaming_report": report}, "synthetic-B10-compatible")
    missing_stance = copy.deepcopy(state)
    missing_stance["evidence_links"][0].pop("stance")
    negative = checker.build_payload(missing_stance, {"streaming_report": report}, "synthetic-B10-original-precondition")
    checks = {"excluded_abstract_does_not_poison_explicit_adopted_fulltext": positive["invariants"]["no_strong_conclusion_on_unfetched_material"],
              "missing_stance_still_fails_closed": not negative["invariants"]["no_strong_conclusion_on_unfetched_material"]}
    result = {"passed": all(checks.values()), "checks": checks,
              "boundary": "Synthetic verifier specificity test only; no scientific entailment or independent evidence claim",
              "original_B10_fixture_difference": "The historical adopted edge has no stance. Only this explicit precondition is added in the positive control.",
              "positive": positive["claims"], "negative": negative["claims"]}
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(checks, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
