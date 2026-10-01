import unittest, importlib.util, os, sys, json, tempfile, io, contextlib
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ledger_tools as lt

spec = importlib.util.spec_from_file_location("anchor", os.path.join(HERE, "pr-review-anchor.py"))
anchor = importlib.util.module_from_spec(spec); spec.loader.exec_module(anchor)


def tmpdir():
    return tempfile.mkdtemp()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


from test_pr_review_anchor import item  # noqa: E402


def led(*items, **root):
    d = {"schema": 2, "items": list(items), "clusters": []}
    d.update(root); return d


class TestProcessVocabulary(unittest.TestCase):
    def test_flags_review_process_words(self):
        for text in ("Note for the refuter: check X", "Item #8 in this batch narrows it",
                     "see item #7's fix", "the pressure-test showed", "same as #4"):
            self.assertTrue(lt.process_vocabulary_in(text), text)

    def test_flags_each_process_pattern_in_isolation(self):
        """The five texts above always trip two or more PROCESS_PATTERNS at
        once ("Note for the refuter" matches both `refuters?` and `Note for
        the`; "item #N" always also matches the bare `#N` pattern), so a
        mutation that deletes any one of `refuters?`, `this batch`, or
        `items?\\s+#\\d+` alone still leaves that test green. These texts
        isolate each of those three patterns so deleting any single one is
        independently caught."""
        # `refuters?` alone - no "Note for the", "#", "item", "this batch".
        self.assertTrue(lt.process_vocabulary_in("the refuters agreed on this"))
        # `this batch` alone - no "#", "item", "refuter", "Note for the".
        self.assertTrue(lt.process_vocabulary_in("duplicated across this batch of findings"))
        # `items?\s+#\d+` alone: the bare-#N pattern excludes a "#N" directly
        # followed by "}}" (that is the {{#n}} token exemption), so this "#4"
        # is invisible to it and only the items-pattern catches it.
        self.assertTrue(lt.process_vocabulary_in("reference item #4}} elsewhere"))

    def test_allows_tokens_code_spans_and_ordinary_rewrite_wording(self):
        for text in ("same mechanism as {{#4}}", "rewrite the `@returns` at `a.ts:3`",
                     "the never-downgrade rule holds", "use `color: #123456` here",
                     "call `arr.at(#1)`"):
            self.assertEqual(lt.process_vocabulary_in(text), [], text)

    def test_validate_reports_the_id_and_the_phrase(self):
        errs = anchor.validate(led(item(1, detail="**Fix:** do X. Note for the refuter: Y")))
        self.assertTrue(any("item 1" in e and "Note for the" in e for e in errs), errs)


class TestClaimShape(unittest.TestCase):
    def test_two_sentences_are_rejected(self):
        self.assertTrue(lt.claim_shape_errors("The tab goes blank. Then it crashes."))

    def test_code_spans_and_abbreviations_are_not_sentence_breaks(self):
        self.assertEqual(lt.claim_shape_errors(
            "Calling `a.b()` e.g. from `Foo.Bar` leaves the pane blank."), [])

    def test_more_than_40_words_is_rejected(self):
        self.assertTrue(lt.claim_shape_errors(" ".join(["word"] * 41) + "."))

    def test_refuted_claims_are_not_shape_checked(self):
        errs = anchor.validate(led(item(1, verdict="REFUTED", claim="A. B.")))
        self.assertFalse(any("sentence" in e for e in errs), errs)


class TestExcerpts(unittest.TestCase):
    def test_ellipsis_lines_are_detected_but_spread_is_not(self):
        for ln in ("...", "  …", "// ...", "# ..."):
            self.assertTrue(lt.is_ellipsis_line(ln), ln)
        for ln in ("  foo(...args);", "const x = [...a];"):
            self.assertFalse(lt.is_ellipsis_line(ln), ln)

    def test_validate_rejects_an_ellipsis_excerpt_without_a_worktree(self):
        errs = anchor.validate(led(item(1, anchor_excerpt="a()\n  ...\nb()")))
        self.assertTrue(any("ellipsis" in e for e in errs), errs)

    def test_worktree_check_requires_a_verbatim_excerpt_at_line(self):
        d = tmpdir(); write(os.path.join(d, "a.ts"), "one\ntwo  \nthree\n")
        ok = item(1, line=2, line_end=3, anchor_excerpt="two\nthree")
        bad = item(1, line=2, line_end=2, anchor_excerpt="TWO")
        self.assertEqual(lt.excerpt_errors(led(ok), d), [])
        self.assertTrue(lt.excerpt_errors(led(bad), d))


class TestVerdictSemantics(unittest.TestCase):
    def test_downgraded_at_the_same_severity_is_rejected(self):
        errs = anchor.validate(led(item(1, verdict="DOWNGRADED", fold_note="folded")))
        self.assertTrue(any("DOWNGRADED" in e and "lower" in e for e in errs), errs)

    def test_downgraded_needs_a_fold_note(self):
        errs = anchor.validate(led(item(1, verdict="DOWNGRADED", severity="low",
                                        severity_in="high")))
        self.assertTrue(any("fold_note" in e for e in errs), errs)

    def test_refuted_may_not_carry_a_rewrite(self):
        errs = anchor.validate(led(item(1, verdict="REFUTED", claim_verdict="REWRITTEN",
                                        claim_original="old")))
        self.assertTrue(any("REFUTED" in e and "rewrite" in e for e in errs), errs)

    def test_rewritten_claim_must_record_a_different_original(self):
        errs = anchor.validate(led(item(1, claim_verdict="REWRITTEN")))
        self.assertTrue(any("claim_original" in e for e in errs), errs)
        errs = anchor.validate(led(item(1, claim_verdict="REWRITTEN", claim_original="c")))
        self.assertTrue(any("claim_original" in e for e in errs), errs)
        self.assertEqual(anchor.validate(led(item(1, claim_verdict="REWRITTEN",
                                                  claim_original="older"))), [])

    def test_rewritten_fix_must_record_a_different_original(self):
        it = item(1, fix_verdict="REWRITTEN", detail="**Why it matters:** y\n\n**Fix:** new")
        self.assertTrue(any("fix_original" in e for e in anchor.validate(led(it))))
        it["fix_original"] = "old"
        self.assertEqual(anchor.validate(led(it)), [])

    def test_unanswered_inconclusive_is_exempt_from_claim_verdict(self):
        it = item(1, verdict="INCONCLUSIVE", unanswered=True, fix_verdict="UNVERIFIABLE")
        del it["claim_verdict"]
        self.assertEqual(anchor.validate(led(it)), [])


class TestNewHedges(unittest.TestCase):
    def test_deferrals_are_hedges(self):
        for fix in ("No code change.", "Not required for this PR.",
                    "If this cannot happen, this should be downgraded or closed.",
                    "If nothing produces it, then drop the finding."):
            errs = anchor.validate(led(item(1, detail="**Fix:** " + fix)))
            self.assertTrue(any("hedged" in e for e in errs), fix)

    def test_type_level_prose_is_not_a_hedge(self):
        for fix in ("Make `id` optional in `Props`.", "Mark `x` as not required in the schema."):
            self.assertEqual(anchor.validate(led(item(1, detail="**Why it matters:** y\n\n**Fix:** " + fix))), [], fix)


class TestPrimitives(unittest.TestCase):
    def test_anchor_reexports_the_moved_primitives(self):
        self.assertIs(anchor.parse_changed_lines, lt.parse_changed_lines)
        self.assertIs(anchor.fix_slot, lt.fix_slot)
        self.assertIs(anchor.demotion_reason, lt.demotion_reason)
        self.assertEqual(anchor.SEVERITIES, lt.SEVERITIES)

    def test_write_ledger_is_atomic_and_keeps_a_backup(self):
        d = tmpdir(); p = os.path.join(d, "ledger.json")
        write(p, '{"schema": 2, "items": []}')
        lt.write_ledger(p, {"schema": 2, "items": [1]}, "ingest")
        self.assertEqual(json.load(open(p))["items"], [1])
        self.assertEqual(json.load(open(p + ".bak-ingest"))["items"], [])
        self.assertEqual([f for f in os.listdir(d) if f.endswith(".tmp")], [])

    def test_load_json_names_the_file_and_rejects_a_non_object_root(self):
        with self.assertRaisesRegex(lt.LedgerError, "ledger file not found: /nope"):
            lt.load_json("/nope")
        p = write(os.path.join(tmpdir(), "l.json"), "[1]")
        with self.assertRaisesRegex(lt.LedgerError, "root must be a JSON object"):
            lt.load_json(p)

    def test_read_lines_errors_past_eof_and_on_a_missing_file(self):
        d = tmpdir(); write(os.path.join(d, "a.ts"), "one\ntwo\n")
        self.assertEqual(lt.read_lines(d, "a.ts", 1, 2), ["one", "two"])
        with self.assertRaisesRegex(lt.LedgerError, "beyond end of file"):
            lt.read_lines(d, "a.ts", 2, 3)
        with self.assertRaisesRegex(lt.LedgerError, "not found"):
            lt.read_lines(d, "gone.ts", 1, 1)
        with self.assertRaisesRegex(lt.LedgerError, "line_end 1 is before line 2"):
            lt.read_lines(d, "a.ts", 2, 1)


