import unittest, importlib.util, os, json, io, contextlib, tempfile
from types import SimpleNamespace
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

    def test_bool_id_is_rejected_and_not_treated_as_duplicate_of_int_1(self):
        led = {"items": [item(1), item(True, sev="low")], "clusters": []}
        errs = anchor.validate(led)
        self.assertTrue(any("not an integer" in e for e in errs), errs)
        self.assertFalse(any("duplicate" in e for e in errs), errs)


class TestCliErrorHandling(unittest.TestCase):
    def _run_validate(self, path):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = anchor.cmd_validate(SimpleNamespace(ledger=path))
        return code, out.getvalue(), err.getvalue()

    def _assert_single_curated_error(self, code, out, err):
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        lines = [line for line in err.splitlines() if line]
        self.assertEqual(len(lines), 1, err)
        self.assertTrue(lines[0].startswith("error: "), err)
        self.assertNotIn("Traceback", err)

    def test_missing_file_emits_single_curated_error_no_traceback(self):
        code, out, err = self._run_validate("/no/such/path/does-not-exist-ledger.json")
        self._assert_single_curated_error(code, out, err)

    def test_malformed_json_emits_single_curated_error_no_traceback(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write("{not valid json")
            path = fh.name
        try:
            code, out, err = self._run_validate(path)
        finally:
            os.remove(path)
        self._assert_single_curated_error(code, out, err)

    def test_non_dict_root_emits_single_curated_error_no_traceback(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write("[1, 2, 3]")
            path = fh.name
        try:
            code, out, err = self._run_validate(path)
        finally:
            os.remove(path)
        self._assert_single_curated_error(code, out, err)

    def test_valid_ledger_still_exits_0_with_exact_ok_line(self):
        led = {"items": [item(1), item(2, sev="low")], "clusters": []}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(led, fh)
            path = fh.name
        try:
            code, out, err = self._run_validate(path)
        finally:
            os.remove(path)
        self.assertEqual(code, 0)
        self.assertEqual(out, "ledger ok: 2 items, 0 clusters\n")
        self.assertEqual(err, "")


if __name__ == "__main__":
    unittest.main()
