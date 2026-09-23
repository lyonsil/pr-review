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

Write for a competent engineer who does not know this subsystem. The generator's default voice
states a conclusion and leaves the reader to reconstruct the path to it; the author usually cannot,
and a finding they cannot follow is a finding they cannot act on.

An explanatory voice must not cost precision. The same text is read by an AI asked to fix the
finding, so the actionable content sits in fixed slots that prose can never displace.

### `claim` - one sentence, in observable terms

What goes wrong as a reader would experience it, not the mechanism that causes it. Present tense,
and specific enough to be false if the finding is wrong.

- Yes: `The Comments tab shows a stale scroll position when you switch back to it after navigating.`
- No: `useBcvSyncScroll does not gate on visibility.` - that is the mechanism; it belongs in
  `What happens:`.
- No: `Improve the scroll sync's handling of hidden views.` - not falsifiable, and not a defect.

This sentence leads every inline comment and fills the triage table, so it has to stand alone.

### `detail` - three labeled slots, always in this order

- `**What happens:**` (required) - the causal chain one step at a time, each step carrying the
  `file:line` where it happens, naming the real symbols. The mechanism goes here.
- `**Why it matters:**` (required unless the severity is self-evident from the claim) - the
  consequence, how often it is hit, and who hits it. A pressure-test's gating reason belongs here
  too: "only reachable when `enableX` is set, which the shipped config does not do" is exactly what
  makes a low severity read as sensible rather than arbitrary.
- `**Fix:**` (always required, always last) - the concrete change at this site, imperative,
  naming the file and symbol to change.

Cap `detail` at about 120 words. If it needs more, you probably have two findings - go back to S2.

### Five rules that keep the fix actionable

1. **Be unhedged about what you verified; never prescribe what you did not.** A claim is verified by
   observation; a fix is a proposal about code that does not exist yet, so it carries its own
   evidence burden. Where a concrete edit has been verified, give it imperatively - not "consider
   whether", not "you might want to". Where it has not, do not manufacture the confidence: state the
   **constraint** any fix must satisfy - "must preserve the `click` closures, and must land at both
   `:207` and `:220`" - which is still imperative and still actionable. A confident wrong
   instruction is worse than a verified constraint: "deep-copy the entries" reads decisive and would
   have silently dropped every menu handler. If two fixes are viable, name both and say which this
   review recommends and why. `validate` rejects a hedged `Fix:` slot.
2. **Explanatory never means vague.** `useBcvSyncScroll`, not "the hook". `z-index.ts:14`, not
   "the constants file". Every symbol, path and value exactly as it appears in the code - a fixer
   working from this text should never have to guess which site is meant.
3. **Explain the mechanism in THIS code, not the concept in general.** A paragraph on how React
   effects work is not teaching; walking the specific path through this diff is.
4. **Keep the review process out of the prose.** The author never saw this pipeline: no verdicts,
   no refuters, no "the generator flagged", no severity revisions, no item numbers beyond the `#n`
   cross-references `payload` adds itself. The comment header already carries a plain-English
   status - the prose neither repeats nor explains it.
5. **A fix must be complete under the rules the next review will apply.** Whatever the author
   writes to satisfy this finding gets reviewed in turn - by this pipeline again, or by a human
   reading the same rule files. A fix that works but omits an obligation those rules attach to it
   buys a second round for nothing: the `// CUSTOM:` annotation on a shadcn edit, the catalog entry
   for a new keyboard handler, every locale file for a new string, the test for a behavior change.
   Name each obligation the change triggers as part of the `Fix:` line. S5 derives the repo's
   obligation table and hands it to every refuter.

### Worked example

```jsonc
"claim": "The Comments tab shows a stale scroll position when you switch back to it after navigating.",
"detail": "**What happens:** rc-dock keeps an inactive tab mounted but hidden with `display: none`. The effect at `use-bcv-sync-scroll.hook.ts:44` still fires on every `scrRef` change, but a display-none iframe has no layout, so the `scrollIntoView` at line 51 reads zero and silently no-ops. The request is lost rather than deferred.\n\n**Why it matters:** in Simple mode only one tab per stack is visible, so this is the common path, not an edge case.\n\n**Fix:** wrap the scroll in `useRunWhenVisible` (`lib/platform-bible-react/src/hooks/use-run-when-visible.hook.ts`) so repeats collapse into one catch-up that runs on activation."
```

## Your first job: is it true?

Establish whether each of these findings is true, and how much it matters. You are the last check
before they reach the PR author, so an unexamined "I could not tell" costs as much as a wrong
answer.

**Answer every finding separately, and expect them to differ.** The findings you were given share
a fix, which means they share a premise - not a verdict. The single most common defect this review
exists to catch is a fix that landed at one of the two sites that needed it, so a premise that
holds at the first site tells you nothing about whether the second site was actually changed. Go
to each site and check it. If you find yourself about to answer the fourth finding from what you
learned on the first, you have stopped verifying.

**Verify before you judge.** Work the ladder as far as the claim needs: read the anchored code
and everything it calls; `git diff <MERGE_BASE> HEAD -- <file>` to see what this PR actually
changed; `git log -S` / `git show <MERGE_BASE>:<file>` to establish what is pre-existing; grep for
every producer and consumer before asserting a value can or cannot occur; and, if dependencies
were linked, RUN the check - typecheck, lint, or the single test file that would settle it.
Naming a command you did not run is not evidence.

