import unittest, importlib.util, os, json, io, contextlib, tempfile, re
from types import SimpleNamespace
spec = importlib.util.spec_from_file_location(
    "anchor", os.path.join(os.path.dirname(__file__), "pr-review-anchor.py"))
anchor = importlib.util.module_from_spec(spec); spec.loader.exec_module(anchor)


def item(i, sev="high", f="a.ts", line=10, cluster=None, **kw):
    d = {"id": i, "severity": sev, "severity_in": sev, "file": f, "line": line,
         "line_end": line, "anchor_excerpt": "x", "claim": "c", "detail": "d",
         "cluster": cluster, "verdict": "CONFIRMED", "verdict_reason": "r",
         "claim_verdict": "SOUND", "claim_reason": "matched at a.ts:10",
         "fix_verdict": "SOUND", "fix_reason": "checked at a.ts:10"}
    d.update(kw); return d


class TestValidate(unittest.TestCase):
    def test_minimal_valid_ledger_has_no_errors(self):
        led = {"schema": 2, "items": [item(1), item(2, sev="low")], "clusters": []}
        self.assertEqual(anchor.validate(led), [])

    def test_letter_suffix_id_is_rejected(self):
        led = {"schema": 2, "items": [item("2a")], "clusters": []}
        errs = anchor.validate(led)
        self.assertTrue(any("not an integer" in e for e in errs), errs)

    def test_non_contiguous_ids_are_rejected(self):
        led = {"schema": 2, "items": [item(1), item(3, sev="low")], "clusters": []}
        self.assertTrue(any("contiguous" in e for e in anchor.validate(led)))

    def test_duplicate_ids_are_rejected(self):
        led = {"schema": 2, "items": [item(1), item(1, sev="low")], "clusters": []}
        self.assertTrue(any("duplicate" in e for e in anchor.validate(led)))

    def test_items_must_be_ordered_by_severity_then_file(self):
        led = {"schema": 2, "items": [item(1, sev="low"), item(2, sev="blocker")], "clusters": []}
        self.assertTrue(any("ordered" in e for e in anchor.validate(led)))

    def test_unknown_severity_is_rejected(self):
        led = {"schema": 2, "items": [item(1, sev="critical")], "clusters": []}
        self.assertTrue(any("severity" in e for e in anchor.validate(led)))

    def test_cluster_referencing_unknown_item_is_rejected(self):
        led = {"schema": 2, "items": [item(1)], "clusters": [{"id": "C1", "item_ids": [1, 99]}]}
        self.assertTrue(any("unknown item" in e for e in anchor.validate(led)))

    def test_item_referencing_unknown_cluster_is_rejected(self):
        led = {"schema": 2, "items": [item(1, cluster="C9")], "clusters": []}
        self.assertTrue(any("unknown cluster" in e for e in anchor.validate(led)))

    def test_bool_id_is_rejected_and_not_treated_as_duplicate_of_int_1(self):
        led = {"schema": 2, "items": [item(1), item(True, sev="low")], "clusters": []}
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
        led = {"schema": 2, "items": [item(1), item(2, sev="low")], "clusters": []}
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


def _diff_for(changed):
    """Minimal unified diff whose parse_changed_lines() output is `changed`."""
    out = []
    for path, lines in changed.items():
        out.append(f"--- a/{path}")
        out.append(f"+++ b/{path}")
        for n in sorted(lines):
            out.append(f"@@ -0,0 +{n},1 @@")
            out.append("+x")
    return "\n".join(out) + "\n"


class TestReanchorFixesOnChangedLine(unittest.TestCase):
    """reanchor derives on_changed_line from the diff instead of trusting the
    S5 refuter. Refuters get this wrong in both directions, and a wrong `true`
    fails the ENTIRE review POST rather than just its own comment."""

    def _run(self, items, changed, content="one\ntwo\nthree\n"):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "a.ts"), "w") as fh:
            fh.write(content)
        led = {"schema": 2, "items": items, "clusters": []}
        lp = os.path.join(d, "ledger.json")
        dp = os.path.join(d, "d.diff")
        with open(lp, "w") as fh:
            json.dump(led, fh)
        # parse_changed_lines is exercised elsewhere; feed a diff that yields
        # exactly `changed` so this test isolates the correction behaviour.
        with open(dp, "w") as fh:
            fh.write(_diff_for(changed))
        args = SimpleNamespace(ledger=lp, worktree=d, diff=dp)
        anchor.cmd_reanchor(args)
        with open(lp) as fh:
            return {i["id"]: i for i in json.load(fh)["items"]}

    def test_a_refuters_false_is_corrected_to_true(self):
        out = self._run([item(1, line=2, anchor_excerpt="two",
                              on_changed_line=False)], {"a.ts": {2}})
        self.assertIs(out[1]["on_changed_line"], True)

    def test_a_refuters_true_is_corrected_to_false(self):
        """The dangerous direction: this is the one that fails the whole POST."""
        out = self._run([item(1, line=2, anchor_excerpt="two",
                              on_changed_line=True)], {"a.ts": {99}})
        self.assertIs(out[1]["on_changed_line"], False)

    def test_a_dead_anchor_is_off_diff_by_definition(self):
        out = self._run([item(1, line=2, anchor_excerpt="nowhere",
                              on_changed_line=True)], {"a.ts": {2}})
        self.assertEqual(out[1]["anchor_status"], "dead")
        self.assertIs(out[1]["on_changed_line"], False)

    def test_a_moved_anchor_is_judged_at_its_new_line(self):
        out = self._run([item(1, line=1, anchor_excerpt="three",
                              on_changed_line=False)], {"a.ts": {3}})
        self.assertEqual(out[1]["line"], 3)
        self.assertIs(out[1]["on_changed_line"], True)

    def test_a_ledger_with_no_prior_value_is_filled_in(self):
        out = self._run([item(1, line=2, anchor_excerpt="two")], {"a.ts": {2}})
        self.assertIs(out[1]["on_changed_line"], True)


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
        ledger_path = self._write_json({"schema": 2, "items": [], "clusters": []})
        try:
            code, out, err = self._run(ledger_path, tempfile.mkdtemp(), "/no/such/diff.txt")
        finally:
            os.remove(ledger_path)
        self._assert_single_curated_error(code, out, err)

    def test_item_without_id_does_not_crash(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "a.ts"), "w") as fh:
            fh.write("one\ntwo\nthree\n")
        led = {"schema": 2, "items": [{"file": "a.ts", "line": 2, "anchor_excerpt": "two"}], "clusters": []}
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
        led = {"schema": 2, "items": [item(1, line=2, anchor_excerpt="two")], "clusters": []}
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


