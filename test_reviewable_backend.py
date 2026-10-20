import unittest, importlib.util, os, json, io, contextlib, subprocess, sys, tempfile
from types import SimpleNamespace
spec = importlib.util.spec_from_file_location(
    "anchor", os.path.join(os.path.dirname(__file__), "pr-review-anchor.py"))
anchor = importlib.util.module_from_spec(spec); spec.loader.exec_module(anchor)

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "pr-review-anchor.py")
NOTE = ("_This comment is about the code on this line, which this pull request "
        "does not change._")


def item(i, sev="high", f="a.ts", line=10, cluster=None, **kw):
    d = {"id": i, "severity": sev, "severity_in": sev, "file": f, "line": line,
         "line_end": line, "anchor_excerpt": "x", "claim": f"claim {i}", "detail": "d",
         "cluster": cluster, "verdict": "CONFIRMED", "verdict_reason": "r",
         "claim_verdict": "SOUND", "claim_reason": "matched at a.ts:10",
         "fix_verdict": "SOUND", "fix_reason": "checked at a.ts:10"}
    d.update(kw); return d


def off(i, **kw):
    """An item reanchor found outside the diff."""
    kw.setdefault("anchor_status", "off-diff")
    kw.setdefault("on_changed_line", False)
    return item(i, **kw)


def ledger(*items, clusters=()):
    return {"schema": 2, "items": list(items), "clusters": list(clusters)}