class TestDemotionReason(unittest.TestCase):
    def test_dead_anchor(self):
        self.assertEqual(lt.demotion_reason(item(1, anchor_status="dead")), "dead anchor")

    def test_off_diff_anchor_status(self):
        self.assertEqual(lt.demotion_reason(item(1, anchor_status="off-diff")),
                         "not on a changed line")

    def test_on_changed_line_explicitly_false(self):
        self.assertEqual(lt.demotion_reason(item(1, on_changed_line=False)),
                         "not on a changed line")

    def test_dead_anchor_wins_over_on_changed_line_true(self):
        # #2 from the fix-round-1 repro: a dead anchor demotes even though
        # on_changed_line still says True (reanchor leaves on_changed_line
        # alone for a dead excerpt - see mark_changed_lines/reanchor_item).
        self.assertEqual(lt.demotion_reason(item(1, anchor_status="dead",
                                                  on_changed_line=True)), "dead anchor")

    def test_missing_or_none_on_changed_line_is_postable(self):
        it = item(1); it.pop("on_changed_line", None)
        self.assertIsNone(lt.demotion_reason(it))
        self.assertIsNone(lt.demotion_reason(item(1, on_changed_line=None)))

    def test_on_changed_line_true_with_no_anchor_status_is_postable(self):
        self.assertIsNone(lt.demotion_reason(item(1, on_changed_line=True)))


class TestSchema(unittest.TestCase):
    def test_schema_1_is_rejected_with_a_rerun_instruction(self):
        errs = anchor.validate({"schema": 1, "items": [], "clusters": []})
        self.assertTrue(any("schema 1" in e and "re-run /pr-review" in e for e in errs), errs)

    def test_missing_schema_is_rejected(self):
        errs = anchor.validate({"items": [], "clusters": []})
        self.assertTrue(any("schema" in e for e in errs), errs)


CTX = {"link_deps": "linked /wt/node_modules", "commands": "`pnpm nx test <proj>`",
       "obligations": "| If the fix ... | it must also ... | rule |\n| --- | --- | --- |\n"
                      "| changes behavior | come with a test | `CLAUDE.md` |",
       "claude_md_paths": ["/wt/CLAUDE.md"], "scratch": "/s"}


def brief_ledger():
    return led(item(1, cluster="C1", refuter_note="check the second site"),
               item(2, cluster="C1"), item(3, sev="low"),
               clusters=[{"id": "C1", "title": "t", "shared_fix": "sf", "item_ids": [1, 2]}],
               target={"kind": "pr", "repo": "o/r", "number": 13},
               head_sha="h" * 40, merge_base="m" * 40, worktree="/wt")


class TestBrief(unittest.TestCase):
    def test_units_are_clusters_and_unclustered_items(self):
        self.assertEqual(lt.refuter_units(brief_ledger()), {"C1": [1, 2], "i3": [3]})

    def test_units_list_ids_in_numeric_order(self):
        l = led(item(2, cluster="C1"), item(10, cluster="C1"),
                clusters=[{"id": "C1", "item_ids": [10, 2]}])
        self.assertEqual(lt.refuter_units(l), {"C1": [2, 10]})

    def test_renders_every_placeholder(self):
        text, meta = lt.render_brief(brief_ledger(), "C1", CTX, "/s/pr-review/pr13")
        self.assertNotRegex(text, r"\{\{[A-Z_]+\}\}")
        self.assertIn("/s/pr-review/pr13/verdicts/C1.txt", text)
        self.assertIn("/s/wt-pr13-rc1", text)
        self.assertIn("check the second site", text)
        self.assertIn('"shared_fix": "sf"', text)
        self.assertEqual(meta, {"key": "C1", "owns": [1, 2], "hash": lt.template_hash(),
                                "run": lt.run_id(brief_ledger())})
        self.assertIn(f"RUN: {lt.run_id(brief_ledger())}", text)
        self.assertIn(f"BRIEF: {lt.template_hash()}", text)

    def test_an_unknown_key_cannot_batch_unrelated_items(self):
        with self.assertRaisesRegex(lt.LedgerError, "unknown refuter key"):
            lt.render_brief(brief_ledger(), "i1", CTX, "/s")   # 1 is clustered
        with self.assertRaisesRegex(lt.LedgerError, "unknown refuter key"):
            lt.render_brief(brief_ledger(), "i3,4", CTX, "/s")

    def test_an_obligation_row_without_a_repo_file_is_rejected(self):
        # R1: the header row is whichever row sits immediately before the
        # `| --- |` separator, regardless of its wording - so this header
        # ("| a | b | c |") is exempt even though it does not start "If the
        # fix", and it is the DATA row below it that must be the one that
        # fails. Asserting on "user convention" (only in the data row) proves
        # that: a header-row false positive would trip on "c" or "rule"
        # instead and could not satisfy this assertion.
        ctx = dict(CTX, obligations="| a | b | c |\n| --- | --- | --- |\n"
                                    "| adds prose | American spelling | user convention |")
        with self.assertRaises(lt.LedgerError) as cm:
            lt.render_brief(brief_ledger(), "C1", ctx, "/s")
        self.assertIn("does not cite a repo file", str(cm.exception))
        self.assertIn("user convention", str(cm.exception))

    def test_a_header_row_without_a_file_cell_is_exempt_when_data_rows_cite_one(self):
        ctx = dict(CTX, obligations="| a | b | c |\n| --- | --- | --- |\n"
                                    "| adds prose | American spelling | `CLAUDE.md` |")
        text, _ = lt.render_brief(brief_ledger(), "C1", ctx, "/s")
        self.assertIn("adds prose", text)

    def test_a_missing_context_key_is_named(self):
        ctx = dict(CTX); del ctx["commands"]
        with self.assertRaisesRegex(lt.LedgerError, "commands"):
            lt.render_brief(brief_ledger(), "C1", ctx, "/s")

    def test_verdict_fields_are_not_leaked_into_items(self):
        l = brief_ledger(); l["items"][0]["verdict_reason"] = "SECRET"
        text, _ = lt.render_brief(l, "C1", CTX, "/s")
        self.assertNotIn("SECRET", text)

    def test_the_reply_header_the_brief_shows_is_accepted_by_ingest(self):
        # Template, parser and ingest must agree on the header: copy the
        # fenced header exactly as the rendered brief shows it and ingest it.
        d = tmpdir()
        l = brief_ledger()
        text, meta = lt.render_brief(l, "C1", CTX, d)
        head = text.split("```text\n", 1)[1].split("\n```", 1)[0]
        body = GOOD.split("OWNS: 1, 2\n", 1)[1]
        write(os.path.join(d, "briefs", "C1.json"), json.dumps(meta))
        write(os.path.join(d, "briefs", "i3.json"), json.dumps(
            lt.render_brief(l, "i3", CTX, d)[1]))
        write(os.path.join(d, "verdicts", "C1.txt"), head + "\n" + body)
        l["items"][0]["detail"] = "**Fix:** old"
        lt.ingest(l, os.path.join(d, "briefs"), os.path.join(d, "verdicts"), lt.template_hash(),
                  inconclusive=[3], reason="Not verified: the automated check did not complete.")
        self.assertEqual(l["items"][0]["verdict"], "CONFIRMED")

    def test_placeholder_shaped_text_in_values_is_left_alone(self):
        l = brief_ledger()
        l["items"][0]["claim"] = "Renders {{TITLE}} literally, and {{KEY}} too."
        text, _ = lt.render_brief(l, "C1", CTX, "/s")
        self.assertIn("Renders {{TITLE}} literally, and {{KEY}} too.", text)

    def test_claude_md_paths_must_be_a_list_of_strings(self):
        for bad in ("/wt/CLAUDE.md", [1], None):
            with self.assertRaisesRegex(lt.LedgerError, "claude_md_paths must be a list"):
                lt.render_brief(brief_ledger(), "C1", dict(CTX, claude_md_paths=bad), "/s")

    def test_a_leftover_placeholder_is_rejected(self):
        # Every placeholder the real template uses has a matching key in
        # render_brief's values dict, so this path is never exercised by the
        # shipped template - it only guards against a future template that
        # introduces a placeholder nothing fills. Point at a stand-in
        # template that does that, so the guard has a test that can fail it.
        d = tmpdir()
        bogus = write(os.path.join(d, "bogus.tmpl.md"), "brief-version: {{BRIEF_HASH}}\n{{NOPE}}\n")
        with self.assertRaisesRegex(lt.LedgerError, "NOPE"):
            lt.render_brief(brief_ledger(), "C1", CTX, "/s", template_path=bogus)


