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

    def test_the_artifact_carries_them_end_to_end(self):
        led = ledger(item(1, sev="blocker"), item(2, sev="blocker", f="b.ts",
                                                  verdict="INCONCLUSIVE"),
                     item(3, sev="blocker", f="c.ts", verdict="REFUTED"), item(4, f="d.ts"))
        art, _ = reviewable(led, include_refuted=["3"])
        got = {d["item_ids"][0]: d["disposition"] for d in art["discussions"]}
        self.assertEqual(got, {1: "blocking", 2: "discussing", 3: "informing", 4: "discussing"})


class TestArtifactShape(unittest.TestCase):
    def test_path_and_line_only(self):
        art, _ = reviewable(ledger(item(1)))
        self.assertEqual(set(art), {"provider", "framing", "discussions"})
        self.assertEqual(art["provider"], "reviewable")
        self.assertEqual(set(art["framing"]), {"markdownBody", "disposition"})
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


class TestStderr(unittest.TestCase):
    LED = staticmethod(lambda: ledger(item(1), off(2, line=5), item(3, line=7, anchor_status="dead")))

    def test_reviewable_accounts_in_its_own_terms(self):
        _, err = reviewable(self.LED())
        self.assertEqual(err.splitlines(), [
            "warning: 1 selected item(s) moved into the review-level comment, which publishes "
            "resolved (no location in this pull request to attach a comment to) - #3 (dead anchor)",
            "notice: 1 selected item(s) are not on a line this PR changes and post inline, each "
            "site opening with a note saying so - #2"])

    def test_github_wording_is_unchanged(self):
        _, err = github(self.LED())
        self.assertEqual(err.splitlines(), [
            "warning: 2 selected item(s) moved into the review body (cannot be anchored to a "
            "changed line) - #2 (not on a changed line), #3 (dead anchor)"])


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

    def test_changed_lines_records_it(self):
        led = ledger(*self.items)
        anchor.lt.mark_changed_lines(led, self.wt, anchor.parse_changed_lines(self.DIFF))
        self.assertEqual(self.recorded(led), {1: True, 2: False, 3: True})

    def test_reanchor_records_it(self):
        lp, dp = os.path.join(self.wt, "ledger.json"), os.path.join(self.wt, "head.diff")
        with open(lp, "w", encoding="utf-8") as fh:
            json.dump(ledger(*self.items), fh)
        with open(dp, "w", encoding="utf-8", newline="") as fh:
            fh.write(self.DIFF)
        args = SimpleNamespace(ledger=lp, diff=dp, worktree=self.wt, head=None, dry_run=False)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(anchor.cmd_reanchor(args), 0)
        with open(lp, encoding="utf-8") as fh:
            self.assertEqual(self.recorded(json.load(fh)), {1: True, 2: False, 3: True})


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


if __name__ == "__main__":
    unittest.main()