def quiet(fn, *a, **kw):
    """Call `fn`, returning (result, stderr)."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        res = fn(*a, **kw)
    return res, err.getvalue()


def reviewable(led, tokens=("all",), intro="intro", **kw):
    return quiet(anchor.build_reviewable, led, list(tokens), intro, **kw)


def github(led, tokens=("all",), intro="intro", **kw):
    return quiet(anchor.build_payload, led, list(tokens), intro, **kw)


def by_ids(art):
    return {tuple(d["item_ids"]): d for d in art["discussions"]}


class TestOffDiffPlacement(unittest.TestCase):
    def test_an_off_diff_finding_posts_as_a_discussion_on_reviewable(self):
        art, _ = reviewable(ledger(item(1), off(2, line=5)))
        d = by_ids(art)[(2,)]
        self.assertEqual((d["path"], d["line"]), ("a.ts", 5))
        self.assertNotIn("**#2**", art["framing"]["markdownBody"])

    def test_the_same_finding_stays_in_the_body_on_github(self):
        payload, _ = github(ledger(item(1), off(2, line=5)))
        self.assertEqual([c["line"] for c in payload["comments"]], [10])
        self.assertIn("**#2**", payload["body"])
        self.assertNotIn(NOTE, payload["body"])

    def test_on_changed_line_false_alone_is_off_diff(self):
        # No anchor_status: a ledger straight from S6's changed-lines.
        led = ledger(item(1), item(2, line=5, on_changed_line=False))
        art, _ = reviewable(led)
        self.assertTrue(by_ids(art)[(2,)]["markdownBody"].startswith(NOTE))

    def test_the_note_opens_an_off_diff_discussion_and_no_other(self):
        art, _ = reviewable(ledger(item(1), off(2, line=5)))
        self.assertTrue(by_ids(art)[(2,)]["markdownBody"].startswith(NOTE + "\n\n**#2**"))
        self.assertNotIn(NOTE, by_ids(art)[(1,)]["markdownBody"])
        self.assertNotIn(NOTE, art["framing"]["markdownBody"])

    def test_the_note_is_the_exact_published_wording(self):
        self.assertEqual(anchor.OFF_DIFF_NOTE, NOTE)

    def test_a_merged_off_diff_site_carries_the_note_once(self):
        art, _ = reviewable(ledger(off(1, line=5), off(2, line=5)))
        self.assertEqual(len(art["discussions"]), 1)
        d = art["discussions"][0]
        self.assertEqual(d["item_ids"], [1, 2])
        self.assertEqual(d["markdownBody"].count(NOTE), 1)
        self.assertTrue(d["markdownBody"].startswith(NOTE))

    def test_on_and_off_diff_findings_on_one_line_stay_separate(self):
        art, _ = reviewable(ledger(item(1), off(2)))
        self.assertEqual([d["item_ids"] for d in art["discussions"]], [[1], [2]])
        self.assertNotIn(NOTE, art["discussions"][0]["markdownBody"])
        self.assertTrue(art["discussions"][1]["markdownBody"].startswith(NOTE))

    def test_a_dead_anchor_is_carried_in_the_framing_comment(self):
        art, _ = reviewable(ledger(item(1), item(2, line=5, anchor_status="dead")))
        self.assertNotIn((2,), by_ids(art))
        body = art["framing"]["markdownBody"]
        self.assertIn("## Findings that could not be attached to a line in this diff", body)
        self.assertIn("**#2**", body)

    def test_an_off_diff_finding_with_no_line_is_carried_in_the_framing_comment(self):
        art, err = reviewable(ledger(item(1), off(2, line=None)))
        self.assertNotIn((2,), by_ids(art))
        self.assertIn("_Not attached to a line: this finding has no file and line to attach a "
                      "comment to_", art["framing"]["markdownBody"])
        self.assertIn("#2 (no file or line)", err)

    def test_a_finding_in_a_file_the_pr_does_not_touch_is_carried_in_the_framing_comment(self):
        art, err = reviewable(ledger(item(1), off(2, f="b.ts", line=5, file_in_diff=False)))
        self.assertEqual([d["item_ids"] for d in art["discussions"]], [[1]])
        body = art["framing"]["markdownBody"]
        self.assertIn("**#2**", body)
        self.assertIn("_Not attached to a line: this finding is in a file the pull request does "
                      "not change, so the review has no file to attach a comment to_", body)
        self.assertIn("#2 (file not in the pull request)", err)
        self.assertNotIn("notice:", err)

    def test_github_places_it_as_it_does_any_off_diff_finding(self):
        led = lambda **kw: ledger(item(1), off(2, f="b.ts", line=5, **kw))
        self.assertEqual(github(led(file_in_diff=False)), github(led()))


class TestCrossReferences(unittest.TestCase):
    def test_an_off_diff_sibling_is_named_on_reviewable(self):
        led = ledger(item(1, cluster="C1"), off(2, f="b.ts", line=5, cluster="C1"),
                     clusters=[{"id": "C1", "item_ids": [1, 2]}])
        art, _ = reviewable(led)
        self.assertIn("Similar fix as #2.", by_ids(art)[(1,)]["markdownBody"])
        self.assertIn("Similar fix as #1.", by_ids(art)[(2,)]["markdownBody"])

    def test_an_off_diff_finding_in_the_same_file_is_named_on_reviewable(self):
        art, _ = reviewable(ledger(item(1), off(2, line=20)))
        self.assertIn("_Other findings in this file: #2_", by_ids(art)[(1,)]["markdownBody"])

    def test_a_sibling_on_the_same_line_is_not_elsewhere(self):
        led = ledger(item(1, cluster="C1"), off(2, cluster="C1"),
                     item(3, f="b.ts", line=1, cluster="C1"),
                     clusters=[{"id": "C1", "item_ids": [1, 2, 3]}])
        art, _ = reviewable(led)
        self.assertIn("Similar fix as #3.", by_ids(art)[(1,)]["markdownBody"])

    def test_github_names_neither(self):
        led = ledger(item(1, cluster="C1"), off(2, line=20, cluster="C1"),
                     clusters=[{"id": "C1", "item_ids": [1, 2]}])
        payload, _ = github(led)
        self.assertNotIn("Similar fix", payload["comments"][0]["body"])
        self.assertNotIn("Other findings", payload["comments"][0]["body"])


class TestDispositions(unittest.TestCase):
    def test_each_finding_maps_as_measured(self):
        cases = [("blocker", "CONFIRMED", "blocking"), ("blocker", "DOWNGRADED", "blocking"),
                 ("high", "CONFIRMED", "discussing"), ("medium", "CONFIRMED", "discussing"),
                 ("low", "DOWNGRADED", "discussing"), ("blocker", "INCONCLUSIVE", "discussing"),
                 ("low", "INCONCLUSIVE", "discussing"), ("blocker", "REFUTED", "informing"),
                 ("low", "REFUTED", "informing")]
        for sev, verdict, want in cases:
            with self.subTest(sev=sev, verdict=verdict):
                self.assertEqual(anchor.finding_disposition(sev, verdict), want)

    def site(self, *pairs):
        return anchor.Site(path="a.ts", line=1, body="b", findings=tuple(
            anchor.SiteFinding(item_id=n, severity=s, verdict=v)
            for n, (s, v) in enumerate(pairs, start=1)))

    def test_a_merged_site_takes_its_strongest(self):
        self.assertEqual(anchor.site_disposition(self.site(("low", "CONFIRMED"),
                                                           ("blocker", "CONFIRMED"))), "blocking")
        self.assertEqual(anchor.site_disposition(self.site(("blocker", "REFUTED"),
                                                           ("low", "CONFIRMED"))), "discussing")

    def test_a_site_with_no_findings_takes_the_weakest(self):
        self.assertEqual(anchor.site_disposition(self.site()), "informing")

    def test_the_framing_comment_is_informing(self):
        art, _ = reviewable(ledger(item(1, sev="blocker")))
        self.assertEqual(art["framing"]["disposition"], "informing")

    def test_a_dead_anchor_blocker_makes_the_framing_comment_blocking(self):
        art, _ = reviewable(ledger(item(1), item(2, sev="blocker", line=5, anchor_status="dead"),
                                   item(3, sev="low", line=7, anchor_status="dead")))
        self.assertIn("**#2**", art["framing"]["markdownBody"])
        self.assertEqual(art["framing"]["disposition"], "blocking")

    def test_the_framing_comment_takes_the_strongest_finding_it_carries(self):
        cases = [
            # A body-only verdict is carried there too.
            (ledger(item(1), item(2, verdict="INCONCLUSIVE")),
             {"body_only_verdicts": ["INCONCLUSIVE"]}, "discussing"),
            (ledger(item(1), item(2, sev="blocker", verdict="DOWNGRADED"),
                    item(3, line=7, anchor_status="dead")),
             {"body_only_verdicts": ["DOWNGRADED"]}, "blocking"),
            # A finding in a file the pull request does not change.
            (ledger(item(1), off(2, f="b.ts", line=5, file_in_diff=False)), {}, "discussing"),
            # A re-admitted REFUTED finding asks nothing, even a blocker.
            (ledger(item(1), item(2, sev="blocker", verdict="REFUTED", anchor_status="dead")),
             {"include_refuted": ["2"]}, "informing"),
        ]
        for led, kw, want in cases:
            with self.subTest(kw=kw, want=want):
                art, _ = reviewable(led, **kw)
                self.assertIn("**#2**", art["framing"]["markdownBody"])
                self.assertEqual(art["framing"]["disposition"], want)

    def test_the_artifact_carries_them_end_to_end(self):
        led = ledger(item(1, sev="blocker"), item(2, sev="blocker", f="b.ts",
                                                  verdict="INCONCLUSIVE"),
                     item(3, sev="blocker", f="c.ts", verdict="REFUTED"), item(4, f="d.ts"))
        art, _ = reviewable(led, include_refuted=["3"])
        got = {d["item_ids"][0]: d["disposition"] for d in art["discussions"]}
        self.assertEqual(got, {1: "blocking", 2: "discussing", 3: "informing", 4: "discussing"})


class TestFramingIds(unittest.TestCase):
    """The framing comment names the findings it carries, so the posting step
    can account for them and link them as it does a discussion's."""

    def test_a_dead_anchor_finding_is_named_by_the_framing_comment(self):
        art, _ = reviewable(ledger(item(1), item(2, line=5, anchor_status="dead"),
                                   off(3, f="b.ts", line=7, file_in_diff=False)))
        self.assertEqual(art["framing"]["item_ids"], [2, 3])
        self.assertEqual(by_ids(art).keys(), {(1,)})

    def test_the_list_is_empty_when_nothing_is_body_bound(self):
        art, _ = reviewable(ledger(item(1), off(2, line=5)))
        self.assertEqual(art["framing"]["item_ids"], [])

    def test_the_preview_header_names_them_only_when_there_are_some(self):
        for led, want in ((ledger(item(1), item(2, line=5, anchor_status="dead")),
                           "REVIEW FRAMING COMMENT - #2\n"),
                          (ledger(item(1)), "REVIEW FRAMING COMMENT\n")):
            with self.subTest(want=want):
                art, _ = reviewable(led)
                self.assertIn(want, anchor.render_preview(art))