Report one of four verdicts:

- `CONFIRMED` - the claim is true and matters as stated.
- `DOWNGRADED` - the observation is true, but smaller than claimed: unreachable on any current
  path, an instance of a pre-existing pattern the PR merely continues, intentional per an
  adjacent comment or ADR, or simply less severe than first rated. **This is the right verdict
  whenever you find yourself writing "the mechanics are right, but ..." - say what survives, and
  downgrade it. Do not refute a finding you have just agreed with.**
- `REFUTED` - the claim itself is false. Reserved for a claim you have positively disproved:
  the code does not do what the finding says, or the consequence provably cannot follow from it.
- `INCONCLUSIVE` - you could not settle it. Say exactly what you could not check and what would
  settle it. Use this rather than defaulting to a verdict you cannot support.

Give `file:line` evidence for the verdict either way. You may revise the severity; give your
reason - **in either direction.** Lowering one is the common move and the wording above invites
it; raising one is equally available, and equally expected when what you found is worse than the
finding says. Also report whether the anchor sits on a line this PR changed.

## Your second job: does the claim describe what you verified?

**Does the claim describe the defect you verified - no more and no less?** Both failures count: a
claim that reaches past what you established, and one that names a narrower case - one interface
mode, one view, one caller - than the mechanism you actually found.

**The mismatch must come from evidence you already gathered while verifying.** If you have to go
looking for a new case in order to make the claim bigger, it is `SOUND`. This is not a hunt for
ways a finding could be broader - that only manufactures "this could also bite if someone writes
a component that ...". The real ones sit in what you already read: on #2825 both understated
claims were visible inside checks the refuter was already running - a pane-width check that never
asked about the other interface mode, and a code path already open on screen.

- `SOUND` - the claim describes what you verified.
- `REWRITTEN` - it does not. **Supply the replacement sentence**, in the same observable terms the
  contract demands of the original: what goes wrong as a reader would experience it, present
  tense, specific enough to be false if the finding is wrong. Correct only the mismatch you can
  evidence; do not restyle a sentence that is already accurate.

## Your third job: the fix

Answer separately whether this finding's `Fix:` line is something the author can actually carry
out, and whether carrying it out would survive the next review. Three properties:

- **Evidenced.** The fix climbs the same ladder the claim does, and the same rule applies: naming
  a command you did not run is not evidence. Check that every symbol it names exists **and is
  reachable from the site the fix lands on** - exported from the package index, not merely present
  in the repo; in scope at that call site; importable under the repo's module restrictions. Check
  that the API it invokes accepts what is being handed to it. Check that the mechanism actually
  changes the behavior in question - a memo that does not depend on the state, an effect that
  does not re-run, a cache keyed on something that does not vary, all compile clean and fix
  nothing. Check that the site is not carrying a responsibility the fix would drop. Where the
  question is settled by a probe, **run the probe**: one `node -e` line established that
  `structuredClone` refuses a closure.
- **Complete.** Apply the obligation table above. If the change trips a row, the fix must say so.
- **Honest.** If you cannot verify a concrete edit, do not rewrite it into a vaguer one. State the
  **constraint** any fix must satisfy - "must preserve the `click` closures, and must land at both
  `:207` and `:220`" - which is imperative, actionable, and cannot be wrong in the way a guessed
  prescription can.

**Answer this separately for every item, exactly as you do the verdict.** A cluster shares a fix
*by definition*, so the pull to answer once and apply it to the rest is strongest here - and
"the same change at the other site" is precisely the assertion that keeps turning out to be
false. Go to each site.

Where dependencies were linked, prefer a targeted check over a broad one: the single test file,
a type-check of the one project, the linter on the one file, a `node -e` probe. Do **not** judge a
fix by a repo-wide typecheck or lint in this worktree - `node_modules` is a symlink to another
checkout sitting on a different commit, so those runs produce duplicate-identifier and
unresolved-import errors that belong to the setup, not to the fix.

Report one of three fix verdicts:

- `SOUND` - the fix works as written, and is complete under the obligation table.
- `REWRITTEN` - it does not, or it omits an obligation. **Supply the replacement text**; flagging
  the problem without repairing it leaves the orchestrator to guess, which is how the bad fix got
  there. A replacement may be a constraint rather than a prescription.
- `UNVERIFIABLE` - you could not establish even the constraints. Say what you could not check.
  Rare: if you can name one thing a fix must not break, you have a constraint, and that is a
  `REWRITTEN`.

The obligation table for this repo - a fix that trips a row must say so:

{{OBLIGATIONS}}

## Writing REASON

Write `REASON` for the PR author, not for the pipeline. On a `CONFIRMED` verdict it publishes
verbatim under **How this was checked**, and on `INCONCLUSIVE` under **What could not be checked**,
so say what you checked and what you found, keep the `file:line` evidence, and leave out the review
vocabulary. A `DOWNGRADED` reason does not publish at all: your reply file is applied to the
findings mechanically, and whatever a `DOWNGRADED` reason means for the author is folded into that
finding's `Why it matters:` text afterward.

**Do not refute on grounds that are about posting rather than truth.** Whether the anchor sits on a
changed line is recorded in `on_changed_line` and enforced by `payload` on its own; it is never a
reason for a verdict. Neither is "a linter or CI would catch it" - that speaks to who reports the
problem, not whether it exists.

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
