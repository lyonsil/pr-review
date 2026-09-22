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
        it = item(1, fix_verdict="REWRITTEN", detail="**Fix:** new")
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
            self.assertEqual(anchor.validate(led(item(1, detail="**Fix:** " + fix))), [], fix)


class TestPrimitives(unittest.TestCase):
    def test_anchor_reexports_the_moved_primitives(self):
        self.assertIs(anchor.parse_changed_lines, lt.parse_changed_lines)
        self.assertIs(anchor.fix_slot, lt.fix_slot)
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


class TestSchema(unittest.TestCase):
    def test_schema_1_is_rejected_with_a_rerun_instruction(self):
        errs = anchor.validate({"schema": 1, "items": [], "clusters": []})
        self.assertTrue(any("schema 1" in e and "re-run /pr-review" in e for e in errs), errs)

    def test_missing_schema_is_rejected(self):
        errs = anchor.validate({"items": [], "clusters": []})
        self.assertTrue(any("schema" in e for e in errs), errs)