class TestPayload(unittest.TestCase):
    def led(self):
        return {"schema": 2, "items": [
            item(1, sev="blocker", f="a.ts", line=10, cluster="C1", claim="alpha"),
            item(2, sev="blocker", f="a.ts", line=10, cluster=None, claim="beta"),
            item(3, sev="high", f="b.ts", line=20, cluster="C1", claim="gamma"),
            item(4, sev="low", f="c.ts", line=30, cluster=None, claim="delta"),
        ], "clusters": [{"id": "C1", "item_ids": [1, 3]}]}

    def test_items_on_the_same_site_merge_into_one_comment(self):
        p = anchor.build_payload(self.led(), ["all"], "intro")
        same = [c for c in p["comments"] if c["path"] == "a.ts" and c["line"] == 10]
        self.assertEqual(len(same), 1)
        self.assertIn("#1", same[0]["body"]); self.assertIn("#2", same[0]["body"])

    def test_cluster_spanning_two_sites_cross_references_the_other(self):
        p = anchor.build_payload(self.led(), ["all"], "intro")
        b = next(c for c in p["comments"] if c["path"] == "b.ts")["body"]
        self.assertIn("Similar fix as #1", b)

    def test_cluster_on_a_single_site_gets_no_cross_reference(self):
        led = {"schema": 2, "items": [item(1, cluster="C2")], "clusters": [{"id": "C2", "item_ids": [1]}]}
        p = anchor.build_payload(led, ["all"], "intro")
        self.assertNotIn("Similar fix as", p["comments"][0]["body"])

    def test_attribution_appears_once_in_body_and_never_in_a_comment(self):
        p = anchor.build_payload(self.led(), ["all"], "intro")
        self.assertEqual(p["body"].count(anchor.ATTRIBUTION), 1)
        for c in p["comments"]:
            self.assertNotIn(anchor.ATTRIBUTION, c["body"])

    def test_selecting_a_cluster_pulls_in_exactly_its_items(self):
        self.assertEqual(anchor.resolve_selection(self.led(), ["C1"]), {1, 3})

    def test_unselected_items_are_absent(self):
        p = anchor.build_payload(self.led(), ["4"], "intro")
        self.assertEqual([c["path"] for c in p["comments"]], ["c.ts"])

    def test_refuted_and_dead_items_are_excluded(self):
        led = self.led()
        led["items"][0]["verdict"] = "REFUTED"
        led["items"][2]["anchor_status"] = "dead"
        p = anchor.build_payload(led, ["all"], "intro")
        joined = " ".join(c["body"] for c in p["comments"])
        self.assertNotIn("alpha", joined); self.assertNotIn("gamma", joined)

    def test_event_is_comment_not_approve_or_request_changes(self):
        self.assertEqual(anchor.build_payload(self.led(), ["all"], "i")["event"], "COMMENT")

    # --- Critical-review follow-ups (see task-5-report.md) ---

    def test_unrecognized_selection_token_is_a_selection_error_not_system_exit(self):
        # A typo'd token must never silently produce a partial/wrong selection
        # (SystemExit would also take the whole process down rather than
        # letting the CLI layer emit the usual curated `error: ...` line).
        with self.assertRaises(anchor.SelectionError):
            anchor.resolve_selection(self.led(), ["bogus"])

    def test_selection_naming_an_id_absent_from_the_ledger_is_a_selection_error(self):
        # id 99 doesn't exist in led(); silently dropping it would make the
        # caller believe they posted a finding that never made it into the set.
        with self.assertRaises(anchor.SelectionError):
            anchor.resolve_selection(self.led(), ["99"])

    def test_dropped_selected_items_are_surfaced_as_a_stderr_warning(self):
        led = self.led()
        led["items"][0]["verdict"] = "REFUTED"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            # #4 keeps the payload non-empty; an all-excluded selection is a
            # PayloadError, covered separately below.
            anchor.build_payload(led, ["1", "4"], "intro")
        self.assertIn("#1", err.getvalue())
        self.assertIn("REFUTED", err.getvalue())

    def test_cluster_spanning_three_sites_cross_references_both_other_sites(self):
        led = {"schema": 2, "items": [
            item(1, f="a.ts", line=1, cluster="C1"),
            item(2, f="b.ts", line=2, cluster="C1"),
            item(3, f="c.ts", line=3, cluster="C1"),
        ], "clusters": [{"id": "C1", "item_ids": [1, 2, 3]}]}
        p = anchor.build_payload(led, ["all"], "intro")
        bodies = {c["path"]: c["body"] for c in p["comments"]}
        self.assertIn("Similar fix as #2, #3.", bodies["a.ts"])
        self.assertIn("Similar fix as #1, #3.", bodies["b.ts"])
        self.assertIn("Similar fix as #1, #2.", bodies["c.ts"])

    # --- Fix round 1 ---

    def test_body_already_ending_with_attribution_is_not_double_appended(self):
        intro = f"Round 2 review.\n\n{anchor.ATTRIBUTION}"
        p = anchor.build_payload(self.led(), ["all"], intro)
        self.assertEqual(p["body"].count(anchor.ATTRIBUTION), 1)

    def test_item_with_null_line_raises_payload_error_naming_the_id(self):
        led = {"schema": 2, "items": [item(1, line=None)], "clusters": []}
        with self.assertRaises(anchor.PayloadError) as ctx:
            anchor.build_payload(led, ["all"], "intro")
        self.assertIn("#1", str(ctx.exception))

    def test_item_missing_file_raises_payload_error_naming_the_id(self):
        led = {"schema": 2, "items": [item(1)], "clusters": []}
        del led["items"][0]["file"]
        with self.assertRaises(anchor.PayloadError) as ctx:
            anchor.build_payload(led, ["all"], "intro")
        self.assertIn("#1", str(ctx.exception))

    def test_cluster_referencing_an_unknown_item_is_a_selection_error(self):
        # Mirrors the direct-digit case: --select 99 already errors when 99
        # isn't in the ledger, so a cluster whose item_ids include a
        # nonexistent id must error the same way, not silently expand short.
        led = {"schema": 2, "items": [item(1, cluster="C1")],
               "clusters": [{"id": "C1", "item_ids": [1, 99]}]}
        with self.assertRaises(anchor.SelectionError):
            anchor.resolve_selection(led, ["C1"])


