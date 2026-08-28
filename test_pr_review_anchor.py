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


DIFF = """diff --git a/a.ts b/a.ts
--- a/a.ts
+++ b/a.ts
@@ -1,3 +1,4 @@
 one
+two
 three
 four
"""


class TestReanchor(unittest.TestCase):
    def test_parse_changed_lines_picks_added_new_side_numbers(self):
        self.assertEqual(anchor.parse_changed_lines(DIFF), {"a.ts": {2}})

    def _wt(self, content):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "a.ts"), "w") as fh:
            fh.write(content)
        return d

    def test_unmoved_anchor_on_changed_line_is_ok(self):
        wt = self._wt("one\ntwo\nthree\nfour\n")
        it = item(1, line=2, anchor_excerpt="two")
        self.assertEqual(anchor.reanchor_item(it, wt, {"a.ts": {2}})[0], "ok")

    def test_shifted_anchor_reports_moved_with_new_line(self):
        wt = self._wt("zero\none\ntwo\nthree\n")
        it = item(1, line=2, anchor_excerpt="two")
        status, line, _ = anchor.reanchor_item(it, wt, {"a.ts": {3}})
        self.assertEqual((status, line), ("moved", 3))

    def test_vanished_excerpt_is_dead(self):
        wt = self._wt("one\nthree\n")
        it = item(1, line=2, anchor_excerpt="two")
        self.assertEqual(anchor.reanchor_item(it, wt, {"a.ts": {1}})[0], "dead")

    def test_anchor_not_on_a_changed_line_is_dead(self):
        wt = self._wt("one\ntwo\nthree\n")
        it = item(1, line=2, anchor_excerpt="two")
        status, _, why = anchor.reanchor_item(it, wt, {"a.ts": {99}})
        self.assertEqual(status, "dead")
        self.assertIn("changed line", why)

    def test_missing_file_is_dead(self):
        it = item(1, line=2, anchor_excerpt="two", f="gone.ts")
        self.assertEqual(anchor.reanchor_item(it, self._wt("x\n"), {})[0], "dead")


# A realistic multi-file `gh pr diff`: two files each with more than one hunk
# (including a deletion-only hunk with a "+"-digit pattern in its trailing
# function context, to check the hunk-header regex doesn't grab the wrong
# number), a whole-file delete, a whole-file add, a rename that also carries a
# content change, a pure rename with no content change, and a no-trailing-
# newline hunk.
REALISTIC_DIFF = r"""diff --git a/a.ts b/a.ts
index 1111111..2222222 100644
--- a/a.ts
+++ b/a.ts
@@ -1,3 +1,4 @@
 one
+two
 three
 four
diff --git a/b.ts b/b.ts
index 3333333..4444444 100644
--- a/b.ts
+++ b/b.ts
@@ -10,4 +10,3 @@ function ctxWith+5plus(x) {
 alpha
-beta
 gamma
 delta
@@ -49,1 +48,2 @@
 zeta
+eta
diff --git a/deleted.ts b/deleted.ts
deleted file mode 100644
index 5555555..0000000
--- a/deleted.ts
+++ /dev/null
@@ -1,3 +0,0 @@
-one
-two
-three
diff --git a/added.ts b/added.ts
new file mode 100644
index 0000000..6666666
--- /dev/null
+++ b/added.ts
@@ -0,0 +1,2 @@
+hello
+world
diff --git a/old-name.ts b/new-name.ts
similarity index 88%
rename from old-name.ts
rename to new-name.ts
index 7777777..8888888 100644
--- a/old-name.ts
+++ b/new-name.ts
@@ -1,3 +1,4 @@
 keep
+added-after-rename
 tail
 tail2
diff --git a/pure-rename-old.ts b/pure-rename-new.ts
similarity index 100%
rename from pure-rename-old.ts
rename to pure-rename-new.ts
diff --git a/noeof.ts b/noeof.ts
index 9999999..aaaaaaa 100644
--- a/noeof.ts
+++ b/noeof.ts
@@ -1,2 +1,2 @@
 keep
-old last line
\ No newline at end of file
+new last line
\ No newline at end of file
"""


class TestParseChangedLinesRealisticDiff(unittest.TestCase):
    def test_multi_file_multi_hunk_delete_add_rename_noeof(self):
        self.assertEqual(anchor.parse_changed_lines(REALISTIC_DIFF), {
            "a.ts": {2},
            "b.ts": {49},
            "added.ts": {1, 2},
            "new-name.ts": {2},
            "noeof.ts": {2},
        })
        # Deleted files contribute no changed lines, and a content-changing
        # rename is keyed by its NEW path only - not the old one.
        self.assertNotIn("deleted.ts", anchor.parse_changed_lines(REALISTIC_DIFF))
        self.assertNotIn("old-name.ts", anchor.parse_changed_lines(REALISTIC_DIFF))
        # A pure rename (no content change) emits no --- /+++ pair at all, so
        # it is invisible to line-level reanchoring; an item still pointing at
        # the old path fails safe as "dead" (file not found), never a false
        # "ok" under the wrong name.
        self.assertNotIn("pure-rename-new.ts", anchor.parse_changed_lines(REALISTIC_DIFF))