GOOD = """BRIEF: abc
RUN: @RUN@
KEY: C1
OWNS: 1, 2

ITEM: 1
VERDICT: CONFIRMED
SEVERITY: high
ON_CHANGED_LINE: true
REASON: line one
line two mentions VERDICT: inline, which is fine
CLAIM_VERDICT: SOUND
CLAIM_REASON: matched at a.ts:3
FIX_VERDICT: REWRITTEN
FIX_REASON: probe at a.ts:4
FIX_REWRITE: call `b()` instead

ITEM: 2
VERDICT: REFUTED
SEVERITY: low
ON_CHANGED_LINE: false
REASON: nothing produces it
"""


class TestParseVerdictFile(unittest.TestCase):
    def test_parses_header_blocks_and_multiline_values(self):
        header, blocks = lt.parse_verdict_file(GOOD)
        self.assertEqual(header, {"BRIEF": "abc", "RUN": "@RUN@", "KEY": "C1", "OWNS": "1, 2"})
        self.assertEqual([b["ITEM"] for b in blocks], ["1", "2"])
        self.assertEqual(blocks[0]["REASON"],
                         "line one\nline two mentions VERDICT: inline, which is fine")
        self.assertEqual(blocks[0]["FIX_REWRITE"], "call `b()` instead")

    def test_prose_before_the_header_is_rejected(self):
        with self.assertRaisesRegex(lt.LedgerError, "unrecognized line"):
            lt.parse_verdict_file("Here is my summary.\n" + GOOD)

    def test_a_field_given_twice_is_rejected(self):
        with self.assertRaisesRegex(lt.LedgerError, "given twice"):
            lt.parse_verdict_file(GOOD.replace("SEVERITY: low", "SEVERITY: low\nSEVERITY: high"))

    def test_indented_labels_parse_like_unindented_ones(self):
        indented = "\n".join("    " + l if l else l for l in GOOD.splitlines())
        self.assertEqual(lt.parse_verdict_file(indented), lt.parse_verdict_file(GOOD))

    def test_labels_indented_unevenly_still_parse(self):
        # Header flush left, blocks indented: no common indent to remove, so
        # each label must be recognized after its own leading whitespace.
        head, rest = GOOD.split("\n\n", 1)
        mixed = head + "\n\n" + "\n".join("  " + l if l.startswith(("ITEM", "VERDICT")) else l
                                            for l in rest.splitlines())
        self.assertEqual(lt.parse_verdict_file(mixed), lt.parse_verdict_file(GOOD))

    def test_indented_continuation_lines_keep_their_indentation(self):
        _, blocks = lt.parse_verdict_file(GOOD.replace("line two mentions", "    line two mentions"))
        self.assertEqual(blocks[0]["REASON"],
                         "line one\n    line two mentions VERDICT: inline, which is fine")

    def test_a_file_wrapped_in_a_code_fence_parses_without_the_fence(self):
        self.assertEqual(lt.parse_verdict_file("```\n" + GOOD + "```\n"),
                         lt.parse_verdict_file(GOOD))
        self.assertEqual(lt.parse_verdict_file("```text\n" + GOOD + "```"),
                         lt.parse_verdict_file(GOOD))

    def test_a_fence_inside_a_value_is_kept(self):
        text = GOOD.replace("REASON: nothing produces it",
                            "REASON: nothing produces it\n```\ncode\n```")
        _, blocks = lt.parse_verdict_file(text)
        self.assertEqual(blocks[1]["REASON"], "nothing produces it\n```\ncode\n```")

    def test_a_header_after_a_block_is_rejected(self):
        with self.assertRaisesRegex(lt.LedgerError, "header line after"):
            lt.parse_verdict_file(GOOD + "KEY: C2\n")


def ingest_env(verdict_text, owns=(1, 2), key="C1"):
    """A draft ledger plus one brief meta and (unless `verdict_text` is None)
    its verdict file. `@RUN@` in `verdict_text` becomes this draft's run id,
    which the meta carries too, as `brief` would have written it."""
    d = tmpdir()
    l = led(item(1, cluster="C1", detail="**What happens:** x\n\n**Fix:** old fix",
                 refuter_note="hint"),
            item(2, cluster="C1", sev="low"),
            clusters=[{"id": "C1", "title": "t", "shared_fix": "s", "item_ids": [1, 2]}],
            head_sha="h" * 40)
    for it in l["items"]:
        for k in ("verdict", "verdict_reason", "claim_verdict", "claim_reason",
                  "fix_verdict", "fix_reason"):
            it.pop(k, None)
    run = lt.run_id(l)
    write(os.path.join(d, "briefs", f"{key}.json"),
          json.dumps({"key": key, "owns": list(owns), "hash": "abc", "run": run}))
    if verdict_text is not None:
        write(os.path.join(d, "verdicts", f"{key}.txt"), verdict_text.replace("@RUN@", run))
    return d, l