class TestArtifactShape(unittest.TestCase):
    def test_path_and_line_only(self):
        art, _ = reviewable(ledger(item(1)))
        self.assertEqual(set(art), {"provider", "framing", "discussions"})
        self.assertEqual(art["provider"], "reviewable")
        self.assertEqual(set(art["framing"]), {"markdownBody", "disposition", "item_ids"})
        self.assertEqual(set(art["discussions"][0]),
                         {"path", "line", "markdownBody", "disposition", "item_ids"})

    def test_the_text_is_the_github_text_where_the_backends_agree(self):
        led = ledger(item(1), item(2), item(3, f="b.ts"), item(4, line=5, anchor_status="dead"))
        art, _ = reviewable(led, intro="Reviewed {count}.")
        payload, _ = github(led, intro="Reviewed {count}.")
        self.assertEqual(art["framing"]["markdownBody"], payload["body"])
        self.assertEqual([d["markdownBody"] for d in art["discussions"]],
                         [c["body"] for c in payload["comments"]])

    def test_count_includes_off_diff_findings(self):
        art, _ = reviewable(ledger(item(1), off(2, line=5)), intro="Reviewed {count}.")
        self.assertTrue(art["framing"]["markdownBody"].startswith("Reviewed 2."))

    def test_attribution_is_in_the_framing_comment_only(self):
        art, _ = reviewable(ledger(item(1), off(2, line=5)))
        self.assertTrue(art["framing"]["markdownBody"].endswith(anchor.ATTRIBUTION))
        self.assertFalse(any(anchor.ATTRIBUTION in d["markdownBody"] for d in art["discussions"]))

    def test_a_line_reviewable_cannot_take_is_refused_before_approval(self):
        for bad in (0, -2, True, "10"):
            with self.subTest(line=bad):
                with self.assertRaises(anchor.PayloadError) as ctx:
                    reviewable(ledger(item(1, line=bad)))
                self.assertIn("#1", str(ctx.exception))

    def test_a_string_line_beside_an_integer_one_is_refused_not_crashed(self):
        for second in (off(2, line="3"), item(2, line="3")):
            with self.subTest(second=second.get("anchor_status")):
                with self.assertRaises(anchor.PayloadError) as ctx:
                    reviewable(ledger(item(1, line=10), second))
                self.assertIn("#2", str(ctx.exception))

    def test_an_empty_selection_is_still_fatal(self):
        with self.assertRaises(anchor.PayloadError):
            reviewable(ledger(item(1, verdict="REFUTED")))

    def test_github_refuses_a_line_that_is_not_a_positive_integer(self):
        for led in (ledger(item(1, line=0)), ledger(item(1, line=True)),
                    ledger(item(1, line=10), item(2, line="3"))):
            with self.subTest(lines=[it["line"] for it in led["items"]]):
                with self.assertRaises(anchor.PayloadError) as ctx:
                    github(led)
                self.assertIn(f"#{led['items'][-1]['id']}", str(ctx.exception))
                self.assertIn("not a positive integer", str(ctx.exception))


