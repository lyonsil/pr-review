import unittest, importlib.util, os
spec = importlib.util.spec_from_file_location(
    "anchor", os.path.join(os.path.dirname(__file__), "pr-review-anchor.py"))
anchor = importlib.util.module_from_spec(spec); spec.loader.exec_module(anchor)


def item(i, sev="high", f="a.ts", line=10, cluster=None, **kw):
    d = {"id": i, "severity": sev, "file": f, "line": line, "line_end": line,
         "anchor_excerpt": "x", "claim": "c", "detail": "d", "cluster": cluster,
         "verdict": "CONFIRMED"}
    d.update(kw); return d


class TestValidate(unittest.TestCase):
    def test_minimal_valid_ledger_has_no_errors(self):
        led = {"items": [item(1), item(2, sev="low")], "clusters": []}
        self.assertEqual(anchor.validate(led), [])

    def test_letter_suffix_id_is_rejected(self):
        led = {"items": [item("2a")], "clusters": []}
        errs = anchor.validate(led)
        self.assertTrue(any("not an integer" in e for e in errs), errs)

    def test_non_contiguous_ids_are_rejected(self):
        led = {"items": [item(1), item(3, sev="low")], "clusters": []}
        self.assertTrue(any("contiguous" in e for e in anchor.validate(led)))

    def test_duplicate_ids_are_rejected(self):
        led = {"items": [item(1), item(1, sev="low")], "clusters": []}
        self.assertTrue(any("duplicate" in e for e in anchor.validate(led)))

    def test_items_must_be_ordered_by_severity_then_file(self):
        led = {"items": [item(1, sev="low"), item(2, sev="blocker")], "clusters": []}
        self.assertTrue(any("ordered" in e for e in anchor.validate(led)))

    def test_unknown_severity_is_rejected(self):
        led = {"items": [item(1, sev="critical")], "clusters": []}
        self.assertTrue(any("severity" in e for e in anchor.validate(led)))

    def test_cluster_referencing_unknown_item_is_rejected(self):
        led = {"items": [item(1)], "clusters": [{"id": "C1", "item_ids": [1, 99]}]}
        self.assertTrue(any("unknown item" in e for e in anchor.validate(led)))

    def test_item_referencing_unknown_cluster_is_rejected(self):
        led = {"items": [item(1, cluster="C9")], "clusters": []}
        self.assertTrue(any("unknown cluster" in e for e in anchor.validate(led)))


if __name__ == "__main__":
    unittest.main()