class TestReanchorAmbiguity(unittest.TestCase):
    def _wt(self, content):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "a.ts"), "w") as fh:
            fh.write(content)
        return d

    def test_duplicate_excerpt_picks_nearest_and_records_ambiguity(self):
        wt = self._wt("function a() {\n}\nfunction b() {\n}\nfunction c() {\n}\n")
        it = item(1, line=4, anchor_excerpt="}")
        status, line, note = anchor.reanchor_item(it, wt, {"a.ts": {2, 4, 6}})
        self.assertEqual((status, line), ("ok", 4))
        self.assertIn("ambiguous", note)
        self.assertIn("2", note)
        self.assertIn("6", note)

    def test_ambiguous_excerpt_moved_still_records_ambiguity(self):
        wt = self._wt("function a() {\n}\nfunction b() {\n}\nfunction c() {\n}\n")
        it = item(1, line=1, anchor_excerpt="}")
        status, line, note = anchor.reanchor_item(it, wt, {"a.ts": {2, 4, 6}})
        self.assertEqual((status, line), ("moved", 2))
        self.assertIn("ambiguous", note)

    def test_unique_excerpt_has_no_ambiguity_note(self):
        wt = self._wt("one\ntwo\nthree\n")
        it = item(1, line=2, anchor_excerpt="two")
        status, line, note = anchor.reanchor_item(it, wt, {"a.ts": {2}})
        self.assertEqual(status, "ok")
        self.assertEqual(note, "")


class TestReanchorWhitespace(unittest.TestCase):
    def _wt(self, content):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "a.ts"), "w") as fh:
            fh.write(content)
        return d

    def test_trailing_whitespace_only_diff_still_counts_as_ok(self):
        wt = self._wt("one\ntwo   \nthree\n")
        it = item(1, line=2, anchor_excerpt="two")
        status, line, _ = anchor.reanchor_item(it, wt, {"a.ts": {2}})
        self.assertEqual((status, line), ("ok", 2))

    def test_reindent_is_conservatively_dead_not_falsely_ok(self):
        # Leading whitespace is significant on purpose (see reanchor_item):
        # a pure re-indent reports dead rather than silently ok, since a false
        # "ok" is the one failure this mechanism exists to prevent.
        wt = self._wt("if (x) {\n        doThing();\n}\n")
        it = item(1, line=2, anchor_excerpt="    doThing();")
        status, line, why = anchor.reanchor_item(it, wt, {"a.ts": {2}})
        self.assertEqual(status, "dead")
        self.assertIn("no longer present", why)


class TestReanchorCli(unittest.TestCase):
    def _run(self, ledger, worktree, diff):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = anchor.cmd_reanchor(SimpleNamespace(ledger=ledger, worktree=worktree, diff=diff))
        return code, out.getvalue(), err.getvalue()

    def _assert_single_curated_error(self, code, out, err):
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        lines = [line for line in err.splitlines() if line]
        self.assertEqual(len(lines), 1, err)
        self.assertTrue(lines[0].startswith("error: "), err)
        self.assertNotIn("Traceback", err)

    def _write_json(self, obj):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(obj, fh)
            return fh.name

    def _write_text(self, text, suffix=".diff"):
        with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False) as fh:
            fh.write(text)
            return fh.name

    def test_missing_ledger_emits_single_curated_error(self):
        diff_path = self._write_text(DIFF)
        try:
            code, out, err = self._run("/no/such/ledger.json", tempfile.mkdtemp(), diff_path)
        finally:
            os.remove(diff_path)
        self._assert_single_curated_error(code, out, err)

    def test_malformed_json_ledger_emits_single_curated_error(self):
        ledger_path = self._write_text("{not valid json", suffix=".json")
        diff_path = self._write_text(DIFF)
        try:
            code, out, err = self._run(ledger_path, tempfile.mkdtemp(), diff_path)
        finally:
            os.remove(ledger_path); os.remove(diff_path)
        self._assert_single_curated_error(code, out, err)

    def test_non_dict_ledger_root_emits_single_curated_error(self):
        ledger_path = self._write_json([1, 2, 3])
        diff_path = self._write_text(DIFF)
        try:
            code, out, err = self._run(ledger_path, tempfile.mkdtemp(), diff_path)
        finally:
            os.remove(ledger_path); os.remove(diff_path)
        self._assert_single_curated_error(code, out, err)

    def test_missing_diff_file_emits_single_curated_error(self):
        ledger_path = self._write_json({"items": [], "clusters": []})
        try:
            code, out, err = self._run(ledger_path, tempfile.mkdtemp(), "/no/such/diff.txt")
        finally:
            os.remove(ledger_path)
        self._assert_single_curated_error(code, out, err)

    def test_item_without_id_does_not_crash(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "a.ts"), "w") as fh:
            fh.write("one\ntwo\nthree\n")
        led = {"items": [{"file": "a.ts", "line": 2, "anchor_excerpt": "two"}], "clusters": []}
        ledger_path = self._write_json(led)
        diff_path = self._write_text(DIFF)
        try:
            code, out, err = self._run(ledger_path, d, diff_path)
        finally:
            os.remove(ledger_path); os.remove(diff_path)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("#None", out)

    def test_end_to_end_rewrites_ledger_with_status_and_line(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "a.ts"), "w") as fh:
            fh.write("zero\none\ntwo\nthree\n")
        led = {"items": [item(1, line=2, anchor_excerpt="two")], "clusters": []}
        ledger_path = self._write_json(led)
        diff_path = self._write_text(
            "diff --git a/a.ts b/a.ts\n--- a/a.ts\n+++ b/a.ts\n"
            "@@ -1,1 +1,4 @@\n+zero\n+one\n+two\n three\n"
        )
        try:
            code, out, err = self._run(ledger_path, d, diff_path)
            with open(ledger_path) as fh:
                rewritten = json.load(fh)
        finally:
            os.remove(ledger_path); os.remove(diff_path)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("ok=0 moved=1 dead=0", out)
        self.assertEqual(rewritten["items"][0]["anchor_status"], "moved")
        self.assertEqual(rewritten["items"][0]["line"], 3)


if __name__ == "__main__":
    unittest.main()