class TestStderr(unittest.TestCase):
    LED = staticmethod(lambda: ledger(item(1), off(2, line=5), item(3, line=7, anchor_status="dead")))

    def test_reviewable_accounts_in_its_own_terms(self):
        _, err = reviewable(self.LED())
        self.assertEqual(err.splitlines(), [
            "warning: 1 selected item(s) moved into the review-level comment, which publishes "
            "resolved only when nothing in it asks for a reply (no location in this pull request "
            "to attach a comment to) - #3 (dead anchor)",
            "notice: 1 selected item(s) are not on a line this PR changes and post inline, each "
            "site opening with a note saying so - #2"])
        _, err = reviewable(ledger(item(1), item(2, verdict="INCONCLUSIVE")),
                            body_only_verdicts=["INCONCLUSIVE"])
        self.assertEqual(err.splitlines(), [
            "notice: 1 selected item(s) carried in the review-level comment rather than posted "
            "inline, which publishes resolved only when nothing in it asks for a reply - "
            "#2 (INCONCLUSIVE)"])

    def test_github_wording_is_unchanged(self):
        _, err = github(self.LED())
        self.assertEqual(err.splitlines(), [
            "warning: 2 selected item(s) moved into the review body (cannot be anchored to a "
            "changed line) - #2 (not on a changed line), #3 (dead anchor)"])
        _, err = github(ledger(item(1), item(2, verdict="INCONCLUSIVE")),
                        body_only_verdicts=["INCONCLUSIVE"])
        self.assertEqual(err.splitlines(), [
            "notice: 1 selected item(s) carried in the review body rather than posted inline - "
            "#2 (INCONCLUSIVE)"])


class TestFileInDiff(unittest.TestCase):
    """Both steps that read the diff record whether each finding's file is in
    it, including a file whose hunks add no line."""
    DIFF = ("diff --git a/a.ts b/a.ts\n--- a/a.ts\n+++ b/a.ts\n@@ -1,1 +1,2 @@\n+new\n one\n"
            "diff --git a/c.ts b/c.ts\n--- a/c.ts\n+++ b/c.ts\n@@ -1,2 +1,1 @@\n-gone\n one\n")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wt = self.tmp.name
        for name, text in (("a.ts", "new\none\n"), ("b.ts", "one\n"), ("c.ts", "one\n")):
            with open(os.path.join(self.wt, name), "w", encoding="utf-8") as fh:
                fh.write(text)
        self.items = [item(1, f="a.ts", line=1, anchor_excerpt="new"),
                      item(2, f="b.ts", line=1, anchor_excerpt="one"),
                      item(3, f="c.ts", line=1, anchor_excerpt="one")]

    def tearDown(self):
        self.tmp.cleanup()

    def recorded(self, led):
        return {it["id"]: it["file_in_diff"] for it in led["items"]}

    def write(self, items, diff):
        lp, dp = os.path.join(self.wt, "ledger.json"), os.path.join(self.wt, "head.diff")
        with open(lp, "w", encoding="utf-8") as fh:
            json.dump(ledger(*items), fh)
        with open(dp, "w", encoding="utf-8", newline="") as fh:
            fh.write(diff)
        return lp, dp

    def changed_lines(self, items, diff):
        lp, dp = self.write(items, diff)
        args = SimpleNamespace(ledger=lp, diff=dp, worktree=self.wt)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(anchor.cmd_changed_lines(args), 0)
        with open(lp, encoding="utf-8") as fh:
            return json.load(fh)

    def reanchor(self, items, diff):
        lp, dp = self.write(items, diff)
        args = SimpleNamespace(ledger=lp, diff=dp, worktree=self.wt, head=None, dry_run=False)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(anchor.cmd_reanchor(args), 0)
        with open(lp, encoding="utf-8") as fh:
            return json.load(fh)

    def test_changed_lines_records_it(self):
        led = self.changed_lines(self.items, self.DIFF)
        self.assertEqual(self.recorded(led), {1: True, 2: False, 3: True})

    def test_reanchor_records_it(self):
        led = self.reanchor(self.items, self.DIFF)
        self.assertEqual(self.recorded(led), {1: True, 2: False, 3: True})

    # Each prints no `+++` line: git's own output for b.ts, from `git diff -M`.
    NO_HUNK = {
        "rename only": ("diff --git a/old.ts b/b.ts\nsimilarity index 100%\n"
                        "rename from old.ts\nrename to b.ts\n"),
        "mode only": "diff --git a/b.ts b/b.ts\nold mode 100644\nnew mode 100755\n",
        "binary": ("diff --git a/b.ts b/b.ts\nindex 1111111..2222222 100644\n"
                   "Binary files a/b.ts and b/b.ts differ\n"),
    }

    def test_a_file_with_no_hunk_is_in_the_diff(self):
        for name, part in self.NO_HUNK.items():
            for step in (self.changed_lines, self.reanchor):
                with self.subTest(diff=name, step=step.__name__):
                    led = step(self.items, self.DIFF + part)
                    self.assertEqual(self.recorded(led), {1: True, 2: True, 3: True})
                    self.assertFalse(next(it for it in led["items"]
                                          if it["id"] == 2)["on_changed_line"])

    def test_a_bare_carriage_return_in_the_diff_file_is_content(self):
        # Read with universal newlines, the "\r" would end the added line and
        # the "+++ b/b.ts" after it would read as a header naming b.ts.
        part = "diff --git a/d.ts b/d.ts\n--- a/d.ts\n+++ b/d.ts\n@@ -0,0 +1 @@\n+x\r+++ b/b.ts\n"
        for step in (self.changed_lines, self.reanchor):
            with self.subTest(step=step.__name__):
                led = step(self.items, self.DIFF + part)
                self.assertEqual(self.recorded(led), {1: True, 2: False, 3: True})

    def test_a_file_with_no_hunk_posts_as_a_discussion_on_reviewable(self):
        led = self.reanchor(self.items, self.DIFF + self.NO_HUNK["rename only"])
        art, _ = reviewable(led)
        d = by_ids(art)[(2,)]
        self.assertEqual((d["path"], d["line"]), ("b.ts", 1))
        self.assertTrue(d["markdownBody"].startswith(NOTE))

    def test_a_deleted_file_is_not_in_the_diff(self):
        gone = ("diff --git a/b.ts b/b.ts\ndeleted file mode 100644\nindex 1111111..0000000\n"
                "--- a/b.ts\n+++ /dev/null\n@@ -1 +0,0 @@\n-one\n")
        self.assertNotIn("b.ts", anchor.lt.diff_files(self.DIFF + gone))

    def test_the_line_map_keeps_only_files_with_hunks(self):
        diff = self.DIFF + "".join(self.NO_HUNK.values())
        self.assertEqual(anchor.parse_changed_lines(diff), {"a.ts": {1}, "c.ts": set()})
        self.assertEqual(anchor.lt.diff_files(diff), {"a.ts", "b.ts", "c.ts"})