class TestIngest(unittest.TestCase):
    def run_ingest(self, text, **kw):
        d, l = ingest_env(text, **{k: v for k, v in kw.items() if k in ("owns", "key")})
        extra = {k: v for k, v in kw.items() if k in ("inconclusive", "reason")}
        lt.ingest(l, os.path.join(d, "briefs"), os.path.join(d, "verdicts"), "abc", **extra)
        return {it["id"]: it for it in l["items"]}

    def test_applies_verdicts_and_the_fix_rewrite_keeping_the_original(self):
        out = self.run_ingest(GOOD)
        self.assertEqual(out[1]["verdict"], "CONFIRMED")
        self.assertEqual(out[1]["fix_original"], "old fix")
        self.assertTrue(out[1]["detail"].endswith("**Fix:** call `b()` instead"))
        self.assertIn("**What happens:** x", out[1]["detail"])
        self.assertEqual(out[1]["refuter_key"], "C1")
        self.assertNotIn("refuter_note", out[1])
        self.assertEqual(out[2]["verdict"], "REFUTED")

    def test_never_touches_severity_in(self):
        out = self.run_ingest(GOOD.replace("SEVERITY: high", "SEVERITY: medium"))
        self.assertEqual((out[1]["severity_in"], out[1]["severity"]), ("high", "medium"))

    def test_claim_rewrite_saves_the_original(self):
        text = GOOD.replace("CLAIM_VERDICT: SOUND", "CLAIM_VERDICT: REWRITTEN\n"
                            "CLAIM_REWRITE: The pane goes blank on resize.")
        out = self.run_ingest(text)
        self.assertEqual((out[1]["claim_original"], out[1]["claim"]),
                         ("c", "The pane goes blank on resize."))

    def assertIngestFails(self, text, pattern, **kw):
        with self.assertRaisesRegex(lt.LedgerError, pattern):
            self.run_ingest(text, **kw)

    def test_failures_are_named(self):
        self.assertIngestFails(None, "C1: no verdict file")
        self.assertIngestFails(GOOD.replace("BRIEF: abc", "BRIEF: zzz"), "does not match")
        self.assertIngestFails(GOOD.split("ITEM: 2")[0], "no answer for item 2")
        self.assertIngestFails(GOOD.replace("ITEM: 2", "ITEM: 1"), "answered twice")
        self.assertIngestFails(GOOD.replace("ITEM: 2", "ITEM: 7"), "does not own item 7")
        self.assertIngestFails(GOOD.replace("VERDICT: CONFIRMED", "VERDICT: PENDING"), "VERDICT")
        self.assertIngestFails(GOOD.replace("FIX_VERDICT: REWRITTEN", "FIX_VERDICT: SOUND"),
                               "FIX_REWRITE given with FIX_VERDICT SOUND")
        self.assertIngestFails(GOOD.replace("FIX_REWRITE: call `b()` instead\n", ""),
                               "FIX_VERDICT REWRITTEN without FIX_REWRITE")
        self.assertIngestFails(GOOD.replace("REASON: nothing produces it",
                                            "REASON: x\nCLAIM_VERDICT: REWRITTEN\n"
                                            "CLAIM_REWRITE: y"), "REFUTED")
        self.assertIngestFails(GOOD.replace("VERDICT: CONFIRMED", "VERDICT: DOWNGRADED"),
                               "not lower than severity_in")
        self.assertIngestFails(GOOD.replace("ON_CHANGED_LINE: true", "ON_CHANGED_LINE: maybe"),
                               "ON_CHANGED_LINE must be true or false")
        self.assertIngestFails(
            GOOD.replace("REASON: line one\nline two mentions VERDICT: inline, which is fine\n",
                        "REASON:\n"), "item 1: REASON is empty")
        self.assertIngestFails(GOOD.replace("CLAIM_VERDICT: SOUND", "CLAIM_VERDICT: MAYBE"),
                               "CLAIM_VERDICT 'MAYBE' not one of")
        self.assertIngestFails(GOOD.replace("CLAIM_REASON: matched at a.ts:3", "CLAIM_REASON:"),
                               "item 1: CLAIM_REASON is empty")
        self.assertIngestFails(GOOD.replace("KEY: C1", "KEY: C9"), "KEY header is")
        self.assertIngestFails(GOOD.replace("ITEM: 2", "ITEM: two"), "is not an integer id")
        self.assertIngestFails(None, "--inconclusive needs --reason", inconclusive=[1])
        # 1 and 2 are real (and cover C1's whole `owns`, so no verdict file is
        # needed for them); 9 is not - without the id check, 9 would survive
        # all the way to the apply loop and crash on `by_id[9]`.
        self.assertIngestFails(None, r"--inconclusive names item 9, which is not in this ledger",
                               inconclusive=[1, 2, 9], reason="stalled")

    def test_no_briefs_is_rejected(self):
        d = tmpdir()
        l = led(item(1))
        with self.assertRaisesRegex(lt.LedgerError, "no briefs in"):
            lt.ingest(l, os.path.join(d, "briefs"), os.path.join(d, "verdicts"), "abc")

    def test_fix_rewrite_without_a_fix_slot_in_detail_is_rejected_before_any_mutation(self):
        # replace_fix_slot() has its own "no **Fix:** slot" guard too, so a
        # LedgerError with matching text alone would pass even if the
        # _block_errors check that is meant to catch this up front (before any
        # item is mutated) were deleted - replace_fix_slot's guard would still
        # fire, just after item 1 was already partly rewritten. Asserting the
        # ledger is byte-for-byte unchanged is what actually pins the
        # _block_errors guard down.
        d, l = ingest_env(GOOD)
        l["items"][0]["detail"] = "**What happens:** x"  # no **Fix:** slot
        before = json.dumps(l, sort_keys=True)
        with self.assertRaisesRegex(lt.LedgerError, r"detail has no \*\*Fix:\*\* slot to rewrite"):
            lt.ingest(l, os.path.join(d, "briefs"), os.path.join(d, "verdicts"), "abc")
        self.assertEqual(json.dumps(l, sort_keys=True), before)

    def test_every_problem_is_reported_at_once(self):
        bad = GOOD.replace("VERDICT: CONFIRMED", "VERDICT: PENDING").replace(
            "SEVERITY: low", "SEVERITY: huge")
        with self.assertRaises(lt.LedgerError) as cm:
            self.run_ingest(bad)
        self.assertGreaterEqual(len(str(cm.exception).splitlines()), 2)

    def test_a_failed_ingest_changes_nothing(self):
        d, l = ingest_env(GOOD.replace("VERDICT: CONFIRMED", "VERDICT: PENDING"))
        before = json.dumps(l, sort_keys=True)
        with self.assertRaises(lt.LedgerError):
            lt.ingest(l, os.path.join(d, "briefs"), os.path.join(d, "verdicts"), "abc")
        self.assertEqual(json.dumps(l, sort_keys=True), before)

    def test_inconclusive_closes_unanswered_ids(self):
        out = self.run_ingest(None, inconclusive=[1, 2], reason="refuter stalled")
        self.assertEqual(out[1]["verdict"], "INCONCLUSIVE")
        self.assertTrue(out[1]["unanswered"])
        self.assertEqual(out[1]["fix_verdict"], "UNVERIFIABLE")
        self.assertEqual(out[1]["verdict_reason"], "refuter stalled")

    def test_an_item_no_brief_owns_is_an_error(self):
        d, l = ingest_env(GOOD)
        l["items"].append(item(3, sev="low"))
        with self.assertRaisesRegex(lt.LedgerError, "item 3 was never dispatched"):
            lt.ingest(l, os.path.join(d, "briefs"), os.path.join(d, "verdicts"), "abc")


class TestIngestInconclusiveMalformed(unittest.TestCase):
    """--inconclusive is the sanctioned way to close a refuter that failed
    twice, so a verdict file that is present but unusable must not block it."""

    def ingest(self, text, inconclusive, reason="Not verified: the automated check did not complete."):
        d, l = ingest_env(text)
        lt.ingest(l, os.path.join(d, "briefs"), os.path.join(d, "verdicts"), "abc",
                  inconclusive=inconclusive, reason=reason)
        return {it["id"]: it for it in l["items"]}

    def test_an_unparseable_file_closes_when_every_owned_id_is_inconclusive(self):
        out = self.ingest("The verdict is above.\n", [1, 2])
        self.assertEqual((out[1]["verdict"], out[2]["verdict"]), ("INCONCLUSIVE", "INCONCLUSIVE"))

    def test_a_file_missing_claim_and_fix_lines_closes_when_every_owned_id_is_inconclusive(self):
        text = "\n".join(l for l in GOOD.splitlines() if not l.startswith(("CLAIM_", "FIX_")))
        out = self.ingest(text, [1, 2])
        self.assertTrue(out[1]["unanswered"])
        self.assertEqual(out[2]["verdict"], "INCONCLUSIVE")

    def test_a_block_error_for_an_inconclusive_id_is_dropped_and_the_rest_is_ingested(self):
        text = "\n".join(l for l in GOOD.splitlines() if not l.startswith(("CLAIM_", "FIX_")))
        out = self.ingest(text, [1])
        self.assertEqual(out[1]["verdict"], "INCONCLUSIVE")
        self.assertTrue(out[1]["unanswered"])
        self.assertEqual(out[2]["verdict"], "REFUTED")

    def test_a_block_error_for_an_id_not_named_still_fails(self):
        text = GOOD.replace("SEVERITY: low", "SEVERITY: huge")
        with self.assertRaisesRegex(lt.LedgerError, "item 2: SEVERITY"):
            self.ingest(text, [1])


def two_unit_env(text_c1, text_i2):
    """Item 2 sits in C1's item_ids but carries no cluster, so it is also its
    own unit `i2` - two refuters are dispatched for it."""
    d = tmpdir()
    l = led(item(1, cluster="C1", detail="**Fix:** f"), item(2, sev="low", detail="**Fix:** g"),
            clusters=[{"id": "C1", "title": "t", "shared_fix": "s", "item_ids": [1, 2]}])
    run = lt.run_id(l)
    for key, owns, text in (("C1", [1, 2], text_c1), ("i2", [2], text_i2)):
        write(os.path.join(d, "briefs", f"{key}.json"),
              json.dumps({"key": key, "owns": owns, "hash": "abc", "run": run}))
        write(os.path.join(d, "verdicts", f"{key}.txt"), text.replace("@RUN@", run))
    return d, l


