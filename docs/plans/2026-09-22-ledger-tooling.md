# Ledger Tooling and Refuter Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the mechanical steps `/pr-review` currently redoes by hand (ledger assembly,
excerpts, changed-line derivation, renumbering, refuter briefs and replies, reports, preview
display) with deterministic, tested subcommands, and rewrite the two command prompts around them.

**Architecture:** `pr-review-anchor.py` stays the only CLI the prompts call. New logic lives in
`ledger_tools.py` beside it; shared primitives (`SEVERITIES`, `parse_changed_lines`, `fix_slot`)
move there and are re-exported by `pr-review-anchor.py` so existing tests keep addressing them as
`anchor.X`. `pr-review-wt.sh` gains `remove-refuters`, `primary-status`, a reaper exception for
refuter worktrees, and pnpm-aware `link-deps`. A shipped `refuter-brief.tmpl.md` replaces
hand-written refuter prompts.

**Tech Stack:** Python 3.10 standard library only; bash; git; pytest 9 as the runner for
`unittest`-style tests.

**Spec:** `docs/specs/2026-09-22-ledger-tooling-design.md` (this repo). Read it before starting.

## Global Constraints

- Python standard library only; no pip installs. Target Python 3.10.
- Every guard is mutation-proven: a test that still passes with its guard deleted is a defect. When
  a step adds a guard, run the test with the guard commented out once and confirm it fails.
- All errors are one `error: ...` line per problem on stderr, exit 1, no traceback.
- Ledger-writing subcommands write atomically and leave `<ledger>.bak-<subcommand>`.
- `"schema": 2` only.
- American English in every file this plan touches (behavior, color, honor, labeled, judgment,
  flavored, neighbor).
- No issue-tracker ids or PR numbers in code comments.
- Commit on branch `ledger-tooling-v2`, at the end of each task. Never push.
- Run the whole suite at the end of every task:
  `cd ~/.claude/pr-review && python3 -m pytest -q && bash pr-review-wt.test.sh | tail -1`.
  Baseline before Task 1: `147 passed`, `passed=60 failed=0`.

## Review Focus

1. A single inline comment larger than the 15 KB page limit - expected: it gets a page of its own,
   is never split, and the page header says it exceeds the limit (Task 9).
2. A claim containing a code span with a period (`` `a.b()` ``) or an abbreviation (`e.g.`) -
   expected: not counted as two sentences (Task 2).
3. An anchor excerpt containing a spread operator (`...args`) - expected: not rejected as an
   ellipsis line (Task 2).
4. A `{{#id}}` token naming a finding that is REFUTED or not selected for posting - expected: it
   still renders as `#n`, and `payload` warns on stderr that the reference points outside the
   review (Task 8).
5. `excerpt` / `changed-lines` on an item whose file is gone or whose `line_end` is beyond EOF -
   expected: one `error:` line naming the id, no traceback, ledger untouched (Task 5).

## File map

| File | Responsibility |
| --- | --- |
| `ledger_tools.py` (new) | shared primitives, atomic write, verdict-file parser, `brief`, `ingest`, `excerpt`, `changed-lines`, `renumber`, `edit`, `report`, preview paging |
| `pr-review-anchor.py` | CLI; `validate`, `reanchor`, `payload`, `preview` (existing, changed); subcommand wiring for everything in `ledger_tools.py` |
| `refuter-brief.tmpl.md` (new) | the refuter's whole contract, with `{{PLACEHOLDERS}}` |
| `test_ledger_tools.py` (new) | tests for `ledger_tools.py` |
| `test_pr_review_anchor.py` | existing tests, audited and updated |
| `tests/fixtures/*.json` (new) | real-data regression ledgers |
| `pr-review-wt.sh`, `pr-review-wt.test.sh` | worktree script and its tests |
| `commands/pr-review.md`, `commands/pr-review-post.md` | prompts, rewritten around the tools |
| `README.md`, `DECISIONS.md` | docs |

## Existing-test audit

Every existing test was classified against the spec. Tasks apply the non-keep rows.

| Test(s) | Class | Why | Task |
| --- | --- | --- | --- |
| Every test constructing `{"items": ..., "clusters": ...}` passed to `validate` | rewrite (mechanical) | `validate` now requires `"schema": 2` | 1 |
| `TestReanchorFixesOnChangedLine._run` (`"schema": 1`) | rewrite | same | 1 |
| `TestTerminalVerdictRequired.test_each_terminal_verdict_is_accepted` | fix + rewrite | loops over the constant it guards; DOWNGRADED now needs a lower severity and `fold_note` | 1, 2 |
| `TestFixVerdictRequired.test_each_fix_verdict_is_accepted` | fix + rewrite | loops over `anchor.FIX_VERDICTS`; REWRITTEN now needs `fix_original` | 1, 2 |
| `TestFixVerdictRequired.test_each_claim_verdict_is_accepted` | rewrite | REWRITTEN now needs `claim_original` | 2 |
| `TestReanchor.test_anchor_not_on_a_changed_line_is_dead` | rewrite | status is now `off-diff` | 7 |
| `TestReanchorFixesOnChangedLine.test_a_dead_anchor_is_off_diff_by_definition` | rewrite | a dead excerpt no longer flips `on_changed_line` | 7 |
| `TestReanchorCli.test_end_to_end_rewrites_ledger_with_status_and_line` | rewrite | summary line gains `off-diff=` | 7 |
| All other `test_pr_review_anchor.py` tests (TestValidate, TestCliErrorHandling, TestParseChangedLines*, TestReanchorAmbiguity, TestReanchorWhitespace, remaining TestReanchorCli, TestPayload, TestIncludeRefuted*, TestOnChangedLineExclusion, TestDemotedFindings, TestEmptyPayloadIsFatal, TestPayloadCli, TestRenderPreview, TestPreviewCli, TestFixSlot*, TestBodyOnlyVerdicts) | keep | guard still in force | - |
| All 60 checks in `pr-review-wt.test.sh` | keep | guards unchanged; new checks are added | - |
| delete | none | No existing test pins a removed behavior: the cheaper-model allowance and schema-1 acceptance were never tested. Re-confirm during Task 1 by running the suite after the schema change; any test that can only pass against schema 1 is deleted and named in the commit message. | 1 |

---

### Task 1: Module split, schema 2, test harness

**Files:**
- Create: `ledger_tools.py`
- Modify: `pr-review-anchor.py:1-6, 136-149, 162-165, 267-285`
- Create: `test_ledger_tools.py`
- Modify: `test_pr_review_anchor.py` (schema in fixtures, two constant-loop tests)

**Interfaces:**
- Produces (in `ledger_tools`): `SCHEMA = 2`, `SEVERITIES`, `SEV_RANK: dict[str, int]`,
  `class LedgerError(ValueError)`, `fix_slot(detail) -> str`,
  `parse_changed_lines(diff_text) -> dict[str, set[int]]`,
  `load_json(path, what="ledger") -> dict` (raises `LedgerError`),
  `write_ledger(path, ledger, step) -> None`,
  `read_lines(worktree, file, start, end) -> list[str]` (raises `LedgerError`).
- `pr-review-anchor.py` re-exports `SEVERITIES`, `fix_slot`, `parse_changed_lines` and exposes the
  module as `anchor.lt`.

- [ ] **Step 1: Write failing tests for the new primitives**

Create `test_ledger_tools.py`:

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `cd ~/.claude/pr-review && python3 -m pytest -q test_ledger_tools.py`
Expected: FAIL / collection error, `No module named 'ledger_tools'`.

- [ ] **Step 3: Create `ledger_tools.py` and move the primitives**

```python
"""Deterministic ledger steps for the PR review pipeline. Standard library only.

pr-review-anchor.py is the only CLI the command prompts call; it imports this
module and re-exports the shared primitives so both files agree on them."""
import hashlib, json, os, re, shutil, tempfile

SCHEMA = 2
SEVERITIES = ("blocker", "high", "medium", "low")
SEV_RANK = {s: n for n, s in enumerate(SEVERITIES)}


class LedgerError(ValueError):
    """A deterministic ledger step cannot proceed. The message is user-facing
    and is printed as one `error: ...` line per line of the message."""


def fix_slot(detail):
    # (move the body of pr-review-anchor.py's fix_slot here unchanged, docstring included)


def parse_changed_lines(diff_text):
    # (move the body of pr-review-anchor.py's parse_changed_lines here unchanged)


def load_json(path, what="ledger"):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise LedgerError(f"{what} file not found: {path}")
    except OSError as exc:
        raise LedgerError(f"could not read {what} file {path}: {exc}")
    except json.JSONDecodeError as exc:
        raise LedgerError(f"{what} file {path} is not valid JSON: {exc}")
    if not isinstance(data, dict):
        raise LedgerError(f"{what} root must be a JSON object, got {type(data).__name__}")
    return data


def write_ledger(path, ledger, step):
    """Write atomically (temp file in the same directory, then rename), keeping
    the previous content as `<path>.bak-<step>` so a bad step can be inspected
    and a failed one never leaves a half-written ledger."""
    d = os.path.dirname(os.path.abspath(path))
    if os.path.exists(path):
        shutil.copyfile(path, f"{path}.bak-{step}")
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".ledger-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(ledger, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def read_lines(worktree, file, start, end):
    """Lines start..end (1-based, inclusive) of `file` in `worktree`, without
    line terminators."""
    if end < start:
        raise LedgerError(f"{file}: line_end {end} is before line {start}")
    path = os.path.join(worktree, file)
    if not os.path.isfile(path):
        raise LedgerError(f"{file}: not found in {worktree}")
    with open(path, encoding="utf-8", errors="replace") as fh:
        content = fh.read().splitlines()
    if end > len(content):
        raise LedgerError(f"{file}: line {end} is beyond end of file ({len(content)} lines)")
    return content[start - 1:end]
```

In `pr-review-anchor.py`, replace line 4-6 with:

```python
import argparse, json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger_tools as lt  # noqa: E402
from ledger_tools import SEVERITIES, fix_slot, parse_changed_lines  # noqa: E402,F401
```

Delete the old `SEVERITIES`, `fix_slot` and `parse_changed_lines` definitions from
`pr-review-anchor.py`. At the top of `validate()`, after the non-dict check, add:

```python
    if ledger.get("schema") != lt.SCHEMA:
        return [f"ledger schema {ledger.get('schema')!r} is not supported - this is schema "
                f"{ledger.get('schema')} and {lt.SCHEMA} is required; re-run /pr-review"]
```

(The message must contain `schema 1` when the value is 1; the f-string above renders
`this is schema 1`.)

- [ ] **Step 4: Update existing test fixtures to schema 2**

```bash
cd ~/.claude/pr-review
sed -i 's/{"items": \[/{"schema": 2, "items": [/g; s/"schema": 1,/"schema": 2,/g' test_pr_review_anchor.py
grep -c '"schema": 2' test_pr_review_anchor.py
grep -nE '\{"items": [a-z]' test_pr_review_anchor.py   # ledgers built from a variable; add "schema": 2 by hand
```

Then fix the two constant-loop tests. Replace the body of
`test_each_terminal_verdict_is_accepted` and `test_each_fix_verdict_is_accepted`:

```python
    def test_each_terminal_verdict_is_accepted(self):
        """Literals, not anchor.VERDICTS: iterating the constant under test
        shrinks with it, so narrowing the accepted set would still pass."""
        for v, extra in (("CONFIRMED", {}), ("REFUTED", {}), ("INCONCLUSIVE", {}),
                         ("DOWNGRADED", {"severity": "low", "severity_in": "high",
                                         "fold_note": "folded"})):
            led = {"schema": 2, "items": [item(1, verdict=v, **extra)], "clusters": []}
            self.assertEqual(anchor.validate(led), [], f"{v} should be legal")
```

```python
    def test_each_fix_verdict_is_accepted(self):
        """Literals, not anchor.FIX_VERDICTS - see test_each_claim_verdict_is_accepted."""
        for v in ("SOUND", "REWRITTEN", "UNVERIFIABLE"):
            led = {"schema": 2, "items": [item(1, fix_verdict=v)], "clusters": []}
            self.assertEqual(anchor.validate(led), [], f"{v} should be legal")
```

(DOWNGRADED and REWRITTEN extras only become necessary in Task 2; they are harmless now.)

- [ ] **Step 5: Run the whole suite**