class TestQuotedPaths(unittest.TestCase):
    """Git C-quotes a path with a non-ASCII byte, a control character, a quote
    or a backslash. Every reader of the diff decodes it to the plain path a
    ledger records. On GitHub this is a deliberate change: such a finding used
    to be judged off the diff and moved into the review body."""
    CAFE = "caf\u00e9.ts"
    DIFF = ('diff --git "a/caf\\303\\251.ts" "b/caf\\303\\251.ts"\n'
            'index 1111111..2222222 100644\n'
            '--- "a/caf\\303\\251.ts"\n'
            '+++ "b/caf\\303\\251.ts"\n'
            "@@ -1,1 +1,2 @@\n+new\n one\n")

    def test_the_key_is_the_plain_path(self):
        self.assertIn('+++ "b/caf\\303\\251.ts"', self.DIFF)
        self.assertEqual(anchor.parse_changed_lines(self.DIFF), {self.CAFE: {1}})
        self.assertEqual(anchor.lt.diff_files(self.DIFF), {self.CAFE})

    def test_every_escape_git_writes_is_decoded(self):
        cases = {'"b/caf\\303\\251.ts"': "b/" + self.CAFE,
                 '"a\\tb\\nc\\"d\\\\e.ts"': 'a\tb\nc"d\\e.ts',
                 "b/plain name.ts": "b/plain name.ts", "/dev/null": "/dev/null"}
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(anchor.lt.git_path(raw), want)

    def test_a_quoted_rename_target_is_in_the_diff(self):
        diff = ('diff --git a/old.ts "b/caf\\303\\251.ts"\nsimilarity index 100%\n'
                'rename from old.ts\nrename to "caf\\303\\251.ts"\n')
        self.assertEqual(anchor.lt.diff_files(diff), {self.CAFE})

    def test_changed_lines_puts_the_finding_on_its_line_on_github(self):
        led = ledger(item(1, f=self.CAFE, line=1, anchor_excerpt="new"))
        with tempfile.TemporaryDirectory() as wt:
            with open(os.path.join(wt, self.CAFE), "w", encoding="utf-8") as fh:
                fh.write("new\none\n")
            anchor.lt.mark_changed_lines(led, wt, anchor.parse_changed_lines(self.DIFF),
                                         anchor.lt.diff_files(self.DIFF))
        self.assertEqual((led["items"][0]["on_changed_line"], led["items"][0]["file_in_diff"]),
                         (True, True))
        payload, err = github(led)
        self.assertEqual([(c["path"], c["line"]) for c in payload["comments"]], [(self.CAFE, 1)])
        self.assertEqual(err, "")


# Every character but "\n" that str.splitlines breaks a line at.
SEPARATORS = "\r\v\f\x1c\x1d\x1e\x85\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}"