class TestIngestRunChecks(unittest.TestCase):
    def assertFails(self, d, l, pattern):
        before = json.dumps(l, sort_keys=True)
        with self.assertRaisesRegex(lt.LedgerError, pattern):
            lt.ingest(l, os.path.join(d, "briefs"), os.path.join(d, "verdicts"), "abc")
        self.assertEqual(json.dumps(l, sort_keys=True), before)

    def test_an_id_answered_in_two_files_fails(self):
        i2 = GOOD.split("ITEM: 2")[1]
        text_i2 = "BRIEF: abc\nRUN: @RUN@\nKEY: i2\nOWNS: 2\n\nITEM: 2" + i2
        d, l = two_unit_env(GOOD, text_i2)
        self.assertFails(d, l, r"item 2 answered in both C1 and i2")

    def test_a_meta_whose_owns_differs_from_the_ledger_unit_fails(self):
        d, l = ingest_env(GOOD.split("ITEM: 2")[0].replace("OWNS: 1, 2", "OWNS: 1"), owns=(1,))
        self.assertFails(d, l, r"C1: brief owns \[1\] but this ledger's C1 is \[1, 2\]")

    def test_a_meta_for_a_key_this_ledger_does_not_have_fails(self):
        d, l = ingest_env(GOOD)
        write(os.path.join(d, "briefs", "C9.json"),
              json.dumps({"key": "C9", "owns": [1], "hash": "abc", "run": lt.run_id(l)}))
        self.assertFails(d, l, r"C9: not a refuter unit of this ledger")

    def test_an_owns_header_that_differs_from_the_meta_fails(self):
        d, l = ingest_env(GOOD.replace("OWNS: 1, 2", "OWNS: 1, 3"))
        self.assertFails(d, l, r"C1: OWNS header is '1, 3', expected '1, 2'")

    def test_a_meta_from_another_draft_fails(self):
        d, l = ingest_env(GOOD)
        l["items"][0]["claim"] = "A different claim from a later draft."
        self.assertFails(d, l, r"C1: brief was rendered for a different draft")

    def test_a_meta_without_a_run_id_fails(self):
        d, l = ingest_env(GOOD)
        write(os.path.join(d, "briefs", "C1.json"),
              json.dumps({"key": "C1", "owns": [1, 2], "hash": "abc"}))
        self.assertFails(d, l, r"C1: brief was rendered for a different draft")

    def test_a_run_header_that_differs_from_the_meta_fails(self):
        d, l = ingest_env(GOOD.replace("RUN: @RUN@", "RUN: 000000000000"))
        self.assertFails(d, l, r"C1: RUN header is '000000000000'")

    def test_the_run_id_covers_head_ids_and_claims(self):
        base = lt.run_id(ingest_env(None)[1])
        for change in (lambda l: l.update(head_sha="x" * 40),
                       lambda l: l["items"][0].update(claim="other"),
                       lambda l: l["items"][0].update(id=9)):
            l = ingest_env(None)[1]; change(l)
            self.assertNotEqual(lt.run_id(l), base)
        l = ingest_env(None)[1]; l["items"][0]["detail"] = "changed"
        self.assertEqual(lt.run_id(l), base)


class TestIngestCli(unittest.TestCase):
    def test_cmd_ingest_writes_the_ledger_and_a_backup_on_success_and_leaves_it_untouched_on_failure(self):
        # cmd_ingest hashes the real template (unlike lt.ingest() in the tests
        # above, which is given "abc" directly), so the verdict file's BRIEF
        # header has to match that real hash here.
        good = GOOD.replace("BRIEF: abc", f"BRIEF: {lt.template_hash()}")
        d, l = ingest_env(good)
        ledger_path = write(os.path.join(d, "ledger.json"), json.dumps(l))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = anchor.cmd_ingest(SimpleNamespace(ledger=ledger_path, inconclusive=None,
                                                      reason=None))
        self.assertEqual(code, 0, err.getvalue())
        written = {it["id"]: it for it in json.load(open(ledger_path))["items"]}
        self.assertEqual(written[1]["verdict"], "CONFIRMED")
        self.assertTrue(os.path.isfile(ledger_path + ".bak-ingest"))

        d2, l2 = ingest_env(good.replace("VERDICT: CONFIRMED", "VERDICT: PENDING"))
        bad_path = write(os.path.join(d2, "ledger.json"), json.dumps(l2))
        before = open(bad_path, "rb").read()
        out2, err2 = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out2), contextlib.redirect_stderr(err2):
            code2 = anchor.cmd_ingest(SimpleNamespace(ledger=bad_path, inconclusive=None,
                                                       reason=None))
        self.assertEqual(code2, 1)
        lines = err2.getvalue().splitlines()
        self.assertTrue(lines)
        self.assertTrue(all(line.startswith("error: ") for line in lines), lines)
        self.assertEqual(open(bad_path, "rb").read(), before)
        self.assertFalse(os.path.isfile(bad_path + ".bak-ingest"))

    def test_cmd_ingest_rejects_a_non_integer_inconclusive_token_without_a_traceback(self):
        good = GOOD.replace("BRIEF: abc", f"BRIEF: {lt.template_hash()}")
        d, l = ingest_env(good)
        ledger_path = write(os.path.join(d, "ledger.json"), json.dumps(l))
        before = open(ledger_path, "rb").read()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = anchor.cmd_ingest(SimpleNamespace(ledger=ledger_path, inconclusive="abc",
                                                      reason="whatever"))
        self.assertEqual(code, 1)
        lines = err.getvalue().splitlines()
        self.assertTrue(lines)
        self.assertTrue(all(line.startswith("error: ") for line in lines), lines)
        self.assertEqual(open(ledger_path, "rb").read(), before)
        self.assertFalse(os.path.isfile(ledger_path + ".bak-ingest"))


class TestBriefCli(unittest.TestCase):
    def test_cmd_brief_writes_md_and_json_and_prints_the_md_path(self):
        d = tmpdir()
        ledger_path = write(os.path.join(d, "ledger.json"), json.dumps(brief_ledger()))
        context_path = write(os.path.join(d, "context.json"), json.dumps(CTX))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = anchor.cmd_brief(SimpleNamespace(ledger=ledger_path, key="C1",
                                                     context=context_path))
        self.assertEqual(code, 0, err.getvalue())
        md_path = os.path.join(d, "briefs", "C1.md")
        json_path = os.path.join(d, "briefs", "C1.json")
        self.assertTrue(os.path.isfile(md_path))
        self.assertTrue(os.path.isfile(json_path))
        self.assertEqual(out.getvalue().strip(), md_path)
        with open(md_path, encoding="utf-8") as fh:
            self.assertIn("check the second site", fh.read())
        with open(json_path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"key": "C1", "owns": [1, 2],
                                              "hash": lt.template_hash(),
                                              "run": lt.run_id(brief_ledger())})


class TestExcerptAndChangedLines(unittest.TestCase):
    def setUp(self):
        self.wt = tmpdir()
        write(os.path.join(self.wt, "a.ts"), "l1\nl2\nl3\nl4\nl5\n")

    def test_excerpt_cuts_line_to_line_end(self):
        l = led(item(1, line=2, line_end=4, anchor_excerpt="paraphrase ..."))
        lt.fill_excerpts(l, self.wt)
        self.assertEqual(l["items"][0]["anchor_excerpt"], "l2\nl3\nl4")

    def test_excerpt_errors_name_the_id_and_leave_the_ledger_alone(self):
        l = led(item(1, line=2, line_end=2), item(2, sev="low", line=4, line_end=9))
        before = json.dumps(l, sort_keys=True)
        with self.assertRaisesRegex(lt.LedgerError, r"item 2: .*beyond end of file"):
            lt.fill_excerpts(l, self.wt)
        self.assertEqual(json.dumps(l, sort_keys=True), before)

    def test_first_line_changed_is_on_changed_line(self):
        l = led(item(1, line=2, line_end=3, anchor_excerpt="l2\nl3"))
        lt.mark_changed_lines(l, self.wt, {"a.ts": {2}})
        self.assertIs(l["items"][0]["on_changed_line"], True)

    def test_range_moves_onto_its_first_changed_line_and_recuts(self):
        l = led(item(1, line=2, line_end=4, anchor_excerpt="l2\nl3\nl4"))
        notes = lt.mark_changed_lines(l, self.wt, {"a.ts": {3, 4}})
        it = l["items"][0]
        self.assertEqual((it["line"], it["anchor_excerpt"], it["on_changed_line"]),
                         (3, "l3\nl4", True))
        self.assertTrue(any("#1" in n and "2 -> 3" in n for n in notes), notes)

    def test_first_changed_line_searches_the_closed_range(self):
        self.assertEqual(lt.first_changed_line({3, 4}, 2, 4), 3)
        self.assertEqual(lt.first_changed_line({2, 3}, 2, 4), 2)
        self.assertEqual(lt.first_changed_line({4}, 2, 4), 4)
        self.assertIsNone(lt.first_changed_line({1, 5}, 2, 4))

    def test_nothing_changed_in_range_is_off_diff_and_flip_is_reported(self):
        l = led(item(1, line=2, line_end=3, anchor_excerpt="l2\nl3", on_changed_line=True))
        notes = lt.mark_changed_lines(l, self.wt, {"a.ts": {5}})
        self.assertIs(l["items"][0]["on_changed_line"], False)
        self.assertTrue(any("True -> False" in n for n in notes), notes)

    def test_file_absent_from_the_diff_is_off_diff(self):
        l = led(item(1, line=2, anchor_excerpt="l2"))
        lt.mark_changed_lines(l, self.wt, {})
        self.assertIs(l["items"][0]["on_changed_line"], False)

    def test_changed_lines_errors_name_the_id_and_leave_the_ledger_alone(self):
        # Not in the brief's Step 1 list: closes the same "no traceback" gap
        # Task 4's ingest() had for --inconclusive ids missing from by_id -
        # here an item missing 'file' must raise a named LedgerError instead
        # of a bare KeyError escaping mark_changed_lines uncaught.
        l = led(item(1, line=2, line_end=3, anchor_excerpt="l2\nl3"))
        del l["items"][0]["file"]
        before = json.dumps(l, sort_keys=True)
        with self.assertRaisesRegex(lt.LedgerError, r"item 1: .*'file'"):
            lt.mark_changed_lines(l, self.wt, {"a.ts": {2}})
        self.assertEqual(json.dumps(l, sort_keys=True), before)