Run: `cd ~/.claude/pr-review && python3 -m pytest -q && bash pr-review-wt.test.sh | tail -1`
Expected: `153 passed` (147 + 6 new), `passed=60 failed=0`. If any existing test fails only
because it can pass solely against schema 1, delete it and record its name for the commit message.

- [ ] **Step 6: Mutation check**

Comment out the schema check in `validate()`; `test_schema_1_is_rejected_with_a_rerun_instruction`
and `test_missing_schema_is_rejected` must fail. Restore.

- [ ] **Step 7: Commit**

```bash
git add ledger_tools.py pr-review-anchor.py test_ledger_tools.py test_pr_review_anchor.py
git commit -m "refactor: move shared ledger primitives into ledger_tools, require schema 2

Test audit: fixtures moved to schema 2; the terminal- and fix-verdict acceptance
tests now list verdicts literally instead of looping over the constants they guard.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Stricter `validate`

**Files:**
- Modify: `pr-review-anchor.py` (`HEDGE_PATTERNS`, `validate`, `cmd_validate`, `main`)
- Modify: `ledger_tools.py` (lint helpers)
- Test: `test_ledger_tools.py`, `test_pr_review_anchor.py`

**Interfaces:**
- Consumes: Task 1 primitives.
- Produces (in `ledger_tools`): `strip_code_spans(text) -> str`,
  `process_vocabulary_in(text) -> list[str]`, `claim_shape_errors(claim) -> list[str]`,
  `is_ellipsis_line(line) -> bool`, `excerpt_errors(ledger, worktree) -> list[str]`.
- `anchor.validate(ledger, worktree=None) -> list[str]`; CLI `validate <ledger> [--worktree WT]`.

- [ ] **Step 1: Write failing tests**

Append to `test_ledger_tools.py` (reuse the `item` builder from the anchor tests by importing it):

```python
from test_pr_review_anchor import item  # noqa: E402


def led(*items, **root):
    d = {"schema": 2, "items": list(items), "clusters": []}
    d.update(root); return d


class TestProcessVocabulary(unittest.TestCase):
    def test_flags_review_process_words(self):
        for text in ("Note for the refuter: check X", "Item #8 in this batch narrows it",
                     "see item #7's fix", "the pressure-test showed", "same as #4"):
            self.assertTrue(lt.process_vocabulary_in(text), text)

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
```

Then update the existing `test_each_claim_verdict_is_accepted` in `test_pr_review_anchor.py`:

```python
        for v, extra in (("SOUND", {}), ("REWRITTEN", {"claim_original": "older claim"})):
            led = {"schema": 2, "items": [item(1, claim_verdict=v, **extra)], "clusters": []}
            self.assertEqual(anchor.validate(led), [], f"{v} should be legal")
```

and in `test_each_fix_verdict_is_accepted` pass `fix_original="older fix"` when `v == "REWRITTEN"`.

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest -q test_ledger_tools.py -k "Vocabulary or ClaimShape or Excerpts or Semantics or NewHedges"`
Expected: FAIL (`AttributeError: module 'ledger_tools' has no attribute ...`).

- [ ] **Step 3: Implement the helpers in `ledger_tools.py`**