class TestHunkCounts(unittest.TestCase):
    """A line inside a hunk is content, whatever it starts with: the hunk
    header's counts say where its body ends."""

    @staticmethod
    def diff(header, *body):
        return "\n".join(["diff --git a/x.md b/x.md", "--- a/x.md", "+++ b/x.md", header,
                          *body]) + "\n"

    def test_an_added_line_starting_plus_plus_is_content(self):
        diff = self.diff("@@ -1,2 +1,6 @@", " a", "+++ y", "+b", "+c", "+d", " e")
        self.assertEqual(anchor.lt.parse_changed_lines(diff), {"x.md": {2, 3, 4, 5}})
        self.assertEqual(anchor.lt.diff_files(diff), {"x.md"})

    def test_an_omitted_count_is_one(self):
        diff = self.diff("@@ -0,0 +1 @@", "+++ y")
        self.assertEqual(anchor.lt.parse_changed_lines(diff), {"x.md": {1}})
        self.assertEqual(anchor.lt.diff_files(diff), {"x.md"})

    def test_a_no_newline_marker_spends_neither_count(self):
        diff = self.diff("@@ -1 +1,2 @@", "-old", "\\ No newline at end of file", "+old",
                         "+++ y")
        self.assertEqual(anchor.lt.parse_changed_lines(diff), {"x.md": {1, 2}})
        self.assertEqual(anchor.lt.diff_files(diff), {"x.md"})

    def test_a_hunk_that_overstates_its_counts_still_ends_at_the_next_file(self):
        diff = self.diff("@@ -1 +1,3 @@", " a", "+++ y") + self.diff(
            "@@ -1 +1 @@", "-a", "+b").replace("x.md", "z.md")
        self.assertEqual(anchor.lt.parse_changed_lines(diff), {"x.md": {2}, "z.md": {1}})

    def test_only_a_newline_ends_a_line(self):
        for ch in SEPARATORS:
            for header, body, want in (
                    ("@@ -1,2 +1,3 @@", (f" a{ch} b", "+c", " d"), {2}),
                    ("@@ -1,2 +1,4 @@", (" a", f"+b{ch}+++ zz", "+c", " d"), {2, 3}),
                    ("@@ -0,0 +1 @@", (f"+b{ch}+++ zz",), {1})):
                with self.subTest(ch=ch, body=body):
                    diff = self.diff(header, *body)
                    self.assertEqual(anchor.lt.parse_changed_lines(diff), {"x.md": want})
                    self.assertEqual(anchor.lt.diff_files(diff), {"x.md"})

    def test_a_diff_saved_with_crlf_endings_reads_the_same(self):
        diff = self.diff("@@ -1,2 +1,3 @@", " a", "+b", " c") + (
            "diff --git a/old b/new\nsimilarity index 100%\nrename from old\nrename to new\n")
        crlf = diff.replace("\n", "\r\n")
        self.assertEqual(anchor.lt.parse_changed_lines(crlf), {"x.md": {2}})
        self.assertEqual(anchor.lt.diff_files(crlf), {"x.md", "new"})