class TestExcerptAndChangedLinesCli(unittest.TestCase):
    def test_cmd_excerpt_writes_the_ledger_and_a_backup_on_success_and_leaves_it_untouched_on_failure(self):
        d = tmpdir()
        write(os.path.join(d, "a.ts"), "l1\nl2\nl3\n")
        l = led(item(1, line=1, line_end=2, anchor_excerpt="stale"))
        ledger_path = write(os.path.join(d, "ledger.json"), json.dumps(l))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = anchor.cmd_excerpt(SimpleNamespace(ledger=ledger_path, worktree=d))
        self.assertEqual(code, 0, err.getvalue())
        written = json.load(open(ledger_path))
        self.assertEqual(written["items"][0]["anchor_excerpt"], "l1\nl2")
        self.assertTrue(os.path.isfile(ledger_path + ".bak-excerpt"))

        d2 = tmpdir()
        write(os.path.join(d2, "a.ts"), "l1\nl2\n")
        bad = led(item(1, line=1, line_end=9, anchor_excerpt="stale"))
        bad_path = write(os.path.join(d2, "ledger.json"), json.dumps(bad))
        before = open(bad_path, "rb").read()
        out2, err2 = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out2), contextlib.redirect_stderr(err2):
            code2 = anchor.cmd_excerpt(SimpleNamespace(ledger=bad_path, worktree=d2))
        self.assertEqual(code2, 1)
        lines = err2.getvalue().splitlines()
        self.assertTrue(lines)
        self.assertTrue(all(line.startswith("error: ") for line in lines), lines)
        self.assertEqual(open(bad_path, "rb").read(), before)
        self.assertFalse(os.path.isfile(bad_path + ".bak-excerpt"))

    def test_cmd_changed_lines_writes_the_ledger_and_a_backup_on_success_and_leaves_it_untouched_on_missing_diff(self):
        d = tmpdir()
        write(os.path.join(d, "a.ts"), "l1\nl2\nl3\n")
        l = led(item(1, line=2, line_end=2, anchor_excerpt="l2"))
        ledger_path = write(os.path.join(d, "ledger.json"), json.dumps(l))
        diff_path = write(os.path.join(d, "head.diff"),
                          "--- a/a.ts\n+++ b/a.ts\n@@ -1,3 +1,3 @@\n l1\n+l2\n l3\n")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = anchor.cmd_changed_lines(SimpleNamespace(ledger=ledger_path, diff=diff_path,
                                                             worktree=d))
        self.assertEqual(code, 0, err.getvalue())
        written = json.load(open(ledger_path))
        self.assertIs(written["items"][0]["on_changed_line"], True)
        self.assertTrue(os.path.isfile(ledger_path + ".bak-changed-lines"))

        before = open(ledger_path, "rb").read()
        bak_before = open(ledger_path + ".bak-changed-lines", "rb").read()
        out2, err2 = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out2), contextlib.redirect_stderr(err2):
            code2 = anchor.cmd_changed_lines(SimpleNamespace(
                ledger=ledger_path, diff=os.path.join(d, "no-such.diff"), worktree=d))
        self.assertEqual(code2, 1)
        lines = err2.getvalue().splitlines()
        self.assertEqual(lines, [f"error: diff file not found: {os.path.join(d, 'no-such.diff')}"])
        self.assertEqual(open(ledger_path, "rb").read(), before)
        self.assertEqual(open(ledger_path + ".bak-changed-lines", "rb").read(), bak_before)


class TestRenumber(unittest.TestCase):
    def test_sorts_maps_tokens_clusters_and_keeps_old_id(self):
        l = led(item(1, sev="low", f="b.ts", detail="**Why it matters:** see {{#2}}"),
                item(2, sev="high", f="a.ts", cluster="C1"),
                clusters=[{"id": "C1", "title": "t", "shared_fix": "s", "item_ids": [2]}])
        mapping = lt.renumber(l)
        self.assertEqual(mapping, {2: 1, 1: 2})
        by_new = {it["id"]: it for it in l["items"]}
        self.assertEqual(by_new[2]["detail"], "**Why it matters:** see {{#1}}")
        self.assertEqual(by_new[2]["old_id"], 1)
        self.assertEqual(l["clusters"][0]["item_ids"], [1])
        self.assertEqual(anchor.validate(l), [])

    def test_tokens_in_verdict_reason_are_renumbered_and_checked(self):
        l = led(item(1, sev="low", f="b.ts", verdict_reason="same guard as {{#2}}"),
                item(2, sev="high", f="a.ts"))
        lt.renumber(l)
        self.assertEqual({it["id"]: it["verdict_reason"] for it in l["items"]}[2],
                         "same guard as {{#1}}")
        bad = led(item(1, verdict_reason="see {{#9}}"))
        with self.assertRaisesRegex(lt.LedgerError, r"item 1 references \{\{#9\}\}"):
            lt.renumber(bad)

    def test_running_twice_keeps_the_first_old_id(self):
        l = led(item(1, sev="low"), item(2, sev="high"))
        lt.renumber(l); lt.renumber(l)
        self.assertEqual(sorted(it["old_id"] for it in l["items"]), [1, 2])
        self.assertEqual({it["id"]: it["old_id"] for it in l["items"]}, {1: 2, 2: 1})

    def test_unknown_token_fails_before_any_change(self):
        l = led(item(1, detail="see {{#9}}"))
        before = json.dumps(l, sort_keys=True)
        with self.assertRaisesRegex(lt.LedgerError, r"item 1 references \{\{#9\}\}"):
            lt.renumber(l)
        self.assertEqual(json.dumps(l, sort_keys=True), before)


class TestEdit(unittest.TestCase):
    def test_replaces_text_byte_for_byte(self):
        l = led(item(1))
        lt.edit_field(l, 1, "detail", "don't touch `a`'s \"b\"\n")
        self.assertEqual(l["items"][0]["detail"], "don't touch `a`'s \"b\"")

    def test_refuses_non_text_fields_and_unknown_ids(self):
        for field in ("verdict", "severity", "severity_in", "id", "line"):
            with self.assertRaisesRegex(lt.LedgerError, "not editable"):
                lt.edit_field(led(item(1)), 1, field, "x")
        with self.assertRaisesRegex(lt.LedgerError, "no item 5"):
            lt.edit_field(led(item(1)), 5, "claim", "x")

    def test_reason_fields_are_replaced_in_full(self):
        for field in ("verdict_reason", "fix_reason"):
            l = led(item(1))
            text = "checked at a.ts:10.\n_Re-checked at abc123: still holds._\n"
            lt.edit_field(l, 1, field, text)
            self.assertEqual(l["items"][0][field], text.rstrip("\n"))

    def test_fix_verdict_can_only_be_demoted_to_unverifiable(self):
        l = led(item(1, fix_verdict="SOUND"))
        lt.edit_field(l, 1, "fix_verdict", "UNVERIFIABLE\n")
        self.assertEqual(l["items"][0]["fix_verdict"], "UNVERIFIABLE")
        for value in ("SOUND", "REWRITTEN", "unverifiable", ""):
            l = led(item(1, fix_verdict="UNVERIFIABLE"))
            with self.assertRaisesRegex(lt.LedgerError, "demoted, never promoted"):
                lt.edit_field(l, 1, "fix_verdict", value)
            self.assertEqual(l["items"][0]["fix_verdict"], "UNVERIFIABLE")