```python
_CODE_SPAN = re.compile(r"`[^`\n]*`")
PROCESS_PATTERNS = (
    r"\brefuters?\b",
    r"\bpressure-test",
    r"\bNote for the\b",
    r"\bthis batch\b",
    r"\bitems?\s+#\d+",
    r"(?<![\w{])#\d+\b(?!\}\})",
)
_ABBREV = re.compile(r"\b(?:e\.g|i\.e|etc|vs|cf)\.", re.IGNORECASE)
_ELLIPSIS_LINE = re.compile(r"^\s*(?://\s*|#\s*|/\*\s*)?(?:\.\.\.|…)(?:\s*\*/)?\s*$")


def strip_code_spans(text):
    return _CODE_SPAN.sub("``", text or "")


def process_vocabulary_in(text):
    """Review-process wording that must never reach a PR author. `{{#n}}` tokens
    and code spans are exempt."""
    plain = strip_code_spans(text)
    found = []
    for pat in PROCESS_PATTERNS:
        m = re.search(pat, plain, re.IGNORECASE)
        if m:
            found.append(m.group(0))
    return found


def claim_shape_errors(claim):
    plain = _ABBREV.sub("abbr", strip_code_spans(claim))
    errs = []
    if len(plain.split()) > 40:
        errs.append(f"claim is {len(plain.split())} words; the limit is 40")
    if len(re.findall(r"[.!?]\s+(?=[A-Z])", plain)) > 0:
        errs.append("claim is more than one sentence")
    return errs


def is_ellipsis_line(line):
    return bool(_ELLIPSIS_LINE.match(line))


def excerpt_errors(ledger, worktree):
    """Each item's anchor_excerpt must equal lines line..line_end of its file,
    compared with trailing whitespace ignored (reanchor's own rule)."""
    errs = []
    for it in ledger.get("items", []):
        iid, line = it.get("id"), it.get("line")
        end = it.get("line_end") or line
        try:
            want = read_lines(worktree, it.get("file") or "", line, end)
        except (LedgerError, TypeError) as exc:
            errs.append(f"item {iid!r}: cannot read anchor lines - {exc}")
            continue
        got = (it.get("anchor_excerpt") or "").splitlines()
        if [l.rstrip() for l in got] != [l.rstrip() for l in want]:
            errs.append(f"item {iid!r}: anchor_excerpt is not verbatim at "
                        f"{it.get('file')}:{line}-{end} - run `excerpt`, never retype it")
    return errs
```

- [ ] **Step 4: Extend `HEDGE_PATTERNS` and `validate` in `pr-review-anchor.py`**

Append to `HEDGE_PATTERNS`:

```python
    r"\bno\s+code\s+change\b",
    r"\bnot\s+required\s+(?:for|in)\s+this\s+(?:PR|change|pull\s+request)\b",
    r"\bshould\s+be\s+(?:downgraded|closed|dropped)\b",
    r"\bif\b.{0,120}?\bthen\s+(?:downgrade|close|drop)\b",
```

Change the signature to `def validate(ledger, worktree=None):`. Inside the per-item loop, replace
the `claim_verdict` check with an unanswered-aware one and add the new rules:

```python
            unanswered = it.get("unanswered") is True and it.get("verdict") == "INCONCLUSIVE"
            if not unanswered and it.get("claim_verdict") not in CLAIM_VERDICTS:
                # (existing error text unchanged)
            if not unanswered and not str(it.get("claim_reason") or "").strip():
                # (existing error text unchanged)
            for msg in lt.claim_shape_errors(it.get("claim")):
                errs.append(f"item {iid!r} {msg}")
            if it.get("claim_verdict") == "REWRITTEN" and (
                    not it.get("claim_original") or it.get("claim_original") == it.get("claim")):
                errs.append(f"item {iid!r} claim_verdict is REWRITTEN but claim_original is "
                            "missing or identical to claim - the rewrite was not applied")
            if it.get("fix_verdict") == "REWRITTEN" and (
                    not it.get("fix_original")
                    or it.get("fix_original") == fix_slot(it.get("detail"))):
                errs.append(f"item {iid!r} fix_verdict is REWRITTEN but fix_original is "
                            "missing or identical to the Fix: slot - the rewrite was not applied")
            for field in ("claim", "detail"):
                for phrase in lt.process_vocabulary_in(it.get(field)):
                    errs.append(f"item {iid!r} {field} contains review-process wording "
                                f"{phrase!r} - the author never saw this pipeline")
        if it.get("verdict") == "DOWNGRADED":
            if lt.SEV_RANK.get(it.get("severity"), -1) <= lt.SEV_RANK.get(it.get("severity_in"), 99):
                errs.append(f"item {iid!r} is DOWNGRADED but severity {it.get('severity')!r} is "
                            f"not lower than severity_in {it.get('severity_in')!r} - a narrowed "
                            "claim at the same severity is CONFIRMED with a rewritten claim")
            if it.get("fold_note") not in ("folded", "bookkeeping"):
                errs.append(f"item {iid!r} is DOWNGRADED without fold_note 'folded' or "
                            "'bookkeeping'")
        if it.get("verdict") == "REFUTED" and (
                "REWRITTEN" in (it.get("claim_verdict"), it.get("fix_verdict"))
                or it.get("claim_original") or it.get("fix_original")):
            errs.append(f"item {iid!r} is REFUTED but carries a rewrite - a refuted claim has "
                        "nothing to rewrite; if something true survives it is DOWNGRADED")
        if any(lt.is_ellipsis_line(l) for l in (it.get("anchor_excerpt") or "").splitlines()):
            errs.append(f"item {iid!r} anchor_excerpt contains an ellipsis line - run `excerpt`")
```

(The `unanswered`, shape, rewrite and vocabulary checks sit inside the existing
`if it.get("verdict") != "REFUTED":` block; the DOWNGRADED, REFUTED and ellipsis checks sit after
it at item level.) Before `return errs`, add:

```python
    if worktree:
        errs.extend(lt.excerpt_errors(ledger, worktree))
```

In `cmd_validate`, call `validate(led, getattr(args, "worktree", None))`; in `main`, give the
`validate` parser `v.add_argument("--worktree")`.

- [ ] **Step 5: Run the suite; expect all pass**

Run: `python3 -m pytest -q && bash pr-review-wt.test.sh | tail -1`
Expected: all pass. The `item()` default (`claim="c"`, `detail="d"`, CONFIRMED, high/high) must
still validate clean.

- [ ] **Step 6: Mutation check**

One at a time, disable: the DOWNGRADED severity check, the fold_note check, the REFUTED-rewrite
check, the claim_original check, the fix_original check, each new hedge pattern, each
`PROCESS_PATTERNS` entry, the sentence check, the word-count check, the ellipsis check. Each must
turn at least one new test red. Restore.

- [ ] **Step 7: Commit**

```bash
git add ledger_tools.py pr-review-anchor.py test_ledger_tools.py test_pr_review_anchor.py
git commit -m "feat(validate): reject process wording, unapplied rewrites, same-severity downgrades

Also: claim shape, deferral hedges, ellipsis excerpts, and --worktree verbatim check.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Refuter brief template, `brief`, verdict-file parser

**Files:**
- Create: `refuter-brief.tmpl.md`
- Modify: `ledger_tools.py`, `pr-review-anchor.py` (`main`)
- Test: `test_ledger_tools.py`

**Interfaces:**
- Produces: `TEMPLATE_PATH`, `template_hash(path=TEMPLATE_PATH) -> str` (12 hex),
  `refuter_units(ledger) -> dict[str, list[int]]`,
  `render_brief(ledger, key, context, ledger_dir, template_path=TEMPLATE_PATH) -> tuple[str, dict]`
  returning `(brief_text, meta)` with `meta = {"key", "owns", "hash"}`,
  `parse_verdict_file(text) -> tuple[dict, list[dict]]`, `VERDICT_FIELDS`.
- CLI: `brief <ledger> --key KEY --context CTX.json` writes `<ledger_dir>/briefs/<KEY>.md` and
  `.json`, prints the `.md` path.
- Context file keys: `link_deps` (str), `commands` (str), `obligations` (str, markdown table),
  `claude_md_paths` (list of str), `scratch` (absolute path).

- [ ] **Step 1: Write the template**

Create `refuter-brief.tmpl.md` with this structure. Where a step says "copy verbatim", copy the
named lines of the CURRENT `commands/pr-review.md` (before Task 13 edits it), strip one leading
`> ` from blockquoted lines, and apply the American-English word list from Global Constraints.

```markdown
brief-version: {{BRIEF_HASH}}

# Refuter brief - {{KEY}}

You are verifying findings from a code review before they reach the PR author. Everything you
need is in this brief. Do not rely on any other instructions about format.

## Where things are

- Worktree (read-only for you): `{{WT}}`
- Head: `{{HEAD_SHA}}`   Merge base: `{{MERGE_BASE}}`
- Repo guidance to read first: {{CLAUDE_MD_PATHS}}
- Dependencies: {{LINK_DEPS}}
- Commands that work in this repo: {{COMMANDS}}
- Your probe directory: `{{PROBE_DIR}}`
- Your reply file: `{{VERDICT_PATH}}`

## Worktree rules

- `{{WT}}` is shared with other refuters running right now. Never edit a tracked file there and
  never create files there.
- Put probe files under `{{PROBE_DIR}}`.
- If you need a mutation test or a probe that must live inside a package, make your own worktree:
  `~/.claude/pr-review/pr-review-wt.sh create "{{REFUTER_WT}}" {{HEAD_SHA}}` then
  `~/.claude/pr-review/pr-review-wt.sh link-deps "{{REFUTER_WT}}" {{MERGE_BASE}}`. Work there, and
  run `~/.claude/pr-review/pr-review-wt.sh remove "{{REFUTER_WT}}"` before you finish.
- Never cd into, build in, or run tests in any directory other than `{{WT}}`, `{{REFUTER_WT}}` or
  `{{PROBE_DIR}}`. In particular never touch the checkout that `node_modules` links point at.

## The findings you own

{{ITEMS}}

## How a finding is written

(copy verbatim: pr-review.md lines 216-281, "Write for a competent engineer..." through the end of
the worked example)

## Your first job: is it true?

(copy verbatim: pr-review.md lines 383-417, the refuter's-job blockquote)

## Your second job: does the claim describe what you verified?

(copy verbatim: pr-review.md lines 439-454, the claim-match blockquote)

## Your third job: the fix

(copy verbatim: pr-review.md lines 470-508, the fix blockquote)

The obligation table for this repo - a fix that trips a row must say so:

{{OBLIGATIONS}}

## Writing REASON

(copy verbatim: pr-review.md lines 531-540, "Write `REASON` for the PR author..." through
"...not whether it exists.")

## Your reply

Write your reply to `{{VERDICT_PATH}}` - create the directory if needed. The file must start with
exactly these three lines:

    BRIEF: {{BRIEF_HASH}}
    KEY: {{KEY}}
    OWNS: {{OWNS}}

followed by one block per finding you own, in the order given, blank line between blocks, nothing
else in the file:

    ITEM: <id>
    VERDICT: CONFIRMED|DOWNGRADED|REFUTED|INCONCLUSIVE
    SEVERITY: blocker|high|medium|low
    ON_CHANGED_LINE: true|false
    REASON: <3-6 sentences with file:line evidence>
    CLAIM_VERDICT: SOUND|REWRITTEN
    CLAIM_REASON: <what you matched the claim against, with file:line evidence>
    CLAIM_REWRITE: <replacement claim sentence - only when CLAIM_VERDICT is REWRITTEN>
    FIX_VERDICT: SOUND|REWRITTEN|UNVERIFIABLE
    FIX_REASON: <what you checked about the fix, with file:line evidence>
    FIX_REWRITE: <replacement text for after "**Fix:**" - only when FIX_VERDICT is REWRITTEN>

Rules the file is checked against:
- A value may run over several lines; it ends at the next line that starts with a field label.
- `DOWNGRADED` means the severity you give is LOWER than the finding's `severity_in`. If what you
  found is true but narrower at the same severity, answer `CONFIRMED` with `CLAIM_VERDICT:
  REWRITTEN`.
- On `REFUTED`, omit every `CLAIM_` and `FIX_` line.
- Never put a rewrite inside a `_REASON` line.
- Never refer to other findings by number, and never mention this review process, refuters,
  batches or verdicts inside `CLAIM_REWRITE` or `FIX_REWRITE` - that text goes to the author.

When the file is written, your final message must be only its path: `{{VERDICT_PATH}}`. Anything
else you write is discarded; only the file is read.
```

- [ ] **Step 2: Write failing tests**

```python
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

    def test_renders_every_placeholder(self):
        text, meta = lt.render_brief(brief_ledger(), "C1", CTX, "/s/pr-review/pr13")
        self.assertNotRegex(text, r"\{\{[A-Z_]+\}\}")
        self.assertIn("/s/pr-review/pr13/verdicts/C1.txt", text)
        self.assertIn("/s/wt-pr13-rc1", text)
        self.assertIn("check the second site", text)
        self.assertIn('"shared_fix": "sf"', text)
        self.assertEqual(meta, {"key": "C1", "owns": [1, 2], "hash": lt.template_hash()})
        self.assertIn(f"BRIEF: {lt.template_hash()}", text)

    def test_an_unknown_key_cannot_batch_unrelated_items(self):
        with self.assertRaisesRegex(lt.LedgerError, "unknown refuter key"):
            lt.render_brief(brief_ledger(), "i1", CTX, "/s")   # 1 is clustered
        with self.assertRaisesRegex(lt.LedgerError, "unknown refuter key"):
            lt.render_brief(brief_ledger(), "i3,4", CTX, "/s")

    def test_an_obligation_row_without_a_repo_file_is_rejected(self):
        ctx = dict(CTX, obligations="| a | b | c |\n| --- | --- | --- |\n"
                                    "| adds prose | American spelling | user convention |")
        with self.assertRaisesRegex(lt.LedgerError, "does not cite a repo file"):
            lt.render_brief(brief_ledger(), "C1", ctx, "/s")

    def test_a_missing_context_key_is_named(self):
        ctx = dict(CTX); del ctx["commands"]
        with self.assertRaisesRegex(lt.LedgerError, "commands"):
            lt.render_brief(brief_ledger(), "C1", ctx, "/s")

    def test_verdict_fields_are_not_leaked_into_items(self):
        l = brief_ledger(); l["items"][0]["verdict_reason"] = "SECRET"
        text, _ = lt.render_brief(l, "C1", CTX, "/s")
        self.assertNotIn("SECRET", text)


GOOD = """BRIEF: abc
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
        self.assertEqual(header, {"BRIEF": "abc", "KEY": "C1", "OWNS": "1, 2"})
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

    def test_a_header_after_a_block_is_rejected(self):
        with self.assertRaisesRegex(lt.LedgerError, "header line after"):
            lt.parse_verdict_file(GOOD + "KEY: C2\n")
```

- [ ] **Step 3: Run to verify failure**

Run: `python3 -m pytest -q test_ledger_tools.py -k "Brief or ParseVerdict"`
Expected: FAIL, missing attributes.

- [ ] **Step 4: Implement**

```python
TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "refuter-brief.tmpl.md")
VERDICT_FIELDS = ("ITEM", "VERDICT", "SEVERITY", "ON_CHANGED_LINE", "REASON", "CLAIM_VERDICT",
                  "CLAIM_REASON", "CLAIM_REWRITE", "FIX_VERDICT", "FIX_REASON", "FIX_REWRITE")
HEADER_FIELDS = ("BRIEF", "KEY", "OWNS")
_LABEL = re.compile(r"^(%s):[ \t]?(.*)$" % "|".join(HEADER_FIELDS + VERDICT_FIELDS))
# Fields a refuter sees. Verdict fields are deliberately absent: a refuter must
# not be primed by an earlier answer.
BRIEF_ITEM_FIELDS = ("id", "severity_in", "cluster", "file", "line", "line_end", "claim",
                     "detail", "evidence", "refuter_note")
CONTEXT_KEYS = ("link_deps", "commands", "obligations", "claude_md_paths", "scratch")
_REPO_FILE = re.compile(r"[\w./-]+\.(?:md|mdc|txt|json|ya?ml|toml)\b|CLAUDE\.md|AGENTS\.md")


def template_hash(path=TEMPLATE_PATH):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()[:12]


def refuter_units(ledger):
    units = {c["id"]: sorted(c.get("item_ids", [])) for c in ledger.get("clusters", [])}
    for it in ledger.get("items", []):
        if not it.get("cluster"):
            units[f"i{it['id']}"] = [it["id"]]
    return units


def _check_obligations(table):
    for row in table.splitlines():
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        if not row.strip().startswith("|") or set("".join(cells)) <= set("-: "):
            continue
        if cells and cells[0].lower().startswith("if the fix"):
            continue  # header row
        if not _REPO_FILE.search(cells[-1] if cells else ""):
            raise LedgerError(f"obligation row does not cite a repo file: {row.strip()} - "
                              "a personal preference is not a repo obligation")


def render_brief(ledger, key, context, ledger_dir, template_path=TEMPLATE_PATH):
    units = refuter_units(ledger)
    if key not in units:
        raise LedgerError(f"unknown refuter key {key!r}: expected one of {sorted(units)} "
                          "(one cluster, or one unclustered item as i<id>)")
    missing = [k for k in CONTEXT_KEYS if k not in context]
    if missing:
        raise LedgerError(f"context file is missing {', '.join(missing)}")
    _check_obligations(context["obligations"])
    owns = units[key]
    by_id = {it["id"]: it for it in ledger.get("items", [])}
    payload = [{k: by_id[i][k] for k in BRIEF_ITEM_FIELDS if k in by_id[i]} for i in owns]
    cluster = next((c for c in ledger.get("clusters", []) if c["id"] == key), None)
    if cluster:
        payload = {"cluster": {k: cluster.get(k) for k in ("id", "title", "shared_fix")},
                   "items": payload}
    target = ledger.get("target", {})
    n = target.get("number", "local")
    scratch = context["scratch"].rstrip("/")
    h = template_hash(template_path)
    values = {
        "BRIEF_HASH": h, "KEY": key, "OWNS": ", ".join(str(i) for i in owns),
        "WT": ledger.get("worktree", ""), "HEAD_SHA": ledger.get("head_sha", ""),
        "MERGE_BASE": ledger.get("merge_base", ""),
        "LINK_DEPS": context["link_deps"], "COMMANDS": context["commands"],
        "OBLIGATIONS": context["obligations"],
        "CLAUDE_MD_PATHS": ", ".join(f"`{p}`" for p in context["claude_md_paths"]),
        "ITEMS": "```json\n" + json.dumps(payload, indent=2) + "\n```",
        "VERDICT_PATH": os.path.join(ledger_dir, "verdicts", f"{key}.txt"),
        "PROBE_DIR": f"{scratch}/probes/{key}",
        "REFUTER_WT": f"{scratch}/wt-pr{n}-r{key.lower()}",
    }
    with open(template_path, encoding="utf-8") as fh:
        text = fh.read()
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", str(v))
    left = sorted(set(re.findall(r"\{\{[A-Z_]+\}\}", text)))
    if left:
        raise LedgerError(f"template placeholders left unfilled: {', '.join(left)}")
    return text, {"key": key, "owns": owns, "hash": h}


def parse_verdict_file(text):
    header, blocks, cur, field = {}, [], None, None
    for raw in text.splitlines():
        m = _LABEL.match(raw)
        if m:
            name, val = m.group(1), m.group(2)
            if name in HEADER_FIELDS:
                if cur is not None:
                    raise LedgerError(f"{name}: header line after the first ITEM block")
                header[name] = val.strip(); field = None
                continue
            if name == "ITEM":
                cur = {"ITEM": val.strip()}; blocks.append(cur); field = None
                continue
            if cur is None:
                raise LedgerError(f"{name}: field before any ITEM line")
            if name in cur:
                raise LedgerError(f"item {cur['ITEM']}: {name} given twice")
            cur[name] = val; field = name
            continue
        if field is not None:
            cur[field] += "\n" + raw
        elif raw.strip():
            where = "header" if cur is None else f"item {cur['ITEM']}"
            raise LedgerError(f"{where}: unrecognized line {raw.strip()[:60]!r}")
    for b in blocks:
        for k in b:
            b[k] = b[k].strip()
    return header, blocks
```

Wire the CLI in `pr-review-anchor.py`:

```python
def _lt_errors(exc):
    for line in str(exc).splitlines():
        print(f"error: {line}", file=sys.stderr)
    return 1


def cmd_brief(args):
    try:
        led = lt.load_json(args.ledger)
        ctx = lt.load_json(args.context, what="context")
        ldir = os.path.dirname(os.path.abspath(args.ledger))
        text, meta = lt.render_brief(led, args.key, ctx, ldir)
    except lt.LedgerError as exc:
        return _lt_errors(exc)
    bdir = os.path.join(ldir, "briefs"); os.makedirs(bdir, exist_ok=True)
    md = os.path.join(bdir, f"{args.key}.md")
    with open(md, "w", encoding="utf-8") as fh:
        fh.write(text)
    with open(os.path.join(bdir, f"{args.key}.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh)
    print(md)
    return 0
```

In `main`: `b = sub.add_parser("brief"); b.add_argument("ledger"); b.add_argument("--key", required=True); b.add_argument("--context", required=True); b.set_defaults(fn=cmd_brief)`.

- [ ] **Step 5: Run; expect pass. Mutation-check** the unknown-key guard, the obligation check,
the leftover-placeholder check, the verdict-field exclusion, and the "prose before header" error.

- [ ] **Step 6: Commit**

```bash
git add refuter-brief.tmpl.md ledger_tools.py pr-review-anchor.py test_ledger_tools.py
git commit -m "feat: refuter brief template, brief subcommand, verdict-file parser

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `ingest`

**Files:** Modify `ledger_tools.py`, `pr-review-anchor.py`; Test `test_ledger_tools.py`

**Interfaces:**
- Consumes: `parse_verdict_file`, `template_hash`, `fix_slot`, `SEV_RANK`, `write_ledger`.
- Produces: `replace_fix_slot(detail, text) -> str`,
  `ingest(ledger, briefs_dir, verdicts_dir, expected_hash, inconclusive=(), reason="") -> list[str]`
  (mutates `ledger`, returns printable notes, raises `LedgerError` listing every problem at once).
- CLI: `ingest <ledger> [--inconclusive IDS] [--reason TEXT]`; briefs and verdicts are
  `<ledger_dir>/briefs` and `<ledger_dir>/verdicts`.

- [ ] **Step 1: Write failing tests**

```python
def ingest_env(verdict_text, owns=(1, 2), key="C1"):
    d = tmpdir()
    write(os.path.join(d, "briefs", f"{key}.json"),
          json.dumps({"key": key, "owns": list(owns), "hash": "abc"}))
    if verdict_text is not None:
        write(os.path.join(d, "verdicts", f"{key}.txt"), verdict_text)
    l = led(item(1, cluster="C1", detail="**What happens:** x\n\n**Fix:** old fix",
                 refuter_note="hint"),
            item(2, cluster="C1", sev="low"),
            clusters=[{"id": "C1", "title": "t", "shared_fix": "s", "item_ids": [1, 2]}])
    for it in l["items"]:
        for k in ("verdict", "verdict_reason", "claim_verdict", "claim_reason",
                  "fix_verdict", "fix_reason"):
            it.pop(k, None)
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
```

- [ ] **Step 2: Run to verify failure** - `python3 -m pytest -q test_ledger_tools.py -k Ingest`.

- [ ] **Step 3: Implement**

```python
VERDICTS = ("CONFIRMED", "DOWNGRADED", "REFUTED", "INCONCLUSIVE")


def replace_fix_slot(detail, text):
    m = re.search(r"\*\*Fix:\*\*", detail or "")
    if not m:
        raise LedgerError("detail has no **Fix:** slot to rewrite")
    text = re.sub(r"^\s*\*\*Fix:\*\*\s*", "", text)
    return detail[:m.end()] + " " + text.strip()


def _block_errors(b, it):
    iid, v = b["ITEM"], b.get("VERDICT")
    e = []
    if v not in VERDICTS:
        e.append(f"item {iid}: VERDICT {v!r} not one of {VERDICTS}")
    if b.get("SEVERITY") not in SEVERITIES:
        e.append(f"item {iid}: SEVERITY {b.get('SEVERITY')!r} not one of {SEVERITIES}")
    if b.get("ON_CHANGED_LINE", "").lower() not in ("true", "false"):
        e.append(f"item {iid}: ON_CHANGED_LINE must be true or false")
    if not b.get("REASON"):
        e.append(f"item {iid}: REASON is empty")
    if v == "REFUTED":
        extra = [k for k in b if k.startswith(("CLAIM_", "FIX_"))]
        if extra:
            e.append(f"item {iid}: REFUTED must omit {', '.join(extra)}")
        return e
    for slot, allowed in (("CLAIM", ("SOUND", "REWRITTEN")),
                          ("FIX", ("SOUND", "REWRITTEN", "UNVERIFIABLE"))):
        sv = b.get(f"{slot}_VERDICT")
        if sv not in allowed:
            e.append(f"item {iid}: {slot}_VERDICT {sv!r} not one of {allowed}")
        if not b.get(f"{slot}_REASON"):
            e.append(f"item {iid}: {slot}_REASON is empty")
        if sv == "REWRITTEN" and not b.get(f"{slot}_REWRITE"):
            e.append(f"item {iid}: {slot}_VERDICT REWRITTEN without {slot}_REWRITE")
        if sv != "REWRITTEN" and b.get(f"{slot}_REWRITE"):
            e.append(f"item {iid}: {slot}_REWRITE given with {slot}_VERDICT {sv}")
    if b.get("FIX_VERDICT") == "REWRITTEN" and not re.search(r"\*\*Fix:\*\*", it.get("detail") or ""):
        e.append(f"item {iid}: detail has no **Fix:** slot to rewrite")
    if v == "DOWNGRADED" and b.get("SEVERITY") in SEV_RANK and \
            SEV_RANK[b["SEVERITY"]] <= SEV_RANK.get(it.get("severity_in"), 99):
        e.append(f"item {iid}: DOWNGRADED but SEVERITY {b['SEVERITY']} is not lower than "
                 f"severity_in {it.get('severity_in')}")
    return e


def ingest(ledger, briefs_dir, verdicts_dir, expected_hash, inconclusive=(), reason=""):
    by_id = {it["id"]: it for it in ledger.get("items", [])}
    inconclusive = {int(i) for i in inconclusive}
    if inconclusive and not reason.strip():
        raise LedgerError("--inconclusive needs --reason")
    metas = []
    if os.path.isdir(briefs_dir):
        for f in sorted(os.listdir(briefs_dir)):
            if f.endswith(".json"):
                metas.append(load_json(os.path.join(briefs_dir, f), what="brief meta"))
    if not metas:
        raise LedgerError(f"no briefs in {briefs_dir} - render them with `brief` before dispatch")
    errors, answers = [], {}
    dispatched = {i for m in metas for i in m["owns"]}
    for iid in sorted(set(by_id) - dispatched):
        errors.append(f"item {iid} was never dispatched")
    for m in metas:
        key, owns = m["key"], set(m["owns"])
        path = os.path.join(verdicts_dir, f"{key}.txt")
        if not os.path.isfile(path):
            if not owns <= inconclusive:
                errors.append(f"{key}: no verdict file at {path} (owns {sorted(owns)})")
            continue
        with open(path, encoding="utf-8") as fh:
            try:
                header, blocks = parse_verdict_file(fh.read())
            except LedgerError as exc:
                errors.append(f"{key}: {exc}"); continue
        if header.get("BRIEF") != expected_hash:
            errors.append(f"{key}: BRIEF {header.get('BRIEF')!r} does not match the current "
                          f"template ({expected_hash}) - render the brief with `brief`")
        if header.get("KEY") != key:
            errors.append(f"{key}: KEY header is {header.get('KEY')!r}")
        seen = set()
        for b in blocks:
            try:
                iid = int(b["ITEM"])
            except ValueError:
                errors.append(f"{key}: ITEM {b['ITEM']!r} is not an integer id"); continue
            if iid in seen:
                errors.append(f"{key}: item {iid} answered twice"); continue
            seen.add(iid)
            if iid not in owns:
                errors.append(f"{key}: does not own item {iid}"); continue
            errs = _block_errors(b, by_id[iid])
            errors.extend(f"{key}: {x}" for x in errs)
            if not errs:
                answers[iid] = (key, b)
        for iid in sorted(owns - seen - inconclusive):
            errors.append(f"{key}: no answer for item {iid}")
    if errors:
        raise LedgerError("\n".join(errors))

    for iid, (key, b) in answers.items():
        it = by_id[iid]
        it.update(verdict=b["VERDICT"], severity=b["SEVERITY"], verdict_reason=b["REASON"],
                  on_changed_line=b["ON_CHANGED_LINE"].lower() == "true", refuter_key=key)
        if b["VERDICT"] != "REFUTED":
            it.update(claim_verdict=b["CLAIM_VERDICT"], claim_reason=b["CLAIM_REASON"],
                      fix_verdict=b["FIX_VERDICT"], fix_reason=b["FIX_REASON"])
            if b["CLAIM_VERDICT"] == "REWRITTEN":
                it["claim_original"], it["claim"] = it["claim"], b["CLAIM_REWRITE"]
            if b["FIX_VERDICT"] == "REWRITTEN":
                it["fix_original"] = fix_slot(it["detail"])
                it["detail"] = replace_fix_slot(it["detail"], b["FIX_REWRITE"])
    for iid in inconclusive - set(answers):
        it = by_id[iid]
        it.update(verdict="INCONCLUSIVE", verdict_reason=reason, unanswered=True,
                  fix_verdict="UNVERIFIABLE", fix_reason=reason, claim_reason=reason,
                  refuter_key="none")
    for it in by_id.values():
        it.pop("refuter_note", None)
    return [f"ingested {len(answers)} answer(s) from {len(metas)} refuter(s)"
            + (f"; {len(inconclusive - set(answers))} closed INCONCLUSIVE" if inconclusive else "")]
```

CLI:

```python
def cmd_ingest(args):
    try:
        led = lt.load_json(args.ledger)
        ldir = os.path.dirname(os.path.abspath(args.ledger))
        ids = [int(t) for t in re.split(r"[,\s]+", args.inconclusive or "") if t]
        notes = lt.ingest(led, os.path.join(ldir, "briefs"), os.path.join(ldir, "verdicts"),
                          lt.template_hash(), inconclusive=ids, reason=args.reason or "")
        lt.write_ledger(args.ledger, led, "ingest")
    except lt.LedgerError as exc:
        return _lt_errors(exc)
    print("\n".join(notes))
    return 0
```

`main`: `g = sub.add_parser("ingest"); g.add_argument("ledger"); g.add_argument("--inconclusive"); g.add_argument("--reason"); g.set_defaults(fn=cmd_ingest)`.

In `pr-review-anchor.py`, delete its own `VERDICTS` tuple and add `VERDICTS` to the
`from ledger_tools import ...` line, so the two files cannot drift (keep the explanatory comment
above the import).

- [ ] **Step 4: Run; expect pass. Mutation-check** every `_block_errors` rule, the
"never dispatched" rule, the BRIEF hash check, and the all-or-nothing behavior (move the
`if errors: raise` below the apply loop - `test_a_failed_ingest_changes_nothing` must fail).

- [ ] **Step 5: Commit** - `feat: ingest verdict files into the ledger, applying rewrites mechanically`.

---

### Task 5: `excerpt` and `changed-lines`

**Files:** Modify `ledger_tools.py`, `pr-review-anchor.py`; Test `test_ledger_tools.py`

**Interfaces:**
- Produces: `fill_excerpts(ledger, worktree) -> None`,
  `mark_changed_lines(ledger, worktree, changed) -> list[str]` (notes).
- CLI: `excerpt <ledger> --worktree WT`; `changed-lines <ledger> --diff FILE --worktree WT`.

- [ ] **Step 1: Write failing tests**

```python
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

    def test_nothing_changed_in_range_is_off_diff_and_flip_is_reported(self):
        l = led(item(1, line=2, line_end=3, anchor_excerpt="l2\nl3", on_changed_line=True))
        notes = lt.mark_changed_lines(l, self.wt, {"a.ts": {5}})
        self.assertIs(l["items"][0]["on_changed_line"], False)
        self.assertTrue(any("True -> False" in n for n in notes), notes)

    def test_file_absent_from_the_diff_is_off_diff(self):
        l = led(item(1, line=2, anchor_excerpt="l2"))
        lt.mark_changed_lines(l, self.wt, {})
        self.assertIs(l["items"][0]["on_changed_line"], False)
```

- [ ] **Step 2: Verify failure.**

- [ ] **Step 3: Implement**

```python
def _excerpt_for(worktree, it, start=None):
    start = start or it["line"]
    end = max(it.get("line_end") or it["line"], start)
    return "\n".join(read_lines(worktree, it["file"], start, end))


def fill_excerpts(ledger, worktree):
    new, errors = {}, []
    for it in ledger.get("items", []):
        try:
            new[it["id"]] = _excerpt_for(worktree, it)
        except (LedgerError, KeyError, TypeError) as exc:
            errors.append(f"item {it.get('id')}: {exc}")
    if errors:
        raise LedgerError("\n".join(errors))
    for it in ledger["items"]:
        it["anchor_excerpt"] = new[it["id"]]


def mark_changed_lines(ledger, worktree, changed):
    """Same rule reanchor applies: a finding is on the diff when its anchor line
    is a `+` line; a range whose first line is unchanged moves onto the first
    changed line inside [line, line_end]."""
    notes = []
    for it in ledger.get("items", []):
        lines = changed.get(it["file"], set())
        start, end = it["line"], it.get("line_end") or it["line"]
        was = it.get("on_changed_line")
        if start in lines:
            now = True
        else:
            hit = min((n for n in lines if start < n <= end), default=None)
            now = hit is not None
            if hit:
                it["line"] = hit
                it["anchor_excerpt"] = _excerpt_for(worktree, it)
                notes.append(f"moved #{it['id']} {start} -> {hit} "
                             "(first changed line in its range)")
        if was is not None and bool(was) != now:
            notes.append(f"flipped #{it['id']} on_changed_line {bool(was)} -> {now}")
        it["on_changed_line"] = now
    return notes
```

CLI (both write via `write_ledger(..., "excerpt")` / `"changed-lines"`, print notes, `_lt_errors`
on failure; `changed-lines` reads the diff with `open(..., errors="replace")` and raises
`LedgerError(f"diff file not found: {path}")` when missing).

- [ ] **Step 4: Run; mutation-check** the range move and the all-or-nothing excerpt write.

- [ ] **Step 5: Commit** - `feat: excerpt and changed-lines subcommands`.

---

### Task 6: `renumber` and `edit`

**Files:** Modify `ledger_tools.py`, `pr-review-anchor.py`; Test `test_ledger_tools.py`

**Interfaces:**
- Produces: `TOKEN = re.compile(r"\{\{#(\d+)\}\}")`, `renumber(ledger) -> dict[int, int]`,
  `EDITABLE_FIELDS = ("claim", "detail", "fold_note")`, `edit_field(ledger, iid, field, text)`.
- CLI: `renumber <ledger>` prints `old -> new` lines; `edit <ledger> <id> --field F --text-file P`.

- [ ] **Step 1: Write failing tests**

```python
class TestRenumber(unittest.TestCase):
    def test_sorts_maps_tokens_clusters_and_keeps_old_id(self):
        l = led(item(1, sev="low", f="b.ts", detail="see {{#2}}"),
                item(2, sev="high", f="a.ts", cluster="C1"),
                clusters=[{"id": "C1", "title": "t", "shared_fix": "s", "item_ids": [2]}])
        mapping = lt.renumber(l)
        self.assertEqual(mapping, {2: 1, 1: 2})
        by_new = {it["id"]: it for it in l["items"]}
        self.assertEqual(by_new[2]["detail"], "see {{#1}}")
        self.assertEqual(by_new[2]["old_id"], 1)
        self.assertEqual(l["clusters"][0]["item_ids"], [1])
        self.assertEqual(anchor.validate(l), [])

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
```

- [ ] **Step 2: Verify failure.**

- [ ] **Step 3: Implement**

```python
TOKEN = re.compile(r"\{\{#(\d+)\}\}")
EDITABLE_FIELDS = ("claim", "detail", "fold_note")


def renumber(ledger):
    items = ledger.get("items", [])
    ids = {it["id"] for it in items}
    bad = [f"item {it['id']} references {{{{#{m}}}}}, which is not in this ledger"
           for it in items for f in ("claim", "detail")
           for m in TOKEN.findall(it.get(f) or "") if int(m) not in ids]
    if bad:
        raise LedgerError("\n".join(bad))
    order = sorted(items, key=lambda it: (SEV_RANK.get(it.get("severity"), 99),
                                          it.get("file", ""), it["id"]))
    mapping = {it["id"]: n for n, it in enumerate(order, start=1)}
    sub = lambda text: TOKEN.sub(lambda m: "{{#%d}}" % mapping[int(m.group(1))], text or "")
    for it in order:
        it.setdefault("old_id", it["id"])
        it["id"] = mapping[it["id"]]
        for f in ("claim", "detail"):
            if it.get(f):
                it[f] = sub(it[f])
    ledger["items"] = order
    for c in ledger.get("clusters", []):
        c["item_ids"] = sorted(mapping[i] for i in c.get("item_ids", []) if i in mapping)
    return mapping


def edit_field(ledger, iid, field, text):
    if field not in EDITABLE_FIELDS:
        raise LedgerError(f"field {field!r} is not editable; only {EDITABLE_FIELDS}")
    it = next((x for x in ledger.get("items", []) if x.get("id") == iid), None)
    if it is None:
        raise LedgerError(f"no item {iid} in this ledger")
    it[field] = text.rstrip("\n")
```

CLI: `renumber` prints `f"{old} -> {new}"` per changed id, sorted by old; `edit` reads the text
file with `encoding="utf-8"` and writes with step names `renumber` / `edit`.

- [ ] **Step 4: Run; mutation-check** the pre-check (move it after the mutation - the unknown-token
test must fail) and `setdefault` (replace with assignment - the twice test must fail).

- [ ] **Step 5: Commit** - `feat: renumber with symbolic cross-references, and a safe text edit`.

---

### Task 7: `reanchor` - off-diff status, ranges, `--head`, `--dry-run`

**Files:** Modify `pr-review-anchor.py:288-385, main`; Test `test_pr_review_anchor.py`

**Interfaces:**
- `reanchor_item(item, worktree, changed)` returns `(status, line, note)` with status in
  `ok | moved | off-diff | dead`.
- CLI adds `--head SHA` (writes ledger-root `anchored_sha`) and `--dry-run` (no write).

- [ ] **Step 1: Rewrite the three audited tests and add new ones**

Replace `test_anchor_not_on_a_changed_line_is_dead`:

```python
    def test_anchor_not_on_a_changed_line_is_off_diff_not_dead(self):
        wt = self._wt("one\ntwo\nthree\n")
        it = item(1, line=2, anchor_excerpt="two")
        status, line, why = anchor.reanchor_item(it, wt, {"a.ts": {99}})
        self.assertEqual((status, line), ("off-diff", 2))
        self.assertIn("changed line", why)

    def test_a_range_moves_onto_its_first_changed_line(self):
        wt = self._wt("one\ntwo\nthree\nfour\n")
        it = item(1, line=2, line_end=4, anchor_excerpt="two\nthree\nfour")
        self.assertEqual(anchor.reanchor_item(it, wt, {"a.ts": {3}})[:2], ("moved", 3))
```

Replace `test_a_dead_anchor_is_off_diff_by_definition`:

```python
    def test_a_dead_excerpt_leaves_on_changed_line_alone(self):
        """A vanished excerpt says nothing about the diff: flipping the field here
        is what made a bad excerpt look like a refuter's mistake."""
        out = self._run([item(1, line=2, anchor_excerpt="nowhere",
                              on_changed_line=True)], {"a.ts": {2}})
        self.assertEqual(out[1]["anchor_status"], "dead")
        self.assertIs(out[1]["on_changed_line"], True)
```

In `test_end_to_end_rewrites_ledger_with_status_and_line`, change the asserted summary to
`"ok=0 moved=1 off-diff=0 dead=0"`. Add to `TestReanchorCli`:

```python
    def test_head_is_recorded_and_dry_run_writes_nothing(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "a.ts"), "w") as fh:
            fh.write("one\ntwo\nthree\nfour\n")
        lp = self._write_json({"schema": 2, "items": [item(1, line=2, anchor_excerpt="two")],
                               "clusters": []})
        dp = self._write_text(DIFF)
        before = open(lp).read()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            anchor.cmd_reanchor(SimpleNamespace(ledger=lp, worktree=d, diff=dp,
                                                head="abc123", dry_run=True))
        self.assertEqual(open(lp).read(), before)
        self.assertIn("dry run", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            anchor.cmd_reanchor(SimpleNamespace(ledger=lp, worktree=d, diff=dp,
                                                head="abc123", dry_run=False))
        self.assertEqual(json.load(open(lp))["anchored_sha"], "abc123")
```

Existing tests call `cmd_reanchor` with `SimpleNamespace(ledger, worktree, diff)` only, so read
the new attributes with `getattr(args, "head", None)` / `getattr(args, "dry_run", False)`.

- [ ] **Step 2: Verify failure.**

- [ ] **Step 3: Implement.** In `reanchor_item`, replace everything from
`if new_line not in changed...` to the end with:

```python
    span = max((item.get("line_end") or item.get("line") or new_line)
               - (item.get("line") or new_line), 0)
    file_changed = changed.get(item["file"], set())
    target = new_line if new_line in file_changed else min(
        (n for n in file_changed if new_line < n <= new_line + span), default=None)
    if target is None:
        reason = "not on a changed line; cannot be posted inline"
        return "off-diff", new_line, f"{reason} ({note})" if note else reason
    if target == item.get("line"):
        return "ok", target, note
    moved_reason = f"moved {item.get('line')} -> {target}"
    return "moved", target, f"{moved_reason} ({note})" if note else moved_reason
```

In `cmd_reanchor`: counts gain `"off-diff": 0`; after computing status:

```python
        if status in ("ok", "moved") and new_line != it.get("line"):
            span_end = (it.get("line_end") or it.get("line")) - it.get("line") + new_line
            it["line"] = new_line
            it["line_end"] = max(span_end, new_line)
            it["anchor_excerpt"] = "\n".join(lt.read_lines(args.worktree, it["file"],
                                                           new_line, it["line_end"]))
        elif status == "off-diff" and new_line:
            it["line"] = new_line
        # on_changed_line is a property of the diff. A dead excerpt tells us
        # nothing about the diff, so it leaves the recorded value alone.
        if status != "dead":
            was = it.get("on_changed_line")
            now = status in ("ok", "moved")
            it["on_changed_line"] = now
            if was is not None and bool(was) != now:
                corrected.append((it.get("id"), bool(was), now))
```

(Wrap the `read_lines` call: on `lt.LedgerError`, set status `dead` with the error as the note.)
Replace the write with:

```python
    if getattr(args, "head", None):
        led["anchored_sha"] = args.head
    if getattr(args, "dry_run", False):
        print("dry run: ledger not written")
    else:
        try:
            lt.write_ledger(args.ledger, led, "reanchor")
        except OSError as exc:
            print(f"error: could not write ledger file {args.ledger}: {exc}", file=sys.stderr)
            return 1
```

Summary line: `print(f"\nok={counts['ok']} moved={counts['moved']} off-diff={counts['off-diff']} dead={counts['dead']}")`.
The notice becomes `notice: on_changed_line changed against the diff for N item(s) - ...` and its
code comment drops the claim that a refuter asserted something.

`main`: `r.add_argument("--head"); r.add_argument("--dry-run", action="store_true")`.

- [ ] **Step 4: Run whole suite; mutation-check** the dead-leaves-field-alone rule and the range
search.

- [ ] **Step 5: Commit** - message names the three rewritten tests.

---

### Task 8: `payload` - tokens, `{count}`, off-diff routing; `--select` ranges

**Files:** Modify `pr-review-anchor.py` (`resolve_selection`, `build_payload`); Test
`test_pr_review_anchor.py`

**Interfaces:** `lt.render_tokens(text) -> tuple[str, set[int]]` (rendered text, referenced ids).

- [ ] **Step 1: Write failing tests** (append a class to `test_pr_review_anchor.py`):

```python
class TestPayloadV2(unittest.TestCase):
    def led(self, *items):
        return {"schema": 2, "items": list(items), "clusters": []}

    def test_tokens_render_as_plain_references(self):
        p = anchor.build_payload(self.led(item(1, detail="as in {{#2}}"),
                                          item(2, sev="low", line=20)), ["all"], "intro")
        bodies = " ".join(c["body"] for c in p["comments"])
        self.assertIn("as in #2", bodies)
        self.assertNotIn("{{#", bodies)

    def test_a_token_pointing_outside_the_review_is_warned(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            anchor.build_payload(self.led(item(1, detail="as in {{#2}}"),
                                          item(2, sev="low", line=20, verdict="REFUTED")),
                                 ["all"], "intro")
        self.assertIn("#1 references #2, which is not in this review", err.getvalue())

    def test_count_placeholder_counts_inline_and_body_findings(self):
        p = anchor.build_payload(self.led(item(1), item(2, sev="low", on_changed_line=False)),
                                 ["all"], "{count} findings.")
        self.assertTrue(p["body"].startswith("2 findings."))

    def test_off_diff_status_is_demoted_with_its_own_label(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            p = anchor.build_payload(self.led(item(1, anchor_status="off-diff"),
                                              item(2, sev="low", anchor_status="dead")),
                                     ["all"], "x")
        self.assertEqual(p["comments"], [])
        self.assertIn("#1 (not on a changed line)", err.getvalue())
        self.assertIn("#2 (dead anchor)", err.getvalue())

    def test_select_accepts_ranges(self):
        l = self.led(*[item(i, sev="low", line=i) for i in range(1, 6)])
        self.assertEqual(anchor.resolve_selection(l, ["1-2, 4-5"]), {1, 2, 4, 5})
        with self.assertRaises(anchor.SelectionError):
            anchor.resolve_selection(l, ["3-1"])
        with self.assertRaises(anchor.SelectionError):
            anchor.resolve_selection(l, ["4-9"])
```

- [ ] **Step 2: Verify failure.**

- [ ] **Step 3: Implement**

In `ledger_tools.py`:

```python
def render_tokens(text):
    refs = {int(m) for m in TOKEN.findall(text or "")}
    return TOKEN.sub(lambda m: "#" + m.group(1), text or ""), refs
```

In `resolve_selection`, before the `elif tok.isdigit():` branch:

```python
            elif re.fullmatch(r"\d+-\d+", tok):
                a, b = (int(x) for x in tok.split("-"))
                if a > b:
                    unrecognized.append(tok)
                for requested in range(a, b + 1):
                    if requested in known_ids:
                        ids.add(requested)
                    else:
                        unknown.append(str(requested))
```

In `build_payload`: route `anchor_status == "off-diff"` into the existing "not on a changed line"
demotion branch (`elif it.get("anchor_status") == "off-diff" or it.get("on_changed_line") is False:`).
Before building comments, compute `posted = {it["id"] for it in items} | {it["id"] for it, _, _ in demoted} | {it["id"] for it, _ in secondary}`.
Add a helper inside `build_payload`:

```python
    outside = []
    def tok(it, text):
        rendered, refs = lt.render_tokens(text)
        outside.extend((it["id"], r) for r in sorted(refs - posted))
        return rendered
```

Use `tok(it, it["claim"])` and `tok(it, detail)` everywhere `claim`/`detail` are emitted (inline
comments, demoted section, body-only sections). After building, for each pair in
`sorted(set(outside))` print
`warning: #{a} references #{b}, which is not in this review` to stderr. Replace
`body = body_intro.strip()` with
`body = body_intro.replace("{count}", str(len(posted))).strip()`.

- [ ] **Step 4: Run whole suite; mutation-check** each new behavior.

- [ ] **Step 5: Commit** - `feat(payload): render cross-reference tokens, fill {count}, accept ranges`.

---

### Task 9: `preview --page`

**Files:** Modify `pr-review-anchor.py` (`render_preview`, `cmd_preview`, `main`); Test
`test_pr_review_anchor.py`

**Interfaces:** `preview_chunks(payload) -> list[str]`, `preview_pages(payload, limit=15000) -> list[str]`;
`render_preview(payload)` unchanged in output; CLI `preview <payload> [--page N]`, always writes
`preview.txt` beside the payload.

- [ ] **Step 1: Write failing tests**

```python
class TestPreviewPaging(unittest.TestCase):
    def payload(self, sizes):
        return {"event": "COMMENT", "body": "intro",
                "comments": [{"path": "a.ts", "line": n + 1, "side": "RIGHT",
                              "body": "x" * s} for n, s in enumerate(sizes)]}

    def test_pages_reassemble_to_the_full_preview(self):
        p = self.payload([6000] * 7)
        pages = anchor.preview_pages(p)
        stripped = [pg.split("\n", 1)[1] for pg in pages]
        self.assertEqual("\n".join(stripped), anchor.render_preview(p))
        self.assertTrue(all(len(pg.encode()) <= 15000 + 200 for pg in pages))
        self.assertTrue(pages[0].startswith(f"page 1/{len(pages)}"))

    def test_an_oversize_comment_gets_its_own_page_unsplit(self):
        p = self.payload([100, 20000, 100])
        pages = anchor.preview_pages(p)
        big = [pg for pg in pages if "x" * 20000 in pg]
        self.assertEqual(len(big), 1)
        self.assertIn("exceeds the page limit", big[0].split("\n", 1)[0])

    def test_cli_writes_preview_txt_and_prints_one_page(self):
        d = tempfile.mkdtemp(); pp = os.path.join(d, "payload.json")
        with open(pp, "w") as fh:
            json.dump(self.payload([6000] * 7), fh)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            anchor.cmd_preview(SimpleNamespace(payload=pp, page=2))
        self.assertTrue(out.getvalue().startswith("page 2/"))
        self.assertTrue(os.path.isfile(os.path.join(d, "preview.txt")))
```

- [ ] **Step 2: Verify failure.**

- [ ] **Step 3: Implement.** Split `render_preview` into chunks without changing its output:

```python
def preview_chunks(payload):
    comments = payload.get("comments", [])
    rule = "=" * 72
    chunks = ["\n".join([rule, "PR REVIEW PAYLOAD PREVIEW", f"event: {payload.get('event', '')}",
                         f"comments: {len(comments)}", rule, "", "REVIEW BODY", "-" * 72,
                         payload.get("body", ""), ""])]
    for n, c in enumerate(comments, start=1):
        chunks.append("\n".join(["-" * 72,
                                 f"COMMENT {n}/{len(comments)} - {c.get('path', '')}:"
                                 f"{c.get('line', '')} ({c.get('side', '')})",
                                 "-" * 72, c.get("body", ""), ""]))
    return chunks


def render_preview(payload):
    # (keep the existing docstring)
    return "\n".join(preview_chunks(payload))


PAGE_LIMIT = 15000


def preview_pages(payload, limit=PAGE_LIMIT):
    """Pack whole chunks into pages of at most `limit` bytes. A chunk larger than
    the limit gets a page of its own - a comment is never split, because the
    user must read each one whole before approving it."""
    pages, cur = [], []
    size = lambda parts: len("\n".join(parts).encode("utf-8"))
    for ch in preview_chunks(payload):
        if cur and size(cur + [ch]) > limit:
            pages.append(cur); cur = []
        cur.append(ch)
    if cur:
        pages.append(cur)
    out = []
    for n, parts in enumerate(pages, start=1):
        text = "\n".join(parts)
        head = f"page {n}/{len(pages)}"
        if len(text.encode("utf-8")) > limit:
            head += " (one comment that exceeds the page limit; shown whole)"
        out.append(head + "\n" + text)
    return out
```

In `cmd_preview`, after the existing checks:

```python
    full = render_preview(payload)
    txt = os.path.join(os.path.dirname(os.path.abspath(args.payload)), "preview.txt")
    with open(txt, "w", encoding="utf-8") as fh:
        fh.write(full)
    print(f"preview written: {txt}", file=sys.stderr)
    page = getattr(args, "page", None)
    if page is None:
        print(full)
        return 0
    pages = preview_pages(payload)
    if not 1 <= page <= len(pages):
        print(f"error: page {page} out of range 1..{len(pages)}", file=sys.stderr)
        return 1
    print(pages[page - 1])
    return 0
```

`main`: `w.add_argument("--page", type=int)`. The existing `TestPreviewCli` tests print to stdout
without `--page` and must keep passing; the new stderr line must not break
`_assert_single_curated_error` (it only runs on the error paths, which return before the write).

- [ ] **Step 4: Run whole suite; mutation-check** the oversize branch and the byte limit.

- [ ] **Step 5: Commit** - `feat(preview): page output under 15 KB on comment boundaries, always write preview.txt`.

---

### Task 10: `report`

**Files:** Modify `ledger_tools.py`, `pr-review-anchor.py`; Test `test_ledger_tools.py`

**Interfaces:** `render_report(ledger, ledger_path) -> str`; CLI `report <ledger>` writes
`<ledger_dir>/report.md` and prints the same text.

- [ ] **Step 1: Write failing tests**

```python
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

    def test_no_raw_html(self):
        self.assertNotRegex(self.text, r"<(details|summary|br)\b")
```

- [ ] **Step 2: Verify failure.**

- [ ] **Step 3: Implement**

```python
def render_report(ledger, ledger_path):
    items = ledger.get("items", [])
    ldir = os.path.dirname(os.path.abspath(ledger_path))
    by_v = {v: [it for it in items if it.get("verdict") == v] for v in VERDICTS}
    sev = lambda it: (it.get("severity") if it.get("severity") == it.get("severity_in")
                      else f"{it.get('severity_in')} -> {it.get('severity')}")
    lowered = sum(SEV_RANK.get(it.get("severity"), 0) > SEV_RANK.get(it.get("severity_in"), 0) for it in items)
    raised = sum(SEV_RANK.get(it.get("severity"), 0) < SEV_RANK.get(it.get("severity_in"), 0) for it in items)
    out = ["## Triage", "", "| id | severity | verdict | cluster | location | claim |",
           "| --- | --- | --- | --- | --- | --- |"]
    for it in items:
        out.append(f"| {it['id']} | {sev(it)} | {it.get('verdict')} | {it.get('cluster') or ''} | "
                   f"`{it.get('file')}:{it.get('line')}` | {it.get('claim')} |")
    n = len(items)
    out += ["", "## Verdict summary", "",
            " / ".join(f"{v} {len(by_v[v])}" for v in VERDICTS)
            + f"; refuted {len(by_v['REFUTED'])}/{n}"]
    if n and not by_v["REFUTED"]:
        out.append("warning: nothing was refuted - treat the pressure-test as uncalibrated "
                   "until you have looked at why")
    if lowered and not raised:
        out.append(f"warning: {lowered} lowered / 0 raised - a one-sided revision is itself a "
                   "claim about the generator")
    rc = [it for it in items if it.get("claim_verdict") == "REWRITTEN"]
    out += ["", "## Claim audit", "",
            f"SOUND {sum(it.get('claim_verdict') == 'SOUND' for it in items)} / REWRITTEN {len(rc)}; "
            f"severity {lowered} lowered / {raised} raised"]
    out += [f"- #{it['id']}: {it.get('claim_original')} -> {it.get('claim')}" for it in rc]
    rf = [it for it in items if it.get("fix_verdict") == "REWRITTEN"]
    uf = [it for it in items if it.get("fix_verdict") == "UNVERIFIABLE"]
    out += ["", "## Fix audit", "",
            f"SOUND {sum(it.get('fix_verdict') == 'SOUND' for it in items)} / REWRITTEN {len(rf)} / "
            f"UNVERIFIABLE {len(uf)}"]
    out += [f"- #{it['id']}: {it.get('fix_original')} -> {fix_slot(it.get('detail'))}" for it in rf]
    out += [f"- #{it['id']} UNVERIFIABLE (needs a human before it posts): {it.get('fix_reason')}"
            for it in uf]
    out += ["", "## Downgraded", ""]
    out += [f"- #{it['id']} {sev(it)} - {it.get('claim')} - {it.get('verdict_reason')} "
            f"[{it.get('fold_note')}]" for it in by_v["DOWNGRADED"]] or ["none"]
    out += ["", "## Unresolved", ""]
    out += [f"- #{it['id']} - {it.get('claim')} - {it.get('verdict_reason')} (needs a human call)"
            for it in by_v["INCONCLUSIVE"]] or ["none"]
    out += ["", "## Refuted (not posted unless you name them)", ""]
    out += [f"- #{it['id']} - {it.get('claim')} - {it.get('verdict_reason')}"
            for it in by_v["REFUTED"]] or ["none"]
    dd = [it for it in items if it.get("dedupe")]
    out += ["", "## Previously reported", ""]
    out += [f"- #{it['id']} - {it['dedupe'].get('prior_url')}" for it in dd] or ["none"]
    live = [it for it in items if it.get("verdict") != "REFUTED"]
    inline = sum(it.get("on_changed_line") is True for it in live)
    body = len(live) - inline
    owners = {}
    for it in items:
        owners.setdefault(it.get("refuter_key"), []).append(it["id"])
    largest = max((len(v) for k, v in owners.items() if k not in (None, "none")), default=0)
    real = [k for k in owners if k not in (None, "none")]
    out += ["", "## Pressure-test dispatch", "",
            f"{len(real)} refuters for {n} items, largest owned {largest}",
            "", "## Files", "",
            f"    Ledger:   {os.path.abspath(ledger_path)}",
            f"    Report:   {os.path.join(ldir, 'report.md')}",
            f"    Worktree: {ledger.get('worktree')}",
            "", "## To post", "",
            f"    /pr-review-post all          # {inline} inline + {body} in the body; "
            f"{len(by_v['REFUTED'])} refuted excluded",
            "    /pr-review-post 1 3-7        # a chosen subset (ranges allowed)",
            "    /pr-review-post all --include-refuted <id>   # also post a refuted finding",
            "", "Placement follows anchoring, not verdict: every selected finding on a changed "
            "line posts inline whatever its verdict; the rest go in the review body."]
    return "\n".join(out) + "\n"
```

CLI: `report <ledger>` - load, render, write `report.md`, print. (The test for
`"2 inline + 1 in the body"`: live = items 1, 2, 3; inline = 1, 2; body = 3.)

- [ ] **Step 4: Run; mutation-check** both calibration warnings and the inline/body arithmetic.

- [ ] **Step 5: Commit** - `feat: generate the S6 report from the ledger`.

---

### Task 11: `pr-review-wt.sh` - refuter worktrees, primary status, pnpm-aware link-deps

**Files:** Modify `pr-review-wt.sh`, `pr-review-wt.test.sh`

**Interfaces:** `remove-refuters <scratch> <n>`; `primary-status <wt>`; reaper exception for
`*/scratchpad/wt-pr*-r[ci][0-9]*`.

- [ ] **Step 1: Write failing checks.** Append before the final summary line of
`pr-review-wt.test.sh`, and update the header's `Expected: passed=N` to the new total:

```bash
echo "== reap: a stale dirty refuter worktree is disposable =="
setup_repo
R=$(mktemp -d); mkdir -p "$R/other-session/scratchpad"
SP="$R/other-session/scratchpad"
"$SCRIPT" create "$SP/wt-pr5-rc3" HEAD >/dev/null
"$SCRIPT" create "$SP/wt-pr5" HEAD >/dev/null
# create records this test shell's pid, which is alive - unlock so the dirty
# rule, not the live-lock rule, is what decides.
git worktree unlock "$SP/wt-pr5-rc3"; git worktree unlock "$SP/wt-pr5"
echo dirt > "$SP/wt-pr5-rc3/probe.txt"; echo dirt > "$SP/wt-pr5/probe.txt"
age_dir "$SP/wt-pr5-rc3" 30; age_dir "$SP/wt-pr5" 30
OUT=$(CLAUDE_SCRATCHPAD=/elsewhere/scratchpad "$SCRIPT" reap 2>&1)
check "dirty refuter wt reaped"   "$(git worktree list | grep -c wt-pr5-rc3)" "0"
check "dirty main wt kept"        "$(echo "$OUT" | grep -c "kept $SP/wt-pr5 dirty-")" "1"

echo "== reap: current-session refuter worktree is kept even when dirty =="
setup_repo
SP=$(mktemp -d)/scratchpad; mkdir -p "$SP"
"$SCRIPT" create "$SP/wt-pr5-ri12" HEAD >/dev/null
git worktree unlock "$SP/wt-pr5-ri12"
echo dirt > "$SP/wt-pr5-ri12/probe.txt"; age_dir "$SP/wt-pr5-ri12" 30
OUT=$(CLAUDE_SCRATCHPAD="$SP" "$SCRIPT" reap 2>&1)
check "current-session kept"      "$(git worktree list | grep -c wt-pr5-ri12)" "1"

echo "== remove-refuters removes every refuter worktree for the PR only =="
setup_repo
SP=$(mktemp -d)/scratchpad; mkdir -p "$SP"
for w in wt-pr5-rc1 wt-pr5-ri2 wt-pr5 wt-pr6-rc1; do "$SCRIPT" create "$SP/$w" HEAD >/dev/null; done
echo dirt > "$SP/wt-pr5-rc1/x"
"$SCRIPT" remove-refuters "$SP" 5 >/dev/null
check "rc1 gone"   "$([ -d "$SP/wt-pr5-rc1" ] && echo yes || echo no)" "no"
check "ri2 gone"   "$([ -d "$SP/wt-pr5-ri2" ] && echo yes || echo no)" "no"
check "main kept"  "$([ -d "$SP/wt-pr5" ] && echo yes || echo no)" "yes"
check "other PR kept" "$([ -d "$SP/wt-pr6-rc1" ] && echo yes || echo no)" "yes"

echo "== primary-status reports the primary checkout, not the worktree =="
setup_repo
echo change >> f.txt
WT=$(mktemp -d)/wt; "$SCRIPT" create "$WT" HEAD >/dev/null
check "primary dirt shown" "$("$SCRIPT" primary-status "$WT" | grep -c 'f.txt')" "1"

echo "== link-deps: declines when pnpm-lock.yaml changed =="
setup_repo
mkdir -p node_modules && echo x > node_modules/marker
echo 'a: 1' > pnpm-lock.yaml; git add pnpm-lock.yaml; git commit -qm lock
BASE=$(git rev-parse HEAD)
echo 'a: 2' > pnpm-lock.yaml; git add pnpm-lock.yaml; git commit -qm bump
WT=$(mktemp -d)/wt; "$SCRIPT" create "$WT" HEAD >/dev/null
check "pnpm lock declines" "$("$SCRIPT" link-deps "$WT" "$BASE" | grep -c '^declined lockfile-changed:pnpm-lock.yaml')" "1"

echo "== link-deps: links per-package node_modules too =="
setup_repo
mkdir -p node_modules libs/a/node_modules && echo x > libs/a/node_modules/m
echo '{}' > libs/a/package.json; git add libs/a/package.json; git commit -qm pkg
BASE=$(git rev-parse HEAD)
WT=$(mktemp -d)/wt; "$SCRIPT" create "$WT" HEAD >/dev/null
"$SCRIPT" link-deps "$WT" "$BASE" >/dev/null
check "package node_modules linked" "$([ -f "$WT/libs/a/node_modules/m" ] && echo yes || echo no)" "yes"
```

(Check `setup_repo`'s gitignore: if it does not ignore `node_modules`, add
`echo node_modules > .gitignore` inside the per-package test before committing.)

- [ ] **Step 2: Run** `bash pr-review-wt.test.sh` - the new checks fail.

- [ ] **Step 3: Implement**

In `consider()`, wrap the dirty guard:

```bash
  # A refuter's private worktree exists only to hold throwaway probes, so its
  # dirt is disposable by construction. Every other review path keeps the guard.
  case "$wt" in
    */scratchpad/wt-pr*-r[ci][0-9]*) ;;
    *)
      local d; d=$(dirty_count "$wt")
      if [ "$d" -gt 0 ]; then echo "kept $wt dirty-${d}-changes"; return 0; fi
      ;;
  esac
```

New subcommands:

```bash
cmd_remove_refuters() {
  local scratch="${1:-}" n="${2:-}"
  if [ -z "$scratch" ] || [ -z "$n" ]; then
    echo "usage: pr-review-wt.sh remove-refuters <scratchpad> <pr-number>" >&2; return 2
  fi
  local d
  for d in "$scratch"/wt-pr"$n"-r[ci][0-9]*; do
    [ -e "$d" ] || continue
    cmd_remove "$d"; echo "removed $d"
  done
  git worktree prune >/dev/null 2>&1
  return 0
}

cmd_primary_status() {
  local wt="${1:-}"
  [ -n "$wt" ] || { echo "usage: pr-review-wt.sh primary-status <worktree>" >&2; return 2; }
  local common
  common=$(git -C "$wt" rev-parse --git-common-dir 2>/dev/null) || {
    echo "error: not a git worktree: $wt" >&2; return 1; }
  case "$common" in /*) ;; *) common="$wt/$common" ;; esac
  git -C "$common/.." status --porcelain
}
```

In `cmd_link_deps`, widen the lockfile pathspecs to
`'*package-lock.json' '*pnpm-lock.yaml' '*yarn.lock' '*bun.lockb'`, and after the root `ln -s`:

```bash
  local extra=0 pkg
  while IFS= read -r pkg; do
    pkg=$(dirname "$pkg"); [ "$pkg" = "." ] && continue
    if [ -d "$main_repo/$pkg/node_modules" ] && [ ! -e "$wt/$pkg/node_modules" ]; then
      ln -s "$main_repo/$pkg/node_modules" "$wt/$pkg/node_modules" && extra=$((extra+1))
    fi
  done < <(git -C "$wt" ls-files -- '*package.json')
  echo "linked $wt/node_modules (+$extra package node_modules)"
```

(replacing the old single `echo "linked ..."`). Add both subcommands to the dispatch `case` and
the usage line.

- [ ] **Step 4: Run** `bash pr-review-wt.test.sh`; all pass. Mutation-check the reaper exception
(delete the `case` arm - "dirty refuter wt reaped" fails) and the pnpm glob.

- [ ] **Step 5: Commit** - `feat(wt): disposable refuter worktrees, primary-status, pnpm-aware link-deps`.

---

### Task 12: Real-data regression fixtures

**Files:** Create `tests/fixtures/audit-defects.json`, `tests/make_fixtures.py`; Modify
`test_ledger_tools.py`

- [ ] **Step 1: Extract the defective items.** Create `tests/make_fixtures.py`:

```python
"""One-off: extract the audited defects from real ledgers into a small fixture.
Kept so the fixture can be regenerated; not run by the test suite."""
import json, sys

SOURCES = {
    # scratchpad ledger path (under /tmp/claude-1000/) : ids to keep
    "-home-lyonsm-scripture-editors/b57ce8d2-d8e1-4483-9eb4-1caca9143247/scratchpad/pr-review/pr13/ledger.json": [4, 14, 19],
    "-home-lyonsm-paranext-core/d2ca2fcf-6e00-4a89-8849-6ad558d74b7a/scratchpad/pr-review/pr2838/ledger.json": [8, 19],
    "-home-lyonsm-paranext-core/6a905e89-f246-4eb4-90d5-709a1a36bcc9/scratchpad/pr-review/pr2653/ledger.json": [11],
}
EXPECT = {  # (source index, id) -> substring the new validate must report
    (0, 4): "ellipsis", (0, 14): "hedged", (0, 19): "review-process wording",
    (1, 8): "DOWNGRADED", (1, 19): "REFUTED",
    (2, 11): "ellipsis",
}

out = []
for n, (path, ids) in enumerate(SOURCES.items()):
    led = json.load(open("/tmp/claude-1000/" + path))
    for it in led["items"]:
        if it["id"] in ids:
            out.append({"source": n, "item": it, "expect": EXPECT[(n, it["id"])]})
json.dump(out, open(sys.argv[1], "w"), indent=2)
```

Run: `python3 tests/make_fixtures.py tests/fixtures/audit-defects.json`. If any source ledger is
gone, replace its entries with synthetic items reproducing the same defect (copy the `item()`
builder shape) and note it in the commit message. Open the fixture and confirm each item actually
shows its defect (e.g. item 19 of pr13 contains "Item #8 in this batch").

- [ ] **Step 2: Write the test**

```python
class TestAuditRegression(unittest.TestCase):
    def test_every_audited_defect_is_now_rejected(self):
        cases = json.load(open(os.path.join(HERE, "tests", "fixtures", "audit-defects.json")))
        self.assertGreaterEqual(len(cases), 6)
        for c in cases:
            it = dict(c["item"]); it["id"] = 1
            errs = anchor.validate(led(it))
            self.assertTrue(any(c["expect"] in e for e in errs),
                            f"source {c['source']} item {c['item']['id']}: {errs}")
```

- [ ] **Step 3: Run; expect pass.** If an expectation fails, the audit defect is not caught -
fix the validator rule, not the fixture.

- [ ] **Step 4: Commit** - `test: regression fixtures from the audited review runs`.

---

### Task 13: Rewrite the command prompts

**Files:** Modify `commands/pr-review.md`, `commands/pr-review-post.md`, `README.md`

This task has no unit tests; its checks are greps and one dry walk-through.

- [ ] **Step 1: `pr-review.md` S0 additions.** After step 6 (create the worktree), add:

```markdown
7. Snapshot the primary checkout so S6 can prove nothing touched it:
   `~/.claude/pr-review/pr-review-wt.sh primary-status "$WT" > "$SCRATCH/primary.before"`
8. Link dependencies now, not in S5: `~/.claude/pr-review/pr-review-wt.sh link-deps "$WT" "$MERGE_BASE"`.
   Record the `linked ...` / `declined ...` line.
9. Find this repo's real commands - read the root `package.json` scripts, `nx.json`/`project.json`,
   `*.csproj`/`*.sln`, `Makefile` - and record the single-file test, single-project typecheck and
   single-file lint command. Never assume `npm run ...` exists.
```

Replace step 5's rebase check with:

```markdown
   **Check whether the base was rebased.** `main` moving ahead of this branch is the normal case and
   needs nothing. A rebased base shows up as commits the base already carries under a different
   SHA:

   ```bash
   git cherry "$BASE_TIP" "$HEAD_SHA" | grep -q '^-' && echo "base was rebased - derive the fork point"
   ```
```

(keep the fork-point derivation and the two-bases table that follow it).

- [ ] **Step 2: S1.** Move the "Snapshot the worktree registry" block to run after S0 step 6
(it already follows create; reword "**before** invoking the skill" to "after creating `$WT` and
before invoking the skill"), and replace the delta command with:

```bash
comm -13 <(grep '^worktree ' "$SCRATCH/worktrees.before" | sort) \
         <(grep '^worktree ' "$SCRATCH/worktrees.after" | sort) | grep -F "$SCRATCH" || true
```

Append a new subsection at the end of S1:

```markdown
### While the generator runs

The skill runs in the background; a task-notification arrives when it finishes. Until then do
preparation only:

- the S3 comment fetch,
- the obligation table (S5),
- the posting diff: `GH_REPO="$REPO" gh pr diff <n> > "$SCRATCH/pr-review/pr<n>/head.diff"`,
- the brief context file `$SCRATCH/pr-review/pr<n>/ctx.json`:
  `{"link_deps": "...", "commands": "...", "obligations": "<the table>", "claude_md_paths":
  ["$WT/CLAUDE.md", ...], "scratch": "$SCRATCH"}`.

Then end your turn. Do not review the code yourself, do not run the test suite, and never use
`ScheduleWakeup`, `/loop`, `sleep`, or a placeholder command to wait.
```

- [ ] **Step 3: S2/S4.** Add to S2: "Ids assigned here are stable until S6's `renumber`. Refer to
another finding only as `{{#id}}`." Add to S4: "Then write the draft ledger (see S6 step 1 for
the fields present at this point). This is the last time you write ledger JSON directly." Move the
schema block from S6 step 1 to here, updated: `"schema": 2`; add `refuter_note`, `claim_original`,
`fix_original`, `refuter_key`, `old_id`, `fold_note`, `unanswered` with one-line comments matching
the spec's schema table; note verdict fields are written by `ingest`.

- [ ] **Step 4: S5.** Replace the dispatch paragraphs, "The refuter's job", "second job", "third
job", reply format, and "Model" sections (current lines 312-555 except "The obligation table")
with:

```markdown
## S5. Pressure-test

For every refuter key - each cluster id, and `i<id>` for each unclustered item - render its brief:

```bash
python3 ~/.claude/pr-review/pr-review-anchor.py brief <ledger> --key <KEY> \
  --context "$SCRATCH/pr-review/pr<n>/ctx.json"
```

It prints the brief's path. `brief` refuses a key that is not one cluster or one unclustered item,
an obligation row that cites no repo file, or a placeholder it could not fill. Guidance you want a
refuter to have goes in that item's `refuter_note` in the draft ledger - never in `claim` or
`detail`.

Dispatch one Agent per key, `model: "sonnet"`, with exactly this prompt:
`Read <brief path> and follow it exactly.` Run at most 15 at once; launch the next as each
finishes. Ignore task-notifications whose result says the report was delivered as a message.

After dispatching, arm the watchdog - the only wakeup this command may use:
`ScheduleWakeup(delaySeconds=1200, prompt="pr-review watchdog: check refuter progress")`.
On waking, stat (never read) each outstanding refuter's output file. One unchanged for 15 minutes:
`TaskStop` it and dispatch it again once. A second stall: leave it for `ingest --inconclusive`.
Re-arm until every verdict file exists, then stop the wakeup.

When all have answered:

```bash
python3 ~/.claude/pr-review/pr-review-anchor.py ingest <ledger>
~/.claude/pr-review/pr-review-wt.sh remove-refuters "$SCRATCH" <n>
```

`ingest` names every problem at once and changes nothing if there is one. For each refuter it
names, re-dispatch once with the error lines appended to the prompt. If the same refuter fails
again: `ingest <ledger> --inconclusive <ids> --reason "<what went wrong>"`. Never edit the ledger to
get past `ingest`.
```

Keep "The obligation table" section, adding: "Every row's last cell names the repo file the
obligation comes from. A preference of yours or of the user's that no repo file states is not an
obligation."

Delete the "Model" section's cheaper-model allowance entirely; the one-line rule "Refuters always
run on Sonnet" now lives in the dispatch paragraph above.

- [ ] **Step 5: S6.** Replace steps 1-6 with:

```markdown
## S6. Report

Run, in order, stopping on any `error:`:

```bash
A=~/.claude/pr-review/pr-review-anchor.py; L=<ledger>
python3 $A excerpt $L --worktree "$WT"
python3 $A changed-lines $L --diff "$SCRATCH/pr-review/pr<n>/head.diff" --worktree "$WT"
```

Then the two judgment steps, each written back with `edit` (never `python -c`, never a JSON edit):

1. For each DOWNGRADED item, fold what its `verdict_reason` means for the author into
   `Why it matters:` (`edit $L <id> --field detail --text-file <f>`), and record
   `edit $L <id> --field fold_note --text-file <f>` with `folded` or `bookkeeping`.
2. The quantifier sweep:
   `grep -nP '(?<![\w-])(every|all|any|always|never|only|whole|entire|cannot|no [a-z]+)(?![\w-])'`
   over the claims. For each hit, cite the evidence or reword via `edit`. Write each hit and its
   disposition into `$SCRATCH/pr-review/pr<n>/quantifiers.md`.

Then:

```bash
python3 $A renumber $L
python3 $A validate $L --worktree "$WT"
python3 $A reanchor $L --worktree "$WT" --diff "$SCRATCH/pr-review/pr<n>/head.diff" --dry-run
~/.claude/pr-review/pr-review-wt.sh primary-status "$WT" | diff "$SCRATCH/primary.before" -
python3 $A report $L
```

If the primary-status diff is non-empty, stop and show the user the changed paths; do not restore
anything without approval. Relay `report`'s output in full, then the quantifier dispositions.
Then **stop**. Do not post anything.
```

Delete the old S6 prose explaining how to derive `on_changed_line`, the report bullets, and the
"Note for later" paragraph in S5 (its text on `DOWNGRADED`/`INCONCLUSIVE` being carried in the
body is wrong).

- [ ] **Step 6: `pr-review-post.md`.**
  - Step 1.3: "A `schema` error means the ledger predates this version: re-run `/pr-review`."
  - Step 2: add `--head "$NOW"` to the `reanchor` call; replace "dead" wording with "dead or
    off-diff" in the demotion paragraph.
  - Step 3: delete lines 248-257's `notice:` sentence about items "carried in the body by
    verdict"; delete "(the tool output itself is shown in the transcript)" and replace the preview
    paragraph with:

    ```markdown
    Show the preview page by page, as your own message text - tool output does not reach the
    user's screen:

    ```bash
    python3 ~/.claude/pr-review/pr-review-anchor.py preview "$SCRATCH/pr-review/pr$NUMBER/payload.json" --page 1
    ```

    Its first line says `page 1/M`. Paste each page verbatim into a message of its own (copy the
    tool output; never retype or summarize it), fetch the next with `--page 2`, and continue to
    page M. Only after the last page, ask for approval once. Also give the path of `preview.txt`.
    ```

  - The framing paragraph: "State the number of findings as `{count}`; `payload` fills it in."
  - Step 4: compare `$NOW` against the ledger's `anchored_sha`, not `head_sha`.
  - Step 5: add `~/.claude/pr-review/pr-review-wt.sh remove-refuters "$SCRATCH" "$NUMBER"` before
    removing `$WT`.
  - `--select` accepts ranges: say so in step 1.7.

- [ ] **Step 7: American English and stale-text greps**

```bash
cd ~/.claude/pr-review
sed -i 's/honours/honors/g; s/honour/honor/g; s/behaviour/behavior/g; s/colour/color/g; s/neighbour/neighbor/g; s/labelled/labeled/g; s/flavoured/flavored/g; s/judgement/judgment/g; s/normalis/normaliz/g' commands/*.md refuter-brief.tmpl.md README.md
grep -nE 'honour|behaviour|colour|neighbour|labelled|flavoured|judgement' commands/*.md refuter-brief.tmpl.md README.md   # expect nothing
grep -nE 'carried in the (review )?body (automatically|by verdict)|shown in the transcript|cheaper model|\$CLAUDE_SCRATCHPAD' commands/*.md   # expect only the "no \$CLAUDE_SCRATCHPAD" warnings
grep -n 'Template' refuter-brief.tmpl.md >/dev/null; python3 -c "import sys; sys.path.insert(0,'.'); import ledger_tools as t; print(t.template_hash())"
```

The template hash changes whenever the template changes - that is intended.

- [ ] **Step 8: README.** Add the new subcommands to both bullet lines, and:
"`~/.claude/commands/pr-review*.md` are symlinks into `commands/` here - edit these files, not the
links." Also list `refuter-brief.tmpl.md`.

- [ ] **Step 9: Dry walk-through.** Build a tiny ledger in the scratchpad from `tests/fixtures`
shapes and run every command in the order S4 -> S6 -> post steps 2-3 prescribe (brief, a
hand-written verdict file, ingest, excerpt, changed-lines, edit, renumber, validate, reanchor
--dry-run, report, payload, preview --page 1). Every command must exist and accept exactly the
arguments the prompts give it. Fix any mismatch in the prompt, not by adding aliases.

- [ ] **Step 10: Commit** - `feat(prompts): drive S5/S6 and posting through the new subcommands`.

---

### Task 14: DECISIONS, memory, final check

**Files:** Modify `DECISIONS.md`; memory files under
`~/.claude/projects/-home-lyonsm-paranext-core/memory/`

- [ ] **Step 1: DECISIONS.md.** In "Structural", replace "v1 covers fresh reviews only. ..." with
"Fresh reviews only until re-review mode lands (spec part B)." Add a new section:

```markdown
## Pipeline v2: ledger tooling (2026-09-22)

- **Mechanical steps are tools, not prose.** Seven of eight audited runs hand-built the ledger and
  the defects that reached approval came from those scripts. *If wrong:* the subcommands are thin
  and the prompt can call fewer of them.
- **Schema 2, no migration.** Ledgers are session-scoped, so an old one is re-run, not upgraded.
- **The cheaper-model refuter allowance is gone.** Broken in three runs; the saving was small.
- **Refuter worktrees are disposable by name.** `wt-pr<n>-r<key>` skips the reaper's dirty guard;
  the main `wt-pr<n>` keeps it. *If wrong:* a live refuter in a dead session loses probes, never
  review state.
- **One watchdog, no other waiting.** A replayed `ScheduleWakeup` re-entered the whole command
  three times in one run; polling burned turns in six.
- **DOWNGRADED means a lower severity.** A narrowed claim at the same severity is CONFIRMED with a
  rewritten claim. REFUTED never carries a rewrite.
- **Lint wording checked against real ledgers.** `rewrite`, `downgrad`, bare `optional` and bare
  `not required` were dropped from the lints after they matched legitimate author-facing fixes.
- **Obligations cite a repo file.** A user preference once posted as "the project convention".
```

- [ ] **Step 2: Memory.** Update
`feedback_show_user_output_not_tool_output.md`: replace the `SendUserFile` instruction with
"For `/pr-review-post`, run `preview --page N` and paste each page as message text; tool output
never reaches the user." Update `project_pr_review_anchor_excerpt_verbatim.md`: "Fixed in
pipeline v2: excerpts come only from the `excerpt` subcommand, and `validate --worktree` rejects a
non-verbatim one." Read each file first and keep its frontmatter.

- [ ] **Step 3: Final suite and grep**

```bash
cd ~/.claude/pr-review && python3 -m pytest -q && bash pr-review-wt.test.sh | tail -1
grep -rn 'TODO\|TBD' ledger_tools.py pr-review-anchor.py refuter-brief.tmpl.md commands/   # expect nothing
```

- [ ] **Step 4: Commit** - `docs: record pipeline v2 rulings`.