class TestIncludeRefuted(unittest.TestCase):
    def led(self):
        # Two independently REFUTED items (1 and 3) at different sites, plus
        # a plain CONFIRMED item (4) to use as a "not actually REFUTED" id.
        led = {"schema": 2, "items": [
            item(1, sev="blocker", f="a.ts", line=10, cluster=None, claim="alpha"),
            item(2, sev="blocker", f="a.ts", line=10, cluster=None, claim="beta"),
            item(3, sev="high", f="b.ts", line=20, cluster=None, claim="gamma"),
            item(4, sev="low", f="c.ts", line=30, cluster=None, claim="delta"),
        ], "clusters": []}
        led["items"][0]["verdict"] = "REFUTED"
        led["items"][2]["verdict"] = "REFUTED"
        return led

    def test_readmits_only_the_named_id_the_other_refuted_item_stays_out(self):
        p = anchor.build_payload(self.led(), ["all"], "intro", include_refuted=["1"])
        joined = " ".join(c["body"] for c in p["comments"])
        self.assertIn("alpha", joined)   # #1: named in --include-refuted
        self.assertNotIn("gamma", joined)  # #3: also REFUTED, not named - stays out

    def test_unknown_id_passed_to_the_flag_is_an_error(self):
        with self.assertRaises(anchor.IncludeRefutedError):
            anchor.resolve_include_refuted(self.led(), ["99"])

    def test_id_that_is_not_refuted_passed_to_the_flag_is_an_error(self):
        # item 4 is CONFIRMED, not REFUTED - nothing for the flag to lift.
        with self.assertRaises(anchor.IncludeRefutedError):
            anchor.resolve_include_refuted(self.led(), ["4"])

    def test_readmitted_item_still_must_be_selected_to_appear(self):
        # --include-refuted only lifts the REFUTED exclusion; it is not a
        # second selection mechanism. #1 is named but never selected here.
        p = anchor.build_payload(self.led(), ["4"], "intro", include_refuted=["1"])
        self.assertEqual([c["path"] for c in p["comments"]], ["c.ts"])

    def test_include_refuted_does_not_resurrect_a_dead_anchor(self):
        # #2 is postable and only keeps the payload non-empty, so the dead #1
        # is proven absent rather than the whole build failing for being empty.
        led = {"schema": 2, "items": [item(1, f="a.ts"), item(2, f="b.ts", claim="live")], "clusters": []}
        led["items"][0]["verdict"] = "REFUTED"
        led["items"][0]["anchor_status"] = "dead"
        p = anchor.build_payload(led, ["all"], "intro", include_refuted=["1"])
        self.assertEqual([c["path"] for c in p["comments"]], ["b.ts"])

    def test_warning_distinguishes_excluded_from_readmitted(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            anchor.build_payload(self.led(), ["all"], "intro", include_refuted=["1"])
        out = err.getvalue()
        self.assertIn("re-admitted by --include-refuted", out)
        self.assertIn("#1", out)
        self.assertNotIn("#1 (excluded", out)
        self.assertIn("#3 (excluded, REFUTED)", out)

    def test_per_id_scoping_is_not_a_blanket_override(self):
        # Naming exactly one REFUTED id must never re-admit every REFUTED
        # item - that would defeat the point of refuting them. This is the
        # scenario the round's mutation check targets directly.
        p = anchor.build_payload(self.led(), ["all"], "intro", include_refuted=["1"])
        ids_posted = {int(m) for c in p["comments"] for m in re.findall(r"#(\d+)", c["body"])}
        self.assertIn(1, ids_posted)
        self.assertNotIn(3, ids_posted)


class TestOnChangedLineExclusion(unittest.TestCase):
    """`on_changed_line: false` means GitHub cannot accept the comment inline,
    and it rejects the WHOLE review over one such anchor - so payload must
    keep it out of comments[] itself rather than trusting reanchor to have
    run first. The finding is not lost, though: it lands in the review body
    instead (see TestDemotedFindings), demoted rather than excluded."""

    def led(self):
        return {"schema": 2, "items": [
            item(1, f="a.ts", line=10, claim="offline", on_changed_line=False),
            item(2, f="b.ts", line=20, claim="online", on_changed_line=True),
        ], "clusters": []}

    def test_item_not_on_a_changed_line_is_excluded_from_comments(self):
        with contextlib.redirect_stderr(io.StringIO()):
            p = anchor.build_payload(self.led(), ["all"], "intro")
        self.assertEqual([c["path"] for c in p["comments"]], ["b.ts"])

    def test_demotion_is_warned_on_stderr_not_as_an_exclusion(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            anchor.build_payload(self.led(), ["all"], "intro")
        out = err.getvalue()
        self.assertIn("#1", out)
        self.assertIn("moved into the review body", out)
        self.assertIn("not on a changed line", out)
        self.assertNotIn("#1 (excluded", out)

    def test_missing_or_none_on_changed_line_is_not_excluded(self):
        # A ledger that never went through S5/reanchor has no such field; it
        # must stay postable, not silently lose every item.
        led = {"schema": 2, "items": [item(1, f="a.ts"), item(2, f="b.ts", on_changed_line=None)],
               "clusters": []}
        p = anchor.build_payload(led, ["all"], "intro")
        self.assertEqual(sorted(c["path"] for c in p["comments"]), ["a.ts", "b.ts"])

    def test_include_refuted_does_not_lift_the_on_changed_line_exclusion(self):
        led = self.led()
        led["items"][0]["verdict"] = "REFUTED"
        with contextlib.redirect_stderr(io.StringIO()):
            p = anchor.build_payload(led, ["all"], "intro", include_refuted=["1"])
        self.assertEqual([c["path"] for c in p["comments"]], ["b.ts"])


class TestDemotedFindings(unittest.TestCase):
    """A selected item that cannot be anchored to a changed line (a dead
    anchor, or on_changed_line: false) is still a real, verified finding -
    it is demoted into the review body instead of being dropped. REFUTED
    stays a true exclusion: checked first, so a REFUTED item is excluded
    rather than demoted even when it is also dead, unless re-admitted."""

    def test_dead_item_is_demoted_to_body_not_comments(self):
        led = {"schema": 2, "items": [item(1, claim="alpha", anchor_status="dead",
                               anchor_note="anchor excerpt no longer present")],
               "clusters": []}
        with contextlib.redirect_stderr(io.StringIO()):
            p = anchor.build_payload(led, ["1"], "intro")
        self.assertEqual(p["comments"], [])
        self.assertIn("alpha", p["body"])
        self.assertIn("#1", p["body"])
        self.assertIn("Findings that could not be attached to a line in this diff", p["body"])
        self.assertIn("anchor excerpt no longer present", p["body"])

    def test_on_changed_line_false_item_is_demoted_to_body_not_comments(self):
        led = {"schema": 2, "items": [item(1, claim="beta", on_changed_line=False)], "clusters": []}
        with contextlib.redirect_stderr(io.StringIO()):
            p = anchor.build_payload(led, ["1"], "intro")
        self.assertEqual(p["comments"], [])
        self.assertIn("beta", p["body"])
        self.assertIn("Findings that could not be attached to a line in this diff", p["body"])

    def test_demoted_section_includes_id_severity_location_claim_and_detail(self):
        led = {"schema": 2, "items": [item(1, sev="high", f="a.ts", line=42, claim="alpha claim",
                               detail="alpha detail", anchor_status="dead")],
               "clusters": []}
        with contextlib.redirect_stderr(io.StringIO()):
            p = anchor.build_payload(led, ["1"], "intro")
        body = p["body"]
        self.assertIn("#1", body); self.assertIn("high", body)
        self.assertIn("a.ts:42", body)
        self.assertIn("alpha claim", body); self.assertIn("alpha detail", body)

    def test_refuted_item_is_never_demoted_even_if_dead(self):
        led = {"schema": 2, "items": [
            item(1, claim="alpha", verdict="REFUTED", anchor_status="dead"),
            item(2, claim="beta"),  # keeps the payload non-empty
        ], "clusters": []}
        with contextlib.redirect_stderr(io.StringIO()):
            p = anchor.build_payload(led, ["all"], "intro")
        self.assertNotIn("alpha", p["body"])
        self.assertNotIn("alpha", " ".join(c["body"] for c in p["comments"]))

    def test_readmitted_refuted_dead_item_lands_in_body_not_comments(self):
        led = {"schema": 2, "items": [item(1, claim="alpha", verdict="REFUTED", anchor_status="dead")],
               "clusters": []}
        with contextlib.redirect_stderr(io.StringIO()):
            p = anchor.build_payload(led, ["1"], "intro", include_refuted=["1"])
        self.assertEqual(p["comments"], [])
        self.assertIn("alpha", p["body"])
        self.assertIn("Findings that could not be attached to a line in this diff", p["body"])

    def test_attribution_is_last_after_demoted_section(self):
        led = {"schema": 2, "items": [item(1, anchor_status="dead")], "clusters": []}
        with contextlib.redirect_stderr(io.StringIO()):
            p = anchor.build_payload(led, ["1"], "intro")
        body = p["body"]
        self.assertEqual(body.count(anchor.ATTRIBUTION), 1)
        self.assertTrue(body.rstrip().endswith(anchor.ATTRIBUTION))
        self.assertLess(body.index("Findings that could not be attached"), body.index(anchor.ATTRIBUTION))

    def test_attribution_stays_last_even_if_intro_already_ends_with_it(self):
        led = {"schema": 2, "items": [item(1, anchor_status="dead")], "clusters": []}
        intro = f"Review intro.\n\n{anchor.ATTRIBUTION}"
        with contextlib.redirect_stderr(io.StringIO()):
            p = anchor.build_payload(led, ["1"], intro)
        body = p["body"]
        self.assertEqual(body.count(anchor.ATTRIBUTION), 1)
        self.assertTrue(body.rstrip().endswith(anchor.ATTRIBUTION))

    def test_demotion_warning_distinguishes_from_exclusion(self):
        led = {"schema": 2, "items": [
            item(1, claim="alpha", anchor_status="dead"),
            item(2, claim="beta", verdict="REFUTED"),
            item(3, claim="gamma"),  # keeps the payload non-empty
        ], "clusters": []}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            anchor.build_payload(led, ["all"], "intro")
        out = err.getvalue()
        self.assertIn("moved into the review body", out)
        self.assertIn("#1", out)
        self.assertNotIn("#1 (excluded", out)
        self.assertIn("#2 (excluded, REFUTED)", out)


class TestIncludeRefutedNotSelected(unittest.TestCase):
    def led(self):
        return {"schema": 2, "items": [
            item(1, f="a.ts", claim="alpha", verdict="REFUTED"),
            item(2, f="b.ts", claim="beta"),
        ], "clusters": []}

    def test_warns_when_an_included_id_is_outside_the_selection(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            anchor.build_payload(self.led(), ["2"], "intro", include_refuted=["1"])
        out = err.getvalue()
        self.assertIn("not in --select", out)
        self.assertIn("#1", out)

    def test_it_is_a_warning_not_an_error_a_superset_still_builds(self):
        p = anchor.build_payload(self.led(), ["2"], "intro", include_refuted=["1"])
        self.assertEqual([c["path"] for c in p["comments"]], ["b.ts"])

    def test_no_warning_when_the_included_id_is_selected(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            anchor.build_payload(self.led(), ["all"], "intro", include_refuted=["1"])
        self.assertNotIn("not in --select", err.getvalue())


class TestEmptyPayloadIsFatal(unittest.TestCase):
    """A payload with nothing substantive - no inline comments and no
    demoted findings - would POST bare framing plus the attribution footer,
    which is never what the caller asked for. A payload with zero comments
    but a non-empty demoted section is valid (see TestDemotedFindings): the
    findings are still in the body, just not anchored inline."""

    def test_selecting_only_a_refuted_id_raises_instead_of_building_empty(self):
        led = {"schema": 2, "items": [item(1, verdict="REFUTED")], "clusters": []}
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(anchor.PayloadError) as ctx:
                anchor.build_payload(led, ["1"], "intro")
        self.assertIn("REFUTED", str(ctx.exception))
        self.assertIn("#1", str(ctx.exception))

    def test_selecting_only_a_dead_anchor_no_longer_raises(self):
        # A dead anchor is still a real, verified finding - it demotes into
        # the review body instead of failing the whole payload.
        led = {"schema": 2, "items": [item(1, claim="alpha", anchor_status="dead")], "clusters": []}
        with contextlib.redirect_stderr(io.StringIO()):
            p = anchor.build_payload(led, ["1"], "intro")
        self.assertEqual(p["comments"], [])
        self.assertIn("alpha", p["body"])

    def test_all_dead_selection_is_zero_comments_but_valid(self):
        led = {"schema": 2, "items": [item(1, claim="alpha", f="a.ts", anchor_status="dead"),
                          item(2, claim="beta", f="b.ts", anchor_status="dead")],
               "clusters": []}
        with contextlib.redirect_stderr(io.StringIO()):
            p = anchor.build_payload(led, ["all"], "intro")
        self.assertEqual(p["comments"], [])
        self.assertIn("alpha", p["body"]); self.assertIn("beta", p["body"])

    def test_a_cluster_with_no_working_item_ids_raises(self):
        # The `items:`-instead-of-`item_ids:` typo validate() cannot catch.
        led = {"schema": 2, "items": [item(1)], "clusters": [{"id": "C1", "items": [1]}]}
        with self.assertRaises(anchor.PayloadError) as ctx:
            anchor.build_payload(led, ["C1"], "intro")
        self.assertIn("no items", str(ctx.exception))

    def test_an_empty_ledger_raises(self):
        with self.assertRaises(anchor.PayloadError):
            anchor.build_payload({"schema": 2, "items": [], "clusters": []}, ["all"], "intro")


class TestPayloadCli(unittest.TestCase):
    def _run(self, ledger, select, body_file, include_refuted=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = anchor.cmd_payload(SimpleNamespace(
                ledger=ledger, select=select, body_file=body_file, include_refuted=include_refuted))
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

    def _write_text(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write(text)
            return fh.name

    def test_missing_ledger_malformed_json_and_missing_body_file_emit_curated_errors(self):
        good_ledger = self._write_json({"schema": 2, "items": [item(1)], "clusters": []})
        bad_ledger = self._write_text("{not valid json")
        body_file = self._write_text("intro")
        try:
            for ledger_path, select, body in (
                ("/no/such/ledger.json", None, body_file),
                (bad_ledger, None, body_file),
                (good_ledger, None, "/no/such/body.txt"),
            ):
                with self.subTest(ledger_path=ledger_path, body=body):
                    code, out, err = self._run(ledger_path, select, body)
                    self._assert_single_curated_error(code, out, err)
        finally:
            os.remove(good_ledger); os.remove(bad_ledger); os.remove(body_file)

    def test_unanchored_item_via_cli_emits_curated_error_not_a_traceback(self):
        led = {"schema": 2, "items": [item(1, line=None)], "clusters": []}
        del led["items"][0]["anchor_excerpt"]  # not relevant to payload; keep item minimal
        ledger_path = self._write_json(led)
        body_file = self._write_text("intro")
        try:
            code, out, err = self._run(ledger_path, None, body_file)
        finally:
            os.remove(ledger_path); os.remove(body_file)
        self._assert_single_curated_error(code, out, err)

    def test_empty_payload_via_cli_exits_1_with_a_curated_error(self):
        # Not _assert_single_curated_error: the REFUTED exclusion warning is a
        # second, legitimate stderr line alongside the fatal error.
        led = {"schema": 2, "items": [item(1, verdict="REFUTED")], "clusters": []}
        ledger_path = self._write_json(led)
        body_file = self._write_text("intro")
        try:
            code, out, err = self._run(ledger_path, ["1"], body_file)
        finally:
            os.remove(ledger_path); os.remove(body_file)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertNotIn("Traceback", err)
        self.assertTrue(any(line.startswith("error: selection produced no comments")
                            for line in err.splitlines()), err)

    def test_unrecognized_select_token_via_cli_emits_curated_error(self):
        ledger_path = self._write_json({"schema": 2, "items": [item(1)], "clusters": []})
        body_file = self._write_text("intro")
        try:
            code, out, err = self._run(ledger_path, ["bogus"], body_file)
        finally:
            os.remove(ledger_path); os.remove(body_file)
        self._assert_single_curated_error(code, out, err)

    def test_end_to_end_cli_writes_valid_comment_event_json_to_stdout(self):
        ledger_path = self._write_json({"schema": 2, "items": [item(1, claim="alpha")], "clusters": []})
        body_file = self._write_text("Review intro")
        try:
            code, out, err = self._run(ledger_path, None, body_file)
        finally:
            os.remove(ledger_path); os.remove(body_file)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        payload = json.loads(out)
        self.assertEqual(payload["event"], "COMMENT")
        self.assertEqual(payload["body"].count(anchor.ATTRIBUTION), 1)
        self.assertEqual(len(payload["comments"]), 1)
        self.assertIn("alpha", payload["comments"][0]["body"])

    def test_include_refuted_unknown_id_via_cli_emits_curated_error(self):
        ledger_path = self._write_json({"schema": 2, "items": [item(1)], "clusters": []})
        body_file = self._write_text("intro")
        try:
            code, out, err = self._run(ledger_path, None, body_file, include_refuted=["99"])
        finally:
            os.remove(ledger_path); os.remove(body_file)
        self._assert_single_curated_error(code, out, err)

    def test_include_refuted_end_to_end_posts_the_overridden_item(self):
        led = {"schema": 2, "items": [item(1, claim="alpha")], "clusters": []}
        led["items"][0]["verdict"] = "REFUTED"
        ledger_path = self._write_json(led)
        body_file = self._write_text("Review intro")
        try:
            code, out, err = self._run(ledger_path, None, body_file, include_refuted=["1"])
        finally:
            os.remove(ledger_path); os.remove(body_file)
        self.assertEqual(code, 0)
        self.assertIn("re-admitted by --include-refuted", err)
        payload = json.loads(out)
        self.assertEqual(len(payload["comments"]), 1)
        self.assertIn("alpha", payload["comments"][0]["body"])

    def test_all_dead_selection_via_cli_exits_0_with_zero_comments_and_demoted_body(self):
        led = {"schema": 2, "items": [item(1, claim="alpha", anchor_status="dead")], "clusters": []}
        ledger_path = self._write_json(led)
        body_file = self._write_text("Review intro")
        try:
            code, out, err = self._run(ledger_path, None, body_file)
        finally:
            os.remove(ledger_path); os.remove(body_file)
        self.assertEqual(code, 0)
        self.assertIn("moved into the review body", err)
        payload = json.loads(out)
        self.assertEqual(payload["comments"], [])
        self.assertIn("alpha", payload["body"])
        self.assertIn("Findings that could not be attached to a line in this diff", payload["body"])


class TestRenderPreview(unittest.TestCase):
    def _payload(self, comments=None, body="Review intro", event="COMMENT"):
        return {"body": body, "event": event,
                "comments": comments if comments is not None else []}

    def test_renders_all_three_comments_with_full_bodies(self):
        comments = [
            {"path": "a.ts", "line": 10, "side": "RIGHT", "body": "first comment body"},
            {"path": "b.ts", "line": 22, "side": "RIGHT", "body": "second comment body"},
            {"path": "c.ts", "line": 5, "side": "RIGHT", "body": "third comment body"},
        ]
        out = anchor.render_preview(self._payload(comments=comments))
        for c in comments:
            self.assertIn(f"{c['path']}:{c['line']}", out)
            self.assertIn(c["body"], out)
        self.assertIn("comments: 3", out)

    def test_multiline_markdown_body_reproduced_byte_for_byte(self):
        body = "Line one.\n\n**bold** and `code`\n\n- item 1\n- item 2\n\n> quote\n\nEnd."
        payload = self._payload(comments=[{"path": "x.ts", "line": 1, "side": "RIGHT", "body": body}])
        out = anchor.render_preview(payload)
        # Not a prefix check - the exact multi-line string must appear verbatim.
        self.assertIn(body, out)

    def test_review_body_appears_in_full(self):
        long_body = "Intro paragraph.\n\n" + "\n".join(f"Detail line {n}." for n in range(50))
        out = anchor.render_preview(self._payload(body=long_body))
        self.assertIn(long_body, out)

    def test_header_reports_event_and_comment_count(self):
        out = anchor.render_preview(self._payload(comments=[
            {"path": "a.ts", "line": 1, "side": "RIGHT", "body": "x"},
            {"path": "b.ts", "line": 2, "side": "RIGHT", "body": "y"},
        ], event="COMMENT"))
        self.assertIn("COMMENT", out)
        self.assertIn("comments: 2", out)

    def test_no_html_tags_in_output(self):
        out = anchor.render_preview(self._payload(comments=[
            {"path": "a.ts", "line": 1, "side": "RIGHT", "body": "plain body"},
        ]))
        for tag in ("<details>", "<summary>", "<br>"):
            self.assertNotIn(tag, out)

    def test_demoted_section_renders_verbatim_and_is_not_truncated(self):
        led = {"schema": 2, "items": [item(1, claim="alpha claim", detail="alpha detail " + "z" * 500,
                               anchor_status="dead",
                               anchor_note="anchor excerpt no longer present")],
               "clusters": []}
        with contextlib.redirect_stderr(io.StringIO()):
            payload = anchor.build_payload(led, ["1"], "intro")
        out = anchor.render_preview(payload)
        self.assertIn(payload["body"], out)
        self.assertIn("Findings that could not be attached to a line in this diff", out)
        self.assertIn("alpha claim", out)
        self.assertIn("alpha detail " + "z" * 500, out)


class TestPreviewCli(unittest.TestCase):
    def _run(self, payload_path):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = anchor.cmd_preview(SimpleNamespace(payload=payload_path))
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

    def _write_text(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write(text)
            return fh.name

    def test_missing_file_emits_single_curated_error(self):
        code, out, err = self._run("/no/such/payload.json")
        self._assert_single_curated_error(code, out, err)

    def test_malformed_json_emits_single_curated_error(self):
        path = self._write_text("{not valid json")
        try:
            code, out, err = self._run(path)
        finally:
            os.remove(path)
        self._assert_single_curated_error(code, out, err)

    def test_non_dict_root_emits_single_curated_error(self):
        path = self._write_json([1, 2, 3])
        try:
            code, out, err = self._run(path)
        finally:
            os.remove(path)
        self._assert_single_curated_error(code, out, err)

    def test_missing_comments_key_emits_single_curated_error(self):
        path = self._write_json({"body": "intro", "event": "COMMENT"})
        try:
            code, out, err = self._run(path)
        finally:
            os.remove(path)
        self._assert_single_curated_error(code, out, err)

    def test_end_to_end_renders_full_payload_byte_for_byte(self):
        payload = {"body": "Review body text " + "x" * 500, "event": "COMMENT",
                   "comments": [
                       {"path": "a.ts", "line": 10, "side": "RIGHT", "body": "comment one body"},
                       {"path": "b.ts", "line": 5, "side": "RIGHT",
                        "body": "comment two body " + "y" * 500},
                   ]}
        path = self._write_json(payload)
        try:
            code, out, err = self._run(path)
        finally:
            os.remove(path)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn(payload["body"], out)
        for c in payload["comments"]:
            self.assertIn(c["body"], out)
            self.assertIn(f"{c['path']}:{c['line']}", out)


if __name__ == "__main__":
    unittest.main()


class TestTerminalVerdictRequired(unittest.TestCase):
    """An item whose refuter answer was never recorded must not reach posting.
    The payload's exclusion is REFUTED-only, so a placeholder verdict posts as
    though the finding had been confirmed."""

    def test_placeholder_verdict_is_rejected(self):
        led = {"schema": 2, "items": [item(1, verdict="PENDING")], "clusters": []}
        self.assertTrue(any("not one of" in e and "verdict" in e
                            for e in anchor.validate(led)), anchor.validate(led))

    def test_missing_verdict_is_rejected(self):
        led = {"schema": 2, "items": [item(1)], "clusters": []}
        del led["items"][0]["verdict"]
        self.assertTrue(any("verdict" in e for e in anchor.validate(led)))

    def test_each_terminal_verdict_is_accepted(self):
        """Literals, not anchor.VERDICTS: iterating the constant under test
        shrinks with it, so narrowing the accepted set would still pass."""
        for v, extra in (("CONFIRMED", {}), ("REFUTED", {}), ("INCONCLUSIVE", {}),
                         ("DOWNGRADED", {"severity": "low", "severity_in": "high",
                                         "fold_note": "folded"})):
            led = {"schema": 2, "items": [item(1, verdict=v, **extra)], "clusters": []}
            self.assertEqual(anchor.validate(led), [], f"{v} should be legal")

    def test_blank_verdict_reason_is_rejected(self):
        led = {"schema": 2, "items": [item(1, verdict_reason="   ")], "clusters": []}
        self.assertTrue(any("verdict_reason" in e for e in anchor.validate(led)))

    def test_missing_severity_in_is_rejected(self):
        led = {"schema": 2, "items": [item(1)], "clusters": []}
        del led["items"][0]["severity_in"]
        self.assertTrue(any("severity_in" in e for e in anchor.validate(led)))

    def test_unknown_severity_in_is_rejected(self):
        led = {"schema": 2, "items": [item(1, severity_in="critical")], "clusters": []}
        self.assertTrue(any("severity_in" in e for e in anchor.validate(led)))


class TestFixVerdictRequired(unittest.TestCase):
    """The `**Fix:**` slot posts to the author verbatim, so it carries its own
    verified answer. A claim is verified by observation; a fix is a proposal
    about code that does not exist yet, and nothing upstream of this checked
    it - five of twenty fix lines on paranext-core #2821 were unworkable as
    written while every claim under them was sound."""

    def test_missing_fix_verdict_is_rejected(self):
        led = {"schema": 2, "items": [item(1)], "clusters": []}
        del led["items"][0]["fix_verdict"]
        errs = anchor.validate(led)
        self.assertTrue(any("fix_verdict" in e for e in errs), errs)

    def test_unknown_fix_verdict_is_rejected(self):
        led = {"schema": 2, "items": [item(1, fix_verdict="PENDING")], "clusters": []}
        errs = anchor.validate(led)
        self.assertTrue(any("fix_verdict" in e and "not one of" in e for e in errs), errs)

    def test_each_fix_verdict_is_accepted(self):
        """Literals, not anchor.FIX_VERDICTS - see test_each_claim_verdict_is_accepted."""
        for v in ("SOUND", "REWRITTEN", "UNVERIFIABLE"):
            extra = {"fix_original": "older fix"} if v == "REWRITTEN" else {}
            led = {"schema": 2, "items": [item(1, fix_verdict=v, **extra)], "clusters": []}
            self.assertEqual(anchor.validate(led), [], f"{v} should be legal")

    def test_blank_fix_reason_is_rejected(self):
        led = {"schema": 2, "items": [item(1, fix_reason="   ")], "clusters": []}
        errs = anchor.validate(led)
        self.assertTrue(any("fix_reason" in e for e in errs), errs)

    def test_refuted_item_needs_no_fix_verdict(self):
        """A REFUTED finding posts nowhere, so its fix text reaches no author.
        Holding it to this bar would block ledgers over text nobody reads."""
        led = {"schema": 2, "items": [item(1, verdict="REFUTED")], "clusters": []}
        del led["items"][0]["fix_verdict"]
        del led["items"][0]["fix_reason"]
        self.assertEqual(anchor.validate(led), [])

    def test_publishing_verdicts_all_require_a_fix_verdict(self):
        """DOWNGRADED and INCONCLUSIVE post inline whenever they anchor, so the
        exemption is REFUTED-only and must not widen to every non-CONFIRMED."""
        for v in ("CONFIRMED", "DOWNGRADED", "INCONCLUSIVE"):
            led = {"schema": 2, "items": [item(1, verdict=v)], "clusters": []}
            del led["items"][0]["fix_verdict"]
            errs = anchor.validate(led)
            self.assertTrue(any("fix_verdict" in e for e in errs),
                            f"{v} publishes, so it must require a fix verdict; got {errs}")

    def test_missing_claim_verdict_is_rejected(self):
        led = {"schema": 2, "items": [item(1)], "clusters": []}
        del led["items"][0]["claim_verdict"]
        errs = anchor.validate(led)
        self.assertTrue(any("claim_verdict" in e for e in errs), errs)

    def test_unknown_claim_verdict_is_rejected(self):
        led = {"schema": 2, "items": [item(1, claim_verdict="PENDING")], "clusters": []}
        errs = anchor.validate(led)
        self.assertTrue(any("claim_verdict" in e and "not one of" in e for e in errs), errs)

    def test_each_claim_verdict_is_accepted(self):
        """Literals, not `anchor.CLAIM_VERDICTS` - iterating the constant under
        test makes the loop shrink with it, so narrowing the accepted set would
        pass. Verified by mutation: dropping REWRITTEN fails this only as written."""
        for v, extra in (("SOUND", {}), ("REWRITTEN", {"claim_original": "older claim"})):
            led = {"schema": 2, "items": [item(1, claim_verdict=v, **extra)], "clusters": []}
            self.assertEqual(anchor.validate(led), [], f"{v} should be legal")

    def test_claim_verdict_has_no_unverifiable_escape(self):
        """A claim the refuter could not settle is INCONCLUSIVE at the verdict
        level. A second way to say 'I could not tell' would be used to dodge the
        scope question, which is the one this field exists to force."""
        self.assertNotIn("UNVERIFIABLE", anchor.CLAIM_VERDICTS)
        led = {"schema": 2, "items": [item(1, claim_verdict="UNVERIFIABLE")], "clusters": []}
        errs = anchor.validate(led)
        self.assertTrue(any("claim_verdict" in e for e in errs), errs)

    def test_blank_claim_reason_is_rejected(self):
        led = {"schema": 2, "items": [item(1, claim_reason="   ")], "clusters": []}
        errs = anchor.validate(led)
        self.assertTrue(any("claim_reason" in e for e in errs), errs)

    def test_refuted_item_needs_no_claim_verdict(self):
        """Same exemption as the fix: a REFUTED finding's claim reaches nobody."""
        led = {"schema": 2, "items": [item(1, verdict="REFUTED")], "clusters": []}
        for k in ("claim_verdict", "claim_reason", "fix_verdict", "fix_reason"):
            del led["items"][0][k]
        self.assertEqual(anchor.validate(led), [])

    def test_publishing_verdicts_all_require_a_claim_verdict(self):
        """The claim leads the inline comment for DOWNGRADED and INCONCLUSIVE
        findings exactly as it does for CONFIRMED ones."""
        for v in ("CONFIRMED", "DOWNGRADED", "INCONCLUSIVE"):
            led = {"schema": 2, "items": [item(1, verdict=v)], "clusters": []}
            del led["items"][0]["claim_verdict"]
            errs = anchor.validate(led)
            self.assertTrue(any("claim_verdict" in e for e in errs),
                            f"{v} publishes, so it must require a claim verdict; got {errs}")


class TestFixSlotHedges(unittest.TestCase):
    """A hedged fix hands the author a decision the review declined to make.
    Where a concrete edit cannot be verified, the contract asks for the
    constraint any fix must satisfy - which is still imperative."""

    def _detail(self, fix):
        return f"**What happens:** x\n\n**Why it matters:** y\n\n**Fix:** {fix}"

    def test_hedged_fix_is_rejected_and_names_the_phrase(self):
        led = {"schema": 2, "items": [item(1, detail=self._detail(
            "consider whether the entries should be copied"))], "clusters": []}
        errs = anchor.validate(led)
        self.assertTrue(any("hedged" in e for e in errs), errs)
        self.assertTrue(any("consider whether" in e for e in errs), errs)

    def test_imperative_fix_passes(self):
        led = {"schema": 2, "items": [item(1, detail=self._detail(
            "Copy each entry with a spread and a fresh `submenu` array in "
            "`menu.service.ts:220`, preserving the `click` closures."))], "clusters": []}
        self.assertEqual(anchor.validate(led), [])

    def test_hedge_is_matched_case_insensitively(self):
        led = {"schema": 2, "items": [item(1, detail=self._detail("Perhaps widen the guard."))],
               "clusters": []}
        self.assertTrue(any("hedged" in e for e in anchor.validate(led)))

    def test_hedge_elsewhere_in_detail_does_not_trip_the_check(self):
        """Scoped to the Fix slot. `Why it matters:` legitimately discusses what
        a reader might consider; only the prescription has to be unhedged."""
        detail = ("**What happens:** x\n\n**Why it matters:** a reader may want "
                  "to consider whether this path is hot.\n\n**Fix:** Widen the "
                  "guard at `a.ts:10`.")
        led = {"schema": 2, "items": [item(1, detail=detail)], "clusters": []}
        self.assertEqual(anchor.validate(led), [])

    def test_detail_without_a_fix_slot_is_not_linted(self):
        """This is a lint over the fix text, not an enforcement of the slot's
        presence - a ledger predating the slots still validates."""
        led = {"schema": 2, "items": [item(1, detail="consider whether to do something")],
               "clusters": []}
        self.assertEqual(anchor.validate(led), [])

    def test_refuted_item_may_carry_a_hedged_fix(self):
        led = {"schema": 2, "items": [item(1, verdict="REFUTED",
                              detail=self._detail("perhaps revisit this"))], "clusters": []}
        self.assertEqual(anchor.validate(led), [])

    def test_no_fix_is_needed_is_a_hedge(self):
        """The real case: a Fix slot reading 'none is needed if the trade
        stands' asserts a decision the author still has to make."""
        led = {"schema": 2, "items": [item(1, detail=self._detail(
            "none is needed if the trade stands"))], "clusters": []}
        self.assertTrue(any("hedged" in e for e in anchor.validate(led)))


class TestFixSlotExtraction(unittest.TestCase):
    def test_returns_text_after_the_marker(self):
        self.assertEqual(anchor.fix_slot("**What happens:** a\n\n**Fix:** do the thing"),
                         "do the thing")

    def test_returns_empty_without_a_marker(self):
        self.assertEqual(anchor.fix_slot("**What happens:** a"), "")

    def test_returns_empty_for_missing_detail(self):
        self.assertEqual(anchor.fix_slot(None), "")
        self.assertEqual(anchor.fix_slot(""), "")


class TestBodyOnlyVerdicts(unittest.TestCase):
    """Placement follows ANCHORING, not verdict. DOWNGRADED and INCONCLUSIVE
    post inline like anything else and carry their status in the header; only
    an unanchorable finding falls back to the body. --body-only-verdicts is the
    opt-in that restores the old verdict-based routing."""

    def _led(self, verdict):
        return {"schema": 2, "items": [
            item(1, sev="blocker", f="a.ts", line=10, claim="real one"),
            item(2, sev="low", f="b.ts", line=20, claim="smaller one",
                 verdict=verdict, verdict_reason="unreachable in production"),
        ], "clusters": []}

    def _comments(self, p):
        return " ".join(c["body"] for c in p["comments"])

    def test_downgraded_posts_inline_by_default(self):
        p = anchor.build_payload(self._led("DOWNGRADED"), ["all"], "intro")
        self.assertIn("smaller one", self._comments(p))

    def test_inconclusive_posts_inline_by_default(self):
        p = anchor.build_payload(self._led("INCONCLUSIVE"), ["all"], "intro")
        self.assertIn("smaller one", self._comments(p))

    def test_refuted_by_contrast_is_dropped_from_body_and_comments(self):
        p = anchor.build_payload(self._led("REFUTED"), ["all"], "intro")
        self.assertNotIn("smaller one", p["body"])
        self.assertNotIn("smaller one", self._comments(p))

    def test_an_inline_comment_states_its_verdict(self):
        """Placement no longer signals status, so the label must - otherwise a
        downgraded finding reads as an unqualified demand for a change."""
        p = anchor.build_payload(self._led("DOWNGRADED"), ["all"], "intro")
        self.assertIn(anchor.VERDICT_LABEL["DOWNGRADED"], self._comments(p))

    def test_an_inline_comment_carries_a_confirmed_finding_s_reason(self):
        """A CONFIRMED verdict's reasoning is evidence for the claim, so it
        publishes - under a label a PR author can read without a glossary."""
        p = anchor.build_payload(self._led("DOWNGRADED"), ["all"], "intro")
        self.assertIn("_How this was checked: r_", self._comments(p))

    def test_a_downgrade_publishes_no_reason_footer(self):
        """"Why this was lowered" explains an internal revision the author
        never saw. Anything in it they DO need is folded into the finding's own
        `Why it matters:` slot at drafting time, not appended as bookkeeping."""
        p = anchor.build_payload(self._led("DOWNGRADED"), ["all"], "intro")
        item_2 = [c for c in p["comments"] if c["path"] == "b.ts"][0]["body"]
        self.assertIn("smaller one", item_2)
        self.assertNotIn("unreachable in production", item_2)
        self.assertNotIn("Verdict:", item_2)

    def test_inline_shows_only_the_final_severity_when_the_refuter_changed_it(self):
        led = self._led("DOWNGRADED")
        led["items"][1]["severity_in"] = "high"
        p = anchor.build_payload(led, ["all"], "intro")
        item_2 = [c for c in p["comments"] if c["path"] == "b.ts"][0]["body"]
        self.assertNotIn("high", item_2)
        self.assertIn("low", item_2)

    def test_no_published_surface_carries_a_severity_arrow(self):
        """The arrow is the S6 triage table's job. One assertion over the whole
        payload, so a new render site cannot reintroduce it unnoticed."""
        led = self._led("DOWNGRADED")
        led["items"][1]["severity_in"] = "high"
        led["items"][1]["on_changed_line"] = False
        p = anchor.build_payload(led, ["all"], "intro")
        self.assertNotIn(" -> ", p["body"] + self._comments(p))

    def test_inline_shows_one_severity_when_the_refuter_left_it_alone(self):
        p = anchor.build_payload(self._led("DOWNGRADED"), ["all"], "intro")
        self.assertNotIn("low -> low", self._comments(p))

    def test_an_inconclusive_finding_still_says_what_could_not_be_checked(self):
        """Unlike a downgrade, this reasoning is about the code: it tells the
        author what they have to judge themselves."""
        p = anchor.build_payload(self._led("INCONCLUSIVE"), ["all"], "intro")
        self.assertIn("_What could not be checked: unreachable in production_",
                      self._comments(p))

    def test_an_off_diff_downgrade_falls_back_to_the_body(self):
        """Anchoring is the only thing that moves a finding out of comments[],
        and the body must say it was the anchor, not the verdict."""
        led = self._led("DOWNGRADED")
        led["items"][1]["on_changed_line"] = False
        p = anchor.build_payload(led, ["all"], "intro")
        _, _, after = p["body"].partition("## Findings that could not be attached")
        self.assertIn("smaller one", after)
        self.assertNotIn("smaller one", self._comments(p))

    def test_an_unanchored_body_finding_still_carries_its_verdict_label(self):
        led = self._led("DOWNGRADED")
        led["items"][1]["on_changed_line"] = False
        p = anchor.build_payload(led, ["all"], "intro")
        self.assertIn(anchor.VERDICT_LABEL["DOWNGRADED"], p["body"])

    def test_the_body_drops_a_downgrade_s_reason_the_same_way_inline_does(self):
        """The demoted section renders the same finding through a second code
        path; the two must agree or a finding's wording changes with its
        anchoring."""
        led = self._led("DOWNGRADED")
        led["items"][1]["on_changed_line"] = False
        p = anchor.build_payload(led, ["all"], "intro")
        self.assertIn("smaller one", p["body"])
        self.assertNotIn("unreachable in production", p["body"])

    def test_the_opt_in_body_section_drops_a_downgrade_s_reason_too(self):
        """Third render path for the same finding - --body-only-verdicts."""
        led = self._led("DOWNGRADED")
        p = anchor.build_payload(led, ["all"], "intro",
                                 body_only_verdicts=["DOWNGRADED"])
        self.assertIn("smaller one", p["body"])
        self.assertNotIn("unreachable in production", p["body"])

    def test_body_only_verdicts_opt_in_restores_verdict_routing(self):
        p = anchor.build_payload(self._led("DOWNGRADED"), ["all"], "intro",
                                 body_only_verdicts=["DOWNGRADED"])
        heading = anchor.BODY_ONLY_VERDICTS["DOWNGRADED"][0]
        self.assertIn(heading, p["body"])
        self.assertIn("smaller one", p["body"])
        self.assertNotIn("smaller one", self._comments(p))

    def test_body_only_opt_in_leaves_other_verdicts_inline(self):
        p = anchor.build_payload(self._led("INCONCLUSIVE"), ["all"], "intro",
                                 body_only_verdicts=["DOWNGRADED"])
        self.assertIn("smaller one", self._comments(p))

    def test_a_downgraded_only_selection_now_produces_a_comment(self):
        p = anchor.build_payload(self._led("DOWNGRADED"), ["2"], "intro")
        self.assertEqual(len(p["comments"]), 1)

    def test_same_file_findings_cross_reference_each_other(self):
        """The payoff for anchoring-based placement: a reader in one file sees
        what else lands in it without reading the whole review."""
        led = {"schema": 2, "items": [
            item(1, sev="high", f="a.ts", line=10, claim="first"),
            item(2, sev="low", f="a.ts", line=20, claim="second"),
            item(3, sev="low", f="b.ts", line=30, claim="elsewhere"),
        ], "clusters": []}
        p = anchor.build_payload(led, ["all"], "intro")
        by_line = {c["line"]: c["body"] for c in p["comments"]}
        self.assertIn("Other findings in this file: #2", by_line[10])
        self.assertIn("Other findings in this file: #1", by_line[20])
        self.assertNotIn("Other findings in this file", by_line[30])

    def test_include_refuted_does_not_accept_a_downgraded_id(self):
        led = self._led("DOWNGRADED")
        with self.assertRaises(anchor.IncludeRefutedError):
            anchor.resolve_include_refuted(led, ["2"])

    def test_attribution_stays_last_after_a_body_only_section(self):
        p = anchor.build_payload(self._led("DOWNGRADED"), ["all"], "intro")
        self.assertTrue(p["body"].rstrip().endswith(anchor.ATTRIBUTION))
        self.assertEqual(p["body"].count(anchor.ATTRIBUTION), 1)