class TestRenumberCli(unittest.TestCase):
    def test_cmd_renumber_writes_the_ledger_and_a_backup_on_success_and_leaves_it_untouched_on_failure(self):
        l = led(item(1, sev="low", f="b.ts", detail="see {{#2}}"), item(2, sev="high", f="a.ts"))
        d = tmpdir()
        ledger_path = write(os.path.join(d, "ledger.json"), json.dumps(l))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = anchor.cmd_renumber(SimpleNamespace(ledger=ledger_path))
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(out.getvalue().strip().splitlines(), ["1 -> 2", "2 -> 1"])
        written = json.load(open(ledger_path))
        by_new = {it["id"]: it for it in written["items"]}
        self.assertEqual(by_new[2]["detail"], "see {{#1}}")
        self.assertTrue(os.path.isfile(ledger_path + ".bak-renumber"))

        bad = led(item(1, detail="see {{#9}}"))
        bad_path = write(os.path.join(d, "bad.json"), json.dumps(bad))
        before = open(bad_path, "rb").read()
        out2, err2 = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out2), contextlib.redirect_stderr(err2):
            code2 = anchor.cmd_renumber(SimpleNamespace(ledger=bad_path))
        self.assertEqual(code2, 1)
        lines = err2.getvalue().splitlines()
        self.assertTrue(lines)
        self.assertTrue(all(line.startswith("error: ") for line in lines), lines)
        self.assertEqual(open(bad_path, "rb").read(), before)
        self.assertFalse(os.path.isfile(bad_path + ".bak-renumber"))


class TestEditCli(unittest.TestCase):
    def test_cmd_edit_writes_the_ledger_and_a_backup_on_success_and_leaves_it_untouched_on_failure(self):
        l = led(item(1))
        d = tmpdir()
        ledger_path = write(os.path.join(d, "ledger.json"), json.dumps(l))
        text_path = write(os.path.join(d, "text.txt"), "don't touch `a`'s \"b\"\n")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = anchor.cmd_edit(SimpleNamespace(ledger=ledger_path, id="1", field="detail",
                                                    text_file=text_path))
        self.assertEqual(code, 0, err.getvalue())
        written = json.load(open(ledger_path))
        self.assertEqual(written["items"][0]["detail"], "don't touch `a`'s \"b\"")
        self.assertTrue(os.path.isfile(ledger_path + ".bak-edit"))

        bad_path = write(os.path.join(d, "bad.json"), json.dumps(led(item(1))))
        before = open(bad_path, "rb").read()
        out2, err2 = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out2), contextlib.redirect_stderr(err2):
            code2 = anchor.cmd_edit(SimpleNamespace(ledger=bad_path, id="1", field="severity",
                                                     text_file=text_path))
        self.assertEqual(code2, 1)
        lines = err2.getvalue().splitlines()
        self.assertTrue(lines)
        self.assertTrue(all(line.startswith("error: ") for line in lines), lines)
        self.assertIn("not editable", lines[0])
        self.assertEqual(open(bad_path, "rb").read(), before)
        self.assertFalse(os.path.isfile(bad_path + ".bak-edit"))

    def test_cmd_edit_rejects_a_non_integer_id_without_a_traceback(self):
        l = led(item(1))
        d = tmpdir()
        ledger_path = write(os.path.join(d, "ledger.json"), json.dumps(l))
        text_path = write(os.path.join(d, "text.txt"), "x")
        before = open(ledger_path, "rb").read()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = anchor.cmd_edit(SimpleNamespace(ledger=ledger_path, id="abc", field="detail",
                                                    text_file=text_path))
        self.assertEqual(code, 1)
        lines = err.getvalue().splitlines()
        self.assertTrue(lines)
        self.assertTrue(all(line.startswith("error: ") for line in lines), lines)
        self.assertEqual(open(ledger_path, "rb").read(), before)
        self.assertFalse(os.path.isfile(ledger_path + ".bak-edit"))

    def test_cmd_edit_rejects_a_non_utf8_text_file_without_a_traceback(self):
        l = led(item(1))
        d = tmpdir()
        ledger_path = write(os.path.join(d, "ledger.json"), json.dumps(l))
        text_path = os.path.join(d, "text.bin")
        with open(text_path, "wb") as fh:
            fh.write(b"\xff\xfe")
        before = open(ledger_path, "rb").read()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = anchor.cmd_edit(SimpleNamespace(ledger=ledger_path, id="1", field="detail",
                                                    text_file=text_path))
        self.assertEqual(code, 1)
        lines = err.getvalue().splitlines()
        self.assertTrue(lines)
        self.assertTrue(all(line.startswith("error: ") for line in lines), lines)
        self.assertIn("not valid UTF-8", lines[0])
        self.assertEqual(open(ledger_path, "rb").read(), before)
        self.assertFalse(os.path.isfile(ledger_path + ".bak-edit"))


class TestErrorContract(unittest.TestCase):
    """Every subcommand turns a bad input or a failed write into `error:`
    lines and exit 1 - never a traceback."""

    def assertCleanFailure(self, fn, **kw):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = fn(SimpleNamespace(**kw))
        lines = err.getvalue().splitlines()
        self.assertEqual(code, 1, err.getvalue())
        self.assertTrue(lines and all(l.startswith("error: ") for l in lines), lines)
        return err.getvalue()

    def cli_ingest_env(self, text):
        good = text.replace("BRIEF: abc", f"BRIEF: {lt.template_hash()}")
        d, l = ingest_env(good)
        return d, write(os.path.join(d, "ledger.json"), json.dumps(l))

    def readonly(self, d):
        os.chmod(d, 0o555)
        self.addCleanup(os.chmod, d, 0o755)

    def test_ingest_names_a_verdict_file_that_is_not_utf8(self):
        d, path = self.cli_ingest_env(GOOD)
        with open(os.path.join(d, "verdicts", "C1.txt"), "wb") as fh:
            fh.write(b"BRIEF: \xff\xfe\n")
        err = self.assertCleanFailure(anchor.cmd_ingest, ledger=path, inconclusive=None, reason=None)
        self.assertIn(f"C1: verdict file {os.path.join(d, 'verdicts', 'C1.txt')} is not valid UTF-8",
                      err)

    def test_ingest_names_a_meta_without_owns_or_owning_an_unknown_id(self):
        for meta in ({"key": "C1", "hash": "abc"}, {"key": "C1", "owns": [1, 99], "hash": "abc"}):
            d, path = self.cli_ingest_env(GOOD)
            write(os.path.join(d, "briefs", "C1.json"), json.dumps(meta))
            err = self.assertCleanFailure(anchor.cmd_ingest, ledger=path, inconclusive=None,
                                          reason=None)
            self.assertIn("C1: brief owns", err)

    def test_ingest_reports_a_ledger_it_cannot_write(self):
        d, path = self.cli_ingest_env(GOOD)
        self.readonly(d)
        err = self.assertCleanFailure(anchor.cmd_ingest, ledger=path, inconclusive=None, reason=None)
        self.assertIn("could not write ledger file", err)

    def test_excerpt_reports_a_ledger_it_cannot_write(self):
        d = tmpdir(); write(os.path.join(d, "a.ts"), "l1\nl2\n")
        path = write(os.path.join(d, "ledger.json"), json.dumps(led(item(1, line=1, line_end=1))))
        self.readonly(d)
        err = self.assertCleanFailure(anchor.cmd_excerpt, ledger=path, worktree=d)
        self.assertIn("could not write ledger file", err)

    def test_changed_lines_reports_a_ledger_it_cannot_write(self):
        d = tmpdir(); write(os.path.join(d, "a.ts"), "l1\nl2\n")
        path = write(os.path.join(d, "ledger.json"), json.dumps(led(item(1, line=1, line_end=1))))
        diff = write(os.path.join(d, "head.diff"), "--- a/a.ts\n+++ b/a.ts\n@@ -1 +1 @@\n+l1\n")
        self.readonly(d)
        err = self.assertCleanFailure(anchor.cmd_changed_lines, ledger=path, diff=diff, worktree=d)
        self.assertIn("could not write ledger file", err)

    def test_renumber_names_an_item_without_an_id_or_with_a_null_file(self):
        d = tmpdir()
        no_id = led(item(1), item(2, sev="low")); del no_id["items"][1]["id"]
        null_file = led(item(1, f=None), item(2, f="a.ts"))
        for bad, want in ((no_id, "item[1] has no integer id"), (null_file, None)):
            path = write(os.path.join(d, "ledger.json"), json.dumps(bad))
            if want:
                self.assertIn(want, self.assertCleanFailure(anchor.cmd_renumber, ledger=path))
            else:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(anchor.cmd_renumber(SimpleNamespace(ledger=path)), 0)

    def test_renumber_reports_a_ledger_it_cannot_write(self):
        d = tmpdir()
        path = write(os.path.join(d, "ledger.json"), json.dumps(led(item(1))))
        self.readonly(d)
        err = self.assertCleanFailure(anchor.cmd_renumber, ledger=path)
        self.assertIn("could not write ledger file", err)

    def test_edit_reports_a_ledger_it_cannot_write(self):
        d = tmpdir()
        path = write(os.path.join(d, "ledger.json"), json.dumps(led(item(1))))
        text = write(os.path.join(d, "t.txt"), "new claim")
        self.readonly(d)
        err = self.assertCleanFailure(anchor.cmd_edit, ledger=path, id="1", field="claim",
                                      text_file=text)
        self.assertIn("could not write ledger file", err)

    def test_report_survives_an_item_without_an_id_and_a_string_dedupe(self):
        d = tmpdir()
        l = report_ledger(); del l["items"][0]["id"]; l["items"][1]["dedupe"] = "see the old thread"
        path = write(os.path.join(d, "ledger.json"), json.dumps(l))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = anchor.cmd_report(SimpleNamespace(ledger=path))
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("see the old thread", out.getvalue())

    def test_brief_reports_a_briefs_directory_it_cannot_write(self):
        d = tmpdir()
        path = write(os.path.join(d, "ledger.json"), json.dumps(brief_ledger()))
        ctx = write(os.path.join(d, "ctx.json"), json.dumps(CTX))
        write(os.path.join(d, "briefs"), "a file where the directory should be")
        err = self.assertCleanFailure(anchor.cmd_brief, ledger=path, key="C1", context=ctx)
        self.assertIn(f"could not write the brief under {os.path.join(d, 'briefs')}", err)

    def test_validate_names_a_ledger_that_is_not_utf8(self):
        d = tmpdir()
        path = os.path.join(d, "ledger.json")
        with open(path, "wb") as fh:
            fh.write(b'{"schema": 2, "items": [], "x": "\xff"}')
        err = self.assertCleanFailure(anchor.cmd_validate, ledger=path, worktree=None, draft=False)
        self.assertIn("not valid UTF-8", err)

    def test_validate_names_a_null_file_instead_of_failing_to_sort(self):
        d = tmpdir()
        path = write(os.path.join(d, "ledger.json"),
                     json.dumps(led(item(1, f=None), item(2, f="a.ts"))))
        err = self.assertCleanFailure(anchor.cmd_validate, ledger=path, worktree=None, draft=False)
        self.assertIn("missing required field 'file'", err)


