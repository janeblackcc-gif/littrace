"""Human-label import must validate before writing and preserve a revision trail."""
import json
import tempfile
import unittest
from pathlib import Path

import apply_human_labels as apply
import build_eval_sheet as evaluation


class LabelUpdateTests(unittest.TestCase):
    def test_invalid_condition_and_locator_types_leave_bytes_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evaluation.write_artifacts(evaluation.synthetic_state(), root)
            label_file = root / "human_labels.jsonl"
            before = label_file.read_bytes()
            first = json.loads(before.decode("utf-8").splitlines()[0])
            for field, value in (("label_conditions_comparable", "definitely"), ("label_locator_correct", "yes"), ("label_locator_correct", 1)):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    apply.apply_annotations(root, [{"evidence_id": first["evidence_id"], "label": "supports", "annotator": "synthetic-test", field: value}])
                self.assertEqual(before, label_file.read_bytes())

    def test_valid_import_records_exact_previous_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evaluation.write_artifacts(evaluation.synthetic_state(), root)
            label_file = root / "human_labels.jsonl"
            before = [json.loads(line) for line in label_file.read_text(encoding="utf-8").splitlines()]
            result = apply.apply_annotations(root, [{"evidence_id": before[0]["evidence_id"], "label": "inconclusive", "annotator": "synthetic-test", "label_note": "Synthetic import control, not a real human label"}])
            self.assertEqual(result["updated_count"], 1)
            revisions = list((root / "label_history").glob("*.json"))
            self.assertEqual(len(revisions), 1)
            self.assertEqual(json.loads(revisions[0].read_text(encoding="utf-8"))["before_labels"], before)

    def test_invalid_import_leaves_original_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evaluation.write_artifacts(evaluation.synthetic_state(), root)
            label_file = root / "human_labels.jsonl"
            before = label_file.read_bytes()
            first = json.loads(before.decode("utf-8").splitlines()[0])
            with self.assertRaises(ValueError):
                apply.apply_annotations(root, [{"evidence_id": first["evidence_id"], "label": "bad-enum", "annotator": "synthetic-test"}])
            self.assertEqual(before, label_file.read_bytes())

    def test_unknown_ids_are_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evaluation.write_artifacts(evaluation.synthetic_state(), root)
            with self.assertRaises(ValueError):
                apply.apply_annotations(root, [{"evidence_id": "missing", "label": "supports", "annotator": "synthetic-test"}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