class TestLinesAsGitNumbersThem(unittest.TestCase):
    """The worktree, an excerpt and the diff number lines alike, at "\n"
    alone, so a finding on a changed line is judged on it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wt = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text):
        with open(os.path.join(self.wt, "x.py"), "w", encoding="utf-8", newline="") as fh:
            fh.write(text)

    def test_split_lines_ends_lines_at_newline_alone(self):
        for text, want in (("", []), ("a", ["a"]), ("a\n", ["a"]), ("a\n\n", ["a", ""]),
                           ("a\r\nb\r\n", ["a", "b"]), ("a\rb\n", ["a\rb"])):
            with self.subTest(text=text):
                self.assertEqual(anchor.lt.split_lines(text), want)

    def test_a_separator_inside_a_line_keeps_the_lines_after_it_in_place(self):
        for ch in SEPARATORS:
            with self.subTest(ch=ch):
                self.write(f"a{ch}b\nc\nNEW\n")
                self.assertEqual(anchor.lt.read_lines(self.wt, "x.py", 3, 3), ["NEW"])
                it = item(1, f="x.py", line=3, anchor_excerpt="NEW")
                self.assertEqual(anchor._resolve_anchor(it, self.wt, {"x.py": {3}})[:3],
                                 ("ok", 3, 3))
                held = item(1, f="x.py", line=1, anchor_excerpt=f"a{ch}b")
                self.assertEqual(anchor.lt.excerpt_errors(ledger(held), self.wt), [])
                self.assertEqual(anchor._resolve_anchor(held, self.wt, {"x.py": {1}})[:3],
                                 ("ok", 1, 1))

    def test_an_excerpt_line_ending_in_dots_after_a_separator_is_not_an_ellipsis(self):
        for ch in SEPARATORS:
            with self.subTest(ch=ch):
                errs = anchor.validate(ledger(item(1, anchor_excerpt=f"x = 1{ch}...")))
                self.assertFalse([e for e in errs if "ellipsis" in e], errs)

    def test_a_crlf_file_reads_without_its_carriage_returns(self):
        self.write("a\r\nb\r\n")
        self.assertEqual(anchor.lt.read_lines(self.wt, "x.py", 1, 2), ["a", "b"])

    def test_changed_lines_puts_the_finding_on_its_changed_line(self):
        # The space after the separator makes the split-off tail read as a
        # context line, which is what pushed every later line down by one.
        for ch in SEPARATORS:
            with self.subTest(ch=ch):
                self.write(f"a{ch} b\nc\nNEW\n")
                diff = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
                        f"@@ -1,2 +1,3 @@\n a{ch} b\n c\n+NEW\n")
                lp, dp = os.path.join(self.wt, "l.json"), os.path.join(self.wt, "h.diff")
                with open(lp, "w", encoding="utf-8") as fh:
                    json.dump(ledger(item(1, f="x.py", line=3, anchor_excerpt="NEW")), fh)
                with open(dp, "w", encoding="utf-8", newline="") as fh:
                    fh.write(diff)
                args = SimpleNamespace(ledger=lp, diff=dp, worktree=self.wt)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(anchor.cmd_changed_lines(args), 0)
                with open(lp, encoding="utf-8") as fh:
                    got = json.load(fh)["items"][0]
                self.assertEqual((got["line"], got["on_changed_line"]), (3, True))


class TestReportLabel(unittest.TestCase):
    def test_the_count_is_labelled_as_githubs_placement(self):
        text = anchor.lt.render_report(ledger(item(1), off(2, line=5)), "/s/l.json")
        self.assertIn("# on GitHub: 1 inline + 1 in the body", text)


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.ledger = os.path.join(self.dir, "ledger.json")
        with open(self.ledger, "w", encoding="utf-8") as fh:
            json.dump(ledger(item(1, sev="blocker"), item(2, sev="blocker"), off(3, line=5)), fh)
        self.body = os.path.join(self.dir, "body.md")
        with open(self.body, "w", encoding="utf-8") as fh:
            fh.write("intro")

    def tearDown(self):
        self.tmp.cleanup()

    def payload(self, **kw):
        out, err = io.StringIO(), io.StringIO()
        args = SimpleNamespace(ledger=self.ledger, select=None, body_file=self.body,
                               include_refuted=None, **kw)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = anchor.cmd_payload(args)
        return code, out.getvalue()

    def test_provider_reviewable_writes_the_artifact(self):
        code, out = self.payload(provider="reviewable")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["provider"], "reviewable")

    def test_the_default_provider_is_github(self):
        # The golden is master's output for a ledger that reaches every GitHub
        # text path plan_review renders, so any change to GitHub's published
        # text or stderr fails here. See tests/make_github_golden.py.
        with open(os.path.join(HERE, "tests", "fixtures", "github-golden.json"),
                  encoding="utf-8") as fh:
            golden = json.load(fh)
        with open(self.ledger, "w", encoding="utf-8") as fh:
            json.dump(golden["ledger"], fh)
        with open(self.body, "w", encoding="utf-8", newline="") as fh:
            fh.write(golden["body"])
        for run in golden["runs"]:
            for provider in ([], ["--provider", "github"]):
                with self.subTest(args=run["args"], provider=provider):
                    proc = subprocess.run([sys.executable, "-B", SCRIPT, "payload", self.ledger,
                                           "--body-file", self.body, *run["args"], *provider],
                                          capture_output=True)
                    # Text-mode streams translate newlines on some platforms.
                    got = [proc.returncode] + [s.decode("utf-8").replace("\r\n", "\n")
                                               for s in (proc.stdout, proc.stderr)]
                    self.assertEqual(got, [run["code"], run["stdout"], run["stderr"]])

    def test_a_string_line_is_an_error_line_on_reviewable_not_a_traceback(self):
        with open(self.ledger, "w", encoding="utf-8") as fh:
            json.dump(ledger(item(1, line=10), off(2, line="3")), fh)
        proc = subprocess.run([sys.executable, "-B", SCRIPT, "payload", self.ledger,
                               "--body-file", self.body, "--provider", "reviewable"],
                              capture_output=True, text=True)
        self.assertEqual((proc.returncode, proc.stdout), (1, ""))
        last = proc.stderr.splitlines()[-1]
        self.assertTrue(last.startswith("error: "), proc.stderr)
        self.assertIn("#2", last)
        self.assertNotIn("Traceback", proc.stderr)

    def test_a_malformed_line_is_an_error_line_on_github_not_a_traceback(self):
        for items in ([item(1, line=10), item(2, line="3")], [item(1, line=10), item(2, line=0)]):
            with self.subTest(line=items[1]["line"]):
                with open(self.ledger, "w", encoding="utf-8") as fh:
                    json.dump(ledger(*items), fh)
                proc = subprocess.run([sys.executable, "-B", SCRIPT, "payload", self.ledger,
                                       "--body-file", self.body],
                                      capture_output=True, text=True)
                self.assertEqual((proc.returncode, proc.stdout), (1, ""))
                self.assertEqual(proc.stderr.splitlines(), [
                    "error: item id(s) have a line that is not a positive integer, cannot be "
                    "attached to a comment: #2"])

    def test_an_unknown_provider_is_refused(self):
        proc = subprocess.run([sys.executable, "-B", SCRIPT, "payload", self.ledger,
                               "--body-file", self.body, "--provider", "gitlab"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("invalid choice", proc.stderr)

    def write(self, obj):
        path = os.path.join(self.dir, "payload.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        return path

    def preview(self, path, page=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = anchor.cmd_preview(SimpleNamespace(payload=path, page=page))
        return code, out.getvalue(), err.getvalue()

    def test_preview_renders_every_discussion_in_full_with_its_disposition(self):
        _, out = self.payload(provider="reviewable")
        art = json.loads(out)
        code, text, _ = self.preview(self.write(art))
        self.assertEqual(code, 0)
        self.assertIn("PR REVIEW PAYLOAD PREVIEW (reviewable)", text)
        self.assertIn("discussions: 2", text)
        self.assertIn("framing disposition: informing", text)
        self.assertIn("DISCUSSION 1/2 - a.ts:5 [discussing] - #3", text)
        self.assertIn("DISCUSSION 2/2 - a.ts:10 [blocking] - #1, #2", text)
        self.assertIn(art["framing"]["markdownBody"], text)
        for d in art["discussions"]:
            self.assertIn(d["markdownBody"], text)

    def test_preview_refuses_an_artifact_without_discussions(self):
        code, out, err = self.preview(self.write({"provider": "reviewable", "framing": {}}))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("missing required field 'discussions'", err)

    def test_preview_refuses_an_artifact_without_a_framing_comment(self):
        good = {"markdownBody": "f", "disposition": "informing"}
        for framing in (None, "f", {}, dict(good, markdownBody=""), dict(good, disposition=""),
                        {"markdownBody": "f"}, dict(good, disposition=["informing"])):
            with self.subTest(framing=framing):
                art = {"provider": "reviewable", "discussions": []}
                if framing is not None:
                    art["framing"] = framing
                code, out, err = self.preview(self.write(art))
                self.assertEqual((code, out), (1, ""))
                self.assertEqual(err.splitlines(), [
                    f"error: payload file {self.dir}{os.sep}payload.json is missing required "
                    "field 'framing' (an object with non-empty markdownBody and disposition)"])
        code, _, _ = self.preview(self.write({"provider": "reviewable", "discussions": [],
                                              "framing": good}))
        self.assertEqual(code, 0)

    def test_preview_shows_the_framing_disposition_it_will_post(self):
        with open(self.ledger, "w", encoding="utf-8") as fh:
            json.dump(ledger(item(1), item(2, sev="blocker", line=5, anchor_status="dead")), fh)
        _, out = self.payload(provider="reviewable")
        _, text, _ = self.preview(self.write(json.loads(out)))
        self.assertIn("framing disposition: blocking", text)

    def test_preview_pages_reassemble_for_reviewable(self):
        art = {"provider": "reviewable", "framing": {"markdownBody": "f", "disposition": "informing"},
               "discussions": [{"path": "a.ts", "line": n + 1, "markdownBody": "x" * 6000,
                                "disposition": "discussing", "item_ids": [n + 1]}
                               for n in range(7)]}
        pages = anchor.preview_pages(art)
        self.assertGreater(len(pages), 1)
        self.assertEqual("\n".join(pg.split("\n", 1)[1] for pg in pages),
                         anchor.render_preview(art))
        code, text, _ = self.preview(self.write(art), page=2)
        self.assertEqual(code, 0)
        self.assertTrue(text.startswith(f"page 2/{len(pages)}"))


class TestProviderLookup(unittest.TestCase):
    """The step 1.5 lookup in commands/pr-review-post.md, run as written
    against a providers.json passed as the plugin data path would be - here
    a throwaway directory whose name has a space in it."""

    def setUp(self):
        with open(os.path.join(HERE, "commands", "pr-review-post.md"), encoding="utf-8") as fh:
            md = fh.read()
        blocks = [seg.split('\n" "$REPO"')[0] for seg in md.split('python3 -c "\n')[1:]]
        self.lookup = next(b for b in blocks if "keyed by owner/repo" in b)
        self.writer = next(b.split('\n" "$REPO" "')[0]
                           for b in md.split('python3 -c "\n')[1:] if "tempfile" in b)
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "plugin data", "providers.json")
        os.makedirs(os.path.dirname(self.path))

    def tearDown(self):
        self.tmp.cleanup()

    def run_lookup(self, data, repo="o/r"):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        proc = subprocess.run([sys.executable, "-B", "-c", self.lookup, repo, self.path],
                              capture_output=True, text=True)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()

    def test_this_repos_entry_is_read(self):
        self.assertEqual(self.run_lookup({"o/r": "reviewable", "x/y": "github"}),
                         (0, "reviewable", ""))
        self.assertEqual(self.run_lookup({"x/y": "github"}), (0, "", ""))

    def write_default(self, repo, provider):
        return subprocess.run([sys.executable, "-B", "-c", self.writer, repo, provider, self.path],
                              capture_output=True, text=True)

    def test_the_writer_replaces_the_file_and_keeps_other_entries(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"x/y": "github"}, fh)
        before = os.stat(self.path).st_ino
        proc = self.write_default("o/r", "reviewable")
        self.assertEqual((proc.returncode, os.path.normpath(proc.stdout.strip()), proc.stderr),
                         (0, os.path.normpath(self.path), ""))
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"o/r": "reviewable", "x/y": "github"})
        # Renamed over, not rewritten in place: an interrupted write leaves
        # the old file whole. No temporary file is left beside it.
        self.assertNotEqual(os.stat(self.path).st_ino, before)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["providers.json"])

    def test_another_repos_bad_entry_stops_the_run_and_is_named(self):
        code, out, err = self.run_lookup({"o/r": "github", "x/y": "gitlab"})
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("error: "), err)
        self.assertIn("the entry for x/y is 'gitlab', not github or reviewable", err)


if __name__ == "__main__":
    unittest.main()