def report_ledger():
    return led(
        item(1, verdict="CONFIRMED", claim_verdict="REWRITTEN", claim_original="Old claim.",
             refuter_key="C1", cluster="C1", on_changed_line=True),
        item(2, sev="medium", severity_in="high", verdict="DOWNGRADED", fold_note="folded",
             refuter_key="C1", cluster="C1", on_changed_line=True,
             fix_verdict="REWRITTEN", fix_original="old fix", detail="**Fix:** new fix"),
        item(3, sev="low", verdict="INCONCLUSIVE", fix_verdict="UNVERIFIABLE",
             refuter_key="i3", on_changed_line=False),
        item(4, sev="low", verdict="REFUTED", refuter_key="i4", on_changed_line=True),
        clusters=[{"id": "C1", "title": "t", "shared_fix": "s", "item_ids": [1, 2]}],
        target={"kind": "pr", "repo": "o/r", "number": 13}, worktree="/wt")


class TestReport(unittest.TestCase):
    def setUp(self):
        self.text = lt.render_report(report_ledger(), "/s/pr-review/pr13/ledger.json")

    def test_has_every_section(self):
        for heading in ("Triage", "Verdict summary", "Claim audit", "Fix audit", "Downgraded",
                        "Unresolved", "Refuted (not posted unless you name them)",
                        "Previously reported", "Pressure-test dispatch", "Files", "To post"):
            self.assertIn(heading, self.text, heading)

    def test_triage_shows_verdict_and_severity_arrow(self):
        self.assertRegex(self.text, r"\| 2 \| high -> medium \| DOWNGRADED \|")

    def test_audits_show_originals(self):
        self.assertIn("Old claim.", self.text)
        self.assertIn("old fix", self.text)
        self.assertIn("folded", self.text)

    def test_counts_and_dispatch(self):
        self.assertIn("CONFIRMED 1 / DOWNGRADED 1 / REFUTED 1 / INCONCLUSIVE 1", self.text)
        self.assertIn("3 refuters for 4 items, largest owned 2", self.text)
        self.assertIn("/pr-review-post all", self.text)
        self.assertIn("2 inline + 1 in the body", self.text)

    def test_calibration_warnings(self):
        l = report_ledger()
        l["items"][3].update(verdict="CONFIRMED", claim_verdict="SOUND", fix_verdict="SOUND")
        text = lt.render_report(l, "/s/l.json")
        self.assertIn("warning: nothing was refuted", text)
        self.assertIn("warning: 1 lowered / 0 raised", text)

    def test_inline_count_agrees_with_build_payload_for_a_never_reanchored_item(self):
        # Fix round 1 repro #1: an item that never went through S5 or
        # reanchor has neither anchor_status nor on_changed_line set.
        # build_payload posts it inline (see demotion_reason's docstring),
        # so the count must match rather than reading it as body-only.
        it = item(1)
        it.pop("on_changed_line", None)
        text = lt.render_report(led(it), "/s/l.json")
        self.assertIn("1 inline + 0 in the body", text)

    def test_inline_count_agrees_with_build_payload_for_a_dead_anchor(self):
        # Fix round 1 repro #2: on_changed_line: true but anchor_status:
        # "dead" - build_payload demotes this to the review body (a dead
        # anchor is checked before on_changed_line), so it must count as
        # body, not inline.
        text = lt.render_report(
            led(item(1, anchor_status="dead", on_changed_line=True)), "/s/l.json")
        self.assertIn("0 inline + 1 in the body", text)

    def test_no_raw_html(self):
        self.assertNotRegex(self.text, r"<(details|summary|br)\b")

    def test_a_pipe_in_a_claim_is_escaped_so_it_cannot_break_the_triage_table(self):
        l = report_ledger()
        l["items"][0]["claim"] = "compares a | b incorrectly"
        text = lt.render_report(l, "/s/l.json")
        self.assertIn(r"compares a \| b incorrectly", text)
        # The escaped pipe must not be read as a new table cell: once escaped
        # pipes are removed, the triage row for item 1 still has exactly 6
        # cells (7 real column delimiters).
        row = next(ln for ln in text.splitlines() if ln.startswith("| 1 |"))
        self.assertEqual(row.replace("\\|", "").count("|"), 7, row)


class TestReportCli(unittest.TestCase):
    def test_cmd_report_writes_report_md_and_prints_the_same_text(self):
        d = tmpdir()
        ledger_path = write(os.path.join(d, "ledger.json"), json.dumps(report_ledger()))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = anchor.cmd_report(SimpleNamespace(ledger=ledger_path))
        self.assertEqual(code, 0, err.getvalue())
        report_path = os.path.join(d, "report.md")
        self.assertTrue(os.path.isfile(report_path))
        with open(report_path, encoding="utf-8") as fh:
            written = fh.read()
        self.assertEqual(written, out.getvalue())
        self.assertIn("## Triage", written)

    def test_cmd_report_rejects_a_missing_ledger_without_a_traceback(self):
        d = tmpdir()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = anchor.cmd_report(SimpleNamespace(ledger=os.path.join(d, "nope.json")))
        self.assertEqual(code, 1)
        lines = err.getvalue().splitlines()
        self.assertTrue(lines)
        self.assertTrue(all(line.startswith("error: ") for line in lines), lines)
        self.assertEqual(out.getvalue(), "")
        self.assertFalse(os.path.isfile(os.path.join(d, "report.md")))

    def test_cmd_report_reports_a_write_failure_without_a_traceback(self):
        d = tmpdir()
        ledger_path = write(os.path.join(d, "ledger.json"), json.dumps(report_ledger()))
        # A directory already sitting at the report path makes the write
        # fail with IsADirectoryError (an OSError subclass) instead of
        # succeeding, without needing filesystem permission tricks.
        os.makedirs(os.path.join(d, "report.md"))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = anchor.cmd_report(SimpleNamespace(ledger=ledger_path))
        self.assertEqual(code, 1)
        lines = err.getvalue().splitlines()
        self.assertTrue(lines)
        self.assertTrue(all(line.startswith("error: ") for line in lines), lines)
        self.assertEqual(out.getvalue(), "")


class TestAuditRegression(unittest.TestCase):
    def test_every_audited_defect_is_now_rejected(self):
        cases = json.load(open(os.path.join(HERE, "tests", "fixtures", "audit-defects.json")))
        self.assertGreaterEqual(len(cases), 6)
        for c in cases:
            it = dict(c["item"]); it["id"] = 1
            errs = anchor.validate(led(it))
            self.assertTrue(any(c["expect"] in e for e in errs),
                            f"source {c['source']} item {c['item']['id']}: {errs}")
