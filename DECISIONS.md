# Decisions made while building this

Rulings taken during implementation, with what each costs if wrong. Recorded because this tooling
is still being iterated on and the reasoning is not recoverable from the diffs.

## Structural

- **Built as a git repo at `~/.claude/pr-review`, commands symlinked into `~/.claude/commands/`.**
  The plan put everything in untracked `~/.claude/`, leaving no commits or diffs to review.
  *If wrong:* a directory move and a sed.
- **Session-scoped ledger, in the scratchpad.** Chosen for zero cleanup burden. The triage gate
  must therefore close same-day; a `/clear` between the two commands loses the review.
  *If wrong:* re-run `/pr-review`; moving the ledger to `~/.claude/` touches two paths.
- **Fresh reviews only until re-review mode lands (spec part B).**

## Safety of the destructive path

- **The reaper never removes a worktree holding uncommitted or untracked changes** (`kept ...
  dirty-N-changes`). A single `git worktree remove --force` destroys dirty and untracked content
  outright on git 2.52.0 - verified, not assumed. *If wrong:* a dirty abandoned worktree needs one
  manual `-f -f` and is named every run until then.
- **`cmd_remove` deliberately does NOT inherit that guard.** It is the explicit teardown path;
  refusing there would strand exactly the worktrees this tool exists to collect.
- **Dropped the `*/scratch/wt-*` glob** that existed only to suit a test fixture. Reaper breadth is
  the one place this tool destroys data, so it matches the spec exactly.
- **The live-pid lock guard is provenance only, not protection.** Each Bash call is a fresh shell,
  so the recorded pid is dead by the next call whatever the invocation shape. Real protection is
  the scratchpad path exclusion plus the 24h age floor. An earlier ruling claimed otherwise and was
  wrong; the spec and prompts were corrected.
- **Scratchpad path is derived inside the script, not passed in by the prompts.** Pinning it from
  the prompt would create split resolution - `create` pinned, a later `reap` derived, two different
  prefixes, exclusion silently misses the live worktree. *If wrong:* a harness path change degrades
  protection to the 24h age floor, which is the documented fallback, not unprotected deletion.
- **Reaping is name-based.** A native `EnterWorktree` tree named `review-*` is in scope; while in
  use it is protected by a live Claude pid, and once abandoned collecting it is desirable.

## Safety of the outward-facing path

- **Anchor per distinct code site**, never one-per-cluster. A cluster shares a *fix*, but the fix
  must land at every site; on PR #2565 "fixed one of the two sites" was the dominant failure, seven
  times over.
- **`--include-refuted` is per-id and requires the id to also be selected.** A blanket flag would
  defeat the point of refuting anything. Overruling by hand-editing `verdict` is forbidden - it
  destroys the refuter's recorded reasoning.
- **A dead anchor is excluded unconditionally** and cannot be lifted by `--include-refuted`.
  GitHub rejects the whole review if one anchor is off-diff.
- **An empty payload is an error, not a silent success.** A review carrying only framing and the
  attribution footer is never intended.
- **A pure re-indent classifies as `dead` rather than relocating.** The failure modes are
  asymmetric: a false `ok` puts a comment on the wrong line in front of the PR author; a false
  `dead` only demotes the finding to the body.
- **Re-anchoring runs unconditionally**, not only when the head moved - otherwise `anchor_status`
  is never set on the unchanged-head path and the dead-anchor filter cannot fire.

## Voice of a published review

Readers were finding the reviews hard to follow, in two separate ways: the finding prose assumed
the subsystem, and the scaffolding around it was pipeline vocabulary nobody outside this tool has a
glossary for.

- **Findings are drafted into fixed labelled slots**, not free prose: a one-sentence `claim` in
  observable terms, then `**What happens:**` / `**Why it matters:**` / `**Fix:**`. A teaching voice
  was the goal, but the same text is read by an AI asked to fix the finding, and unstructured
  explanation is exactly what buries the actionable sentence. Slots let the prose grow without the
  fix ever moving. *If wrong:* the contract is one section of `pr-review.md`; deleting it reverts
  to whatever the generator emits.
- **Labels say what they mean.** `confirmed` -> `checked and confirmed`, `unresolved` -> `needs a
  human call`, `Verification:` -> `How this was checked:`. The pipeline's stage names were never
  information the PR author could use.
- **The severity revision does not publish.** `severity_in -> severity` is how you judge whether
  the pressure-test is calibrated, so the S6 triage table still prints the arrow - but a PR author
  reading one comment has no use for the revision history of a number they never saw. `severity_in`
  stays in the ledger; `severity_text()` publishes the final value alone.
- **A downgrade's reasoning is folded into the finding, not appended as a footer.** "Why this was
  lowered" describes an internal revision; "only reachable when `enableX` is set, which the shipped
  config does not do" is the same fact written as something the author can use, and it belongs in
  `Why it matters:`. So `VERDICT_REASON_LABEL` has no `DOWNGRADED` entry and the footer renders
  nothing, while `verdict_reason` stays intact in the ledger for the terminal report. CONFIRMED and
  INCONCLUSIVE keep their footers: those reason about the code, not about the review.
- **With no arrow and no footer, DOWNGRADED renders identically to CONFIRMED** - which is correct,
  since it *is* a real finding at the severity shown. A distinguishing label would only describe a
  revision the author never saw.

## Calibrating the pressure-test

Measured over the first three real runs (PRs #2724/#2729/#2730, 141 items): a 73% refutation rate,
84% on one run. Most refutations were well-argued - the confirmed set drove six real fix commits on
#2724 - but the pipeline's own shape pushed the rate up and made it hard to read. One refutation
was demonstrably wrong (#2724 item 14, refuted as "pre-existing" on a line the PR had added; the
author removed exactly that line three days later). Rulings taken in response:

- **Four verdicts, not two.** About a third of refutations conceded the observation and refuted on
  reachability, scope, or overstated severity ("the mechanics are right, but ..."). A binary
  verdict forced those into REFUTED, which `payload` drops. `DOWNGRADED` now carries them into the
  review body, and `INCONCLUSIVE` holds genuine uncertainty instead of resolving it as a refutation.
  *If wrong:* the body grows a section nobody reads, and the vocabulary collapses back to two.
- **The refuter no longer defaults to REFUTED when it cannot verify.** The old instruction paired
  with "you cannot run tests/typecheck/lint" in one run's prompts, which is incoherent: it removed
  the means of verification while keeping the rule that failing to verify means refute. The refuter
  is now given a verification ladder and must climb it; `INCONCLUSIVE` is the only landing spot for
  what it still cannot settle, and it must name what would settle it.
- **`link-deps` exists so verification is possible at all**, and refuses whenever a tracked lockfile
  differs between the merge base and the head - a stale `node_modules` makes "I ran it and it
  passed" worse than no answer. *If wrong:* it declines more often than it needs to, which is the
  safe direction.
- **`link-deps` mirrors each `node_modules` entry by entry instead of linking the directory.** A
  whole-directory symlink resolves pnpm's relative workspace links (`node_modules/shared ->
  ../libs/shared`) against the primary checkout, so every test a refuter ran in `$WT` imported the
  primary's workspace source, not the head under review. On paranext/scripture-editors #11 that
  broke every refuter's platform test run (`collapseSpaceRuns is not a function`), several fell
  back to reading code, and one refuted a real defect from a static trace. Now an entry resolving to
  a tracked path outside any `node_modules` is pointed at the same path in `$WT`, while installed
  packages, `.bin` and the `.pnpm` store are still shared. Tool caches written into `node_modules`
  (vite's `.vite`) stay in `$WT` instead of landing in the primary. *If wrong:* a workspace
  package missing from the head falls back to the primary's copy, the old behavior.
- **`severity_in` is recorded and never overwritten.** Refuters revise severity in the same
  response as the verdict, so on PR #2730 all four `high` findings were refuted AND rewritten to
  `low`. The triage table then read as though only low-severity noise had been filtered. The pair
  is now shown as `high -> low` wherever it differs.
- **Two refute triggers were removed as dispositional rather than truth-based:** "not on a line
  this PR changed" (already its own field, already enforced by `payload` - it was being counted
  three times) and "a linter or CI would catch it" (which speaks to who reports a problem, not
  whether it exists).
- **`validate` rejects any non-terminal verdict.** One item in three runs was left `PENDING`
  because a refuter returned a well-formed verdict behind a prose preamble and the orchestrator
  never recorded it. `payload`'s exclusion is REFUTED-only, so that item would have posted as
  confirmed had it been selected. The gap was in `validate`, which never inspected `verdict` at all.
- **Refuters default to Sonnet, whatever tier the generator ran on.** A weaker refuter's
  characteristic failure - cannot trace a path, reads that as absence of a path - is precisely the
  bias being corrected, so Sonnet is the floor. It is also the ceiling by default: the generator
  runs at `max`, and inheriting that tier across one agent per finding made the pressure-test cost
  more than everything else in the pipeline combined. A cheaper model stays allowed for a single
  unclustered item that is single-file and single-site, but may not return REFUTED on a reachability
  argument, since that requires a whole-repo search. (This supersedes an earlier ruling that
  refuters inherit the generator's tier; the prompt was changed and this entry was not, which left
  the two disagreeing about the most expensive knob in the pipeline.)

## Verifying the fix, not just the finding

Measured on paranext-core PR #2821 (a 20-item posted review): **five of twenty `Fix:` lines were
unworkable as written, while every claim under them was sound.** Nothing in the pipeline had looked
at them - the generator writes the `Fix:` slot in S1, `payload` publishes `detail` verbatim, and S5
asked only whether the *claim* was true. They were caught by the operator asking, twice, by hand.

- `structuredClone` on menu entries carrying `click` closures (it throws; a JSON round-trip silently
  drops every handler - the fix would have caused the defect it was preventing)
- a symbol that exists but is not exported from the package index, so the consumer cannot import it
- "call it inside the memo, it is synchronous" - both halves true, but the memo's dependency array
  does not include the state, so it recomputes no more often and fixes nothing
- a move that is structurally right but drops the flex classes the element was carrying for layout
- "key the cache on interface mode", where the cache is module-level and the mode comes from a hook

Four of the five named a **real symbol that was real in the wrong place.** Reading that a symbol
exists is not checking it is reachable from where the fix lands. Rulings:

- **A claim is verified by observation; a fix is a proposal about code that does not exist yet.**
  They get separate answers. The S5 reply carries `FIX_VERDICT` / `FIX_REASON` / `FIX_REWRITE`
  alongside the claim verdict, answered per item - cluster siblings included, since a cluster shares
  a fix *by definition* and that is exactly where answering once is most tempting and least safe.
- **`validate` rejects a missing or unknown `fix_verdict`** on any item whose verdict is not
  REFUTED. Prose alone does not hold: the `PENDING` verdict shipped for the same reason, and the gap
  was closed the same way. REFUTED items are exempt because their fix text reaches no author.
- **A refuter that finds a broken fix must supply the replacement**, not merely flag it. Flagging
  leaves the orchestrator to invent one from less context than the refuter had, which is how the bad
  fix arrived.
- **"Never hedge the `Fix:` line" was amended, because it was manufacturing part of the problem.**
  It demanded confidence the refuter may not have. The rule is now: unhedged about what you
  verified, never prescribe what you did not - and where a concrete edit cannot be verified, state
  the *constraint* any fix must satisfy, which is still imperative. `validate` lints the slot for a
  tight set of hedge phrases; one of the six #2821 corrections was a hedged fix against the old
  rule, which nothing enforced.
- **The obligation table addresses re-review churn, which is a different failure.** A fix can work
  and still draw a finding next round for omitting what the repo's own rules attach to that change.
  S5 derives a trigger table from `.claude/rules/**` and the CLAUDE.md standards on every run
  (per repo - these commands run against more than one, and a stale table asserts obligations that
  no longer exist) and passes it to every refuter. Two of the six #2821 corrections were this class:
  a locale key named in one file instead of all, and the hedged fix.
- **Rejected: apply-the-fix-and-build as the gate.** It would have caught 2 of the 5 - the other
  three compile clean. And `link-deps` used to symlink the primary checkout's whole `node_modules`
  into `$WT` (now mirrored per entry; see above), which was the known cause of spurious `TS6200`/`TS2300` floods and
  `import/no-unresolved` in workspace typecheck and lint, so a repo-wide check there reports the
  setup, not the fix. Refuters are pointed at targeted checks instead: the one test file, one
  project's typecheck, the linter on one file, a `node -e` probe. Reading plus probes caught 5 of 5.
- **Rejected: a deterministic sweep of backticked symbols in `Fix:` lines.** Yield on the observed
  data is zero - every one of those symbols existed. The failure is reachability, not existence.
- **Known gap:** the hedge lint only fires when `detail` actually has a `**Fix:**` slot, so a
  finding with no fix at all passes it. `fix_verdict` is still required for such an item, which is
  the check that catches it, but the lint itself does not enforce the slot's presence.
- *If wrong:* the fix audit reports a near-zero REWRITTEN rate across several real reviews, meaning
  the stage is buying nothing; the S6 `Fix audit` line exists to make that visible rather than
  assumed. Treat a 0-of-30 the way a 0% refutation rate would be treated.

## Matching the claim, not just verifying it

Measured on paranext-core PR #2825 (a 30-item review): **eight of thirty claims did not describe the
defect their own evidence established, against two that were false.** The pipeline had three
mechanisms aimed at whether a finding is *false* - refuter verdicts, `--include-refuted`, the
refutation rate - and none aimed at whether it is *misdescribed*. The error nothing looked for was
four times more common than the one three things looked for.

- **The verdict cannot catch this, by construction.** A verdict answers "is this true", and a claim
  NARROWER than the defect passes that test: verifying "this breaks in Simple mode" never requires
  looking at Power mode. `DOWNGRADED` does not cover it either - it is about *severity*, and S5
  routes its reasoning into `Why it matters:`, not the claim. So a refuter that narrows a claim while
  verifying it has nowhere to put the correction, and every one of the eight had the narrowing
  sitting in its `REASON` prose - collected, then dropped.
- **`CLAIM_VERDICT: SOUND|REWRITTEN`, not a direction enum.** An earlier draft had
  `ACCURATE|OVERSTATED|UNDERSTATED`. Rejected: a claim can be both at once in different dimensions -
  #2825's item 5 understated *when* the throw happens (any failing definition read, not just
  teardown) while overstating *what follows* (only a pane that never reports an area) - so a
  direction enum forces a false choice on exactly the claims needing most care. Borrowing
  `FIX_VERDICT`'s vocabulary also means a refuter learns one pattern, not two. Direction stays
  recoverable from the rewrite; it is diagnostic colour, not a control.
- **No `UNVERIFIABLE` member.** A claim the refuter could not settle is already `INCONCLUSIVE` at the
  verdict level. A second way to say "I could not tell" would be used to dodge the scope question,
  which is the one this field exists to force.
- **The test is a match, not a hunt, and the mismatch must be sourced from evidence already
  gathered.** Rejected: "name one adjacent case the claim excludes." That is a production quota - an
  agent asked to find understatement will find some, and most would be "this could also bite if
  someone writes a component that ...". The bar instead: if you have to go looking for a new case to
  make the claim bigger, it is `SOUND`. That is where the real ones were - both understated claims on
  #2825 sat inside checks the refuter was already running.
  *What this deliberately gives up:* an understatement nobody tripped over during verification goes
  unnoticed. Accepted - a false "this could be broader" costs an author's attention on every review;
  a missed one costs nothing until someone hits it.
- **The S6 grep sweep is one-directional and says so.** Quantifiers (`every`, `never`, `only`) are
  greppable, so overstatement is; understatement is usually a narrowing noun phrase with no
  quantifier at all ("in the Simple-mode Comments panel"), and the words that narrow a claim are the
  same words that legitimately make it specific. A clean sweep therefore says nothing about
  understated claims - `grep-safety-net.md` rule 5 applied to this tool's own output. That half
  rests entirely on `claim_verdict`.
- **Severity revision was one-sided too** - 4 lowered, 0 raised across 30 items - and the
  `DOWNGRADED` wording actively invites lowering ("say what survives, and downgrade it") with no
  symmetric sentence. Added one, and the S6 `Claim audit` line now reports `N lowered / M raised`
  beside the claim-rewrite rate, so the ratchet is visible per run rather than invisible across many.
- **Enforced in `validate`, not prose.** `claim_verdict`/`claim_reason` are required wherever
  `fix_verdict` is - every item the payload can publish, i.e. not `REFUTED`. Prose alone does not
  hold; the `PENDING` verdict and the unchecked `Fix:` slot both shipped for that reason. Seven
  guards, each mutation-proven. One mutation (`CLAIM_VERDICTS` narrowed to `("SOUND",)`) caught a
  non-falsifiable test of my own: it iterated the constant under test, so the loop shrank with it.
  Pinned to literals. **`test_each_fix_verdict_is_accepted` has the same flaw** and is left alone
  here rather than widened into an unrelated change.
- *If wrong:* refuters rewrite claims that were already fine, and the `Claim audit` shows a high rate
  whose entries are restyling rather than correction. The instruction "correct only the mismatch you
  can evidence; do not restyle a sentence that is already accurate" is the guard; if it does not
  hold, tighten to requiring a named case the original excludes or admits wrongly.

## Cost of the pressure-test

- **One refuter per cluster, one per unclustered item** - not one per item. A refuter pays the same
  fixed cost (its own context, CLAUDE.md and the rules files, the source it reads) whether it
  answers one claim or six. Measured on the two surviving ledgers for paratext-10-studio PR #190:
  68 items across 13 distinct files, one file carrying 14 findings, and 9 clusters covering 50 of
  the 68; the second run, 53 items across 18 files with 13 clusters covering 41. So that constant
  was being paid 68 times to examine 13 files, and cluster grouping takes the agent count to 27 and
  25 respectively. *If wrong:* revert one paragraph of S5; the ledger schema does not change.
- **Grouped by cluster and by nothing else** - explicitly not by file, and not by batch size. The
  saving from batching unrelated items is only the fixed overhead, and it is bought with the two
  failure modes a shared context introduces: one verdict priming the next, and attention thinning
  down a long list. Cluster siblings share a premise, so correlated verdicts there are correlation
  that should exist. It also puts weight on S4 being honest, which is why S4 now says so.
- **No cap on cluster size.** The largest real cluster was 10. A cap would split exactly the groups
  where sharing a context pays best, and the dispatch line in the S6 report exists so the cost and
  the largest group stay visible if that turns out to be the wrong call.
- **A cluster refuter must answer each site separately, and may reach different verdicts.** The
  dominant defect this whole review exists to catch is a fix landing at one of the two sites that
  needed it. A premise holding at the first site says nothing about whether the second was changed,
  so a reply is checked against the dispatched ids and a short reply counts as unanswered, not as
  agreement.

## Process

- Every guard is mutation-proven: a test that still passes with its guard removed is treated as a
  defect. This caught three guards that had shipped under fully green runs.
- Task 5's review and the whole-branch review ran on the most capable model; the rest on a mid
  tier. Task 5 is the only one whose output publishes under a real account.
- Tasks 6 and 7 were implemented as one batch - two prompt files sharing one ledger contract, where
  consistency matters more than separate review surfaces.
- One hardening round was run past the single-fix-wave limit, because the wave's own new subcommand
  re-opened the plan's worst defect (`CLAUDE_SCRATCHPAD=/` yielding an empty path).

## Learned from PR #2724 (a 40-item review on a restacked stacked PR)

- **Placement follows anchoring, not verdict.** `payload` used to route DOWNGRADED and
  INCONCLUSIVE into the review body however cleanly they anchored, on the theory that they sat
  below an inline bar. On a 28-finding review that buried 22 of them in one wall of text, where a
  reader cannot see which findings land in the file they are already reading - the whole reason
  inline comments exist. Anchoring is a mechanical property of the diff; the verdict is a judgement
  about the finding. They are now reported separately: every anchorable finding posts inline
  carrying its status and the refuter's reasoning, and only an unanchorable one falls back to the
  body. (The `<severity_in> -> <severity>` this originally published, and the downgrade reasoning,
  were later dropped from the published surface - see "Voice of a published review".) `--body-only-verdicts` restores the old routing, opt-in.
  The old rule also had no override at all, so the operator's only way out was to hand-build
  `payload.json` and bypass the tool - a worse outcome than either behaviour.
- **`reanchor` derives `on_changed_line`; it no longer trusts S5.** Refuters got it wrong in both
  directions in one run, including one that answered `true` while its own stated evidence said the
  lines were untouched. A wrong `false` silently demotes a good finding; a wrong `true` fails the
  ENTIRE review POST. It is a property of the diff, so the diff decides and corrections are
  reported - a refuter contradicting the diff is worth weighing against its other answers.
- **Re-read the head immediately before POST, not only in step 2.** Step 2 runs before the approval
  pause; a rebase lands during it. A restack between preview and post was caught only by an ad-hoc
  check, and would otherwise have posted 26 comments against rewritten line numbers.
- **A 5xx is ambiguous, not a failure.** A 502 on the POST had already created the review. The old
  guidance ("do not retry blindly - re-run step 2") would still have double-posted every comment
  under the user's own account. Always query for a matching review before retrying anything.
- **After a head move, re-verify the claims, not just the anchors.** `reanchor` checks that a line
  still exists, never that a finding is still true. A restack regenerated a build artifact and
  reformatted a file, which fixed two items the review body asserted were still broken - one of
  them framed as a red CI step. It also removed one of the two gates that had justified downgrading
  two findings: the findings did not change, the reason for holding them low did. Corrections are
  appended as a dated `_Re-checked at <sha>_` note rather than overwriting a refuter's reasoning.
- **A stacked PR whose base was rebased inflates the scope silently.** The merge-base fell below
  the entire base stack, so both the pipeline and GitHub's Files tab showed 107 files across 163
  commits for a 45-file PR. Detect with `git merge-base --is-ancestor`, derive the fork point with
  `git cherry`. This also splits the base in two: the review base (what refuters diff against) and
  the posting base (GitHub's merge-base, which decides what can anchor). The posting base is a
  superset, so a finding can be outside the review scope and still postable - one was.
- **Hedged severities are normalized downward.** The generator emits `medium-high` / `low-medium`,
  outside the four-value vocabulary. The hyphenated form is a hedge, and a hedge should not inflate
  a severity on its way to an author.
- **Parallel refuters share one worktree.** Several spent tokens reporting each other's probe files
  as unexplained repo state; one tidying up "stray" files would have deleted another's probe
  mid-run. They are now told to use their own temp dir and never touch untracked files they did not
  create.

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
- **Preview prints no "written" line, and `preview.txt`'s path is deterministic**
  (`<payload dir>/preview.txt`) - keeps an existing no-stderr guard intact. *If wrong:* one line of
  output.
- **The oversize-page label is chunk-neutral** ("this page exceeds the page limit; shown whole"),
  since chunk 0 is the review body and "one comment" would mislabel it. *If wrong:* wording only.
- **One shared demotion predicate, in `ledger_tools`, drives both `report`'s and `payload`'s
  inline/body split.** Two copies of the routing rule had already diverged once. *If wrong:* a
  small refactor of `build_payload`'s branch.
- **`edit` may set `verdict_reason`, `fix_reason`, and `fix_verdict` - the last only to
  `UNVERIFIABLE`.** `/pr-review-post`'s re-verification step needs these three without a hand edit
  of the ledger. *If wrong:* three more editable fields.
- **`Why it matters:` is always required.** The old "unless the severity is self-evident" escape
  let the generator drop the slot on every nit, and a finding with no stated consequence reads as
  one not worth raising - including ones that were (a frozen-update throw, a stray saved space).
  Enforced in both `validate` and `validate --draft`. *If wrong:* one helper and two call sites.

## A second backend: Reviewable

Measured against paranext-core PRs #2734, #2827 and #2431 through the Reviewable MCP server. The two
backends differ in ways that read as cosmetic and are not, so everything here was tested rather
than reasoned about.

- **One plan, told a capability rather than a provider.** `plan_review` decides placement, renders
  every published word and prints the accounting; `build_payload` and `render_reviewable` only
  restate it in their API's fields. The single fact it needs about the backend is whether it
  accepts a comment on a line outside the diff (`off_diff_inline`). GitHub's output is byte-identical
  to the single-function `build_payload` it replaced: a differential oracle over 25,000 randomized
  ledgers (payload JSON, stderr, raised errors, full and paged preview) and 1,000 CLI runs under two
  console encodings (exit code, stdout, stderr, `preview.txt`) found zero differences, and twelve
  seeded mutants - each a working module - were all caught. Two later changes alter GitHub's output
  on purpose, each recorded below: a git-quoted path is decoded, and a malformed line is refused.
  Re-run against `master` with every difference required to be one of those two, 75,000 ledgers and
  2,028 CLI runs differed only by the line refusal, and 5,000 random diffs through `changed-lines`
  and `reanchor` differed only for quoted paths; 28 seeded mutants were caught, and the one that
  restores `master`'s handling of a `true` line is caught by the unit tests instead.
  *If wrong:* the GitHub renderer is ten lines over the plan.
- **Off-diff findings post inline on Reviewable.** GitHub rejects the whole review over one
  off-diff comment, which is the only reason those findings are demoted into the body. Reviewable
  has no such rule: a comment on line 2 of a file whose only hunk is at line 269 was accepted,
  auto-expanded the collapsed region, and appeared in the file view, the Discussions tab and the
  reply count - while published resolved, by an identity aliased to the reader. A dead anchor, or a
  finding with no file or line, still goes in the body on both, since no backend can place it.
  *If wrong:* build the Reviewable plan with `off_diff_inline=False`.
- **A finding in a file the pull request does not touch goes in the framing comment on Reviewable,
  decided when the payload is built.** Reviewable keys a discussion to one of the PR's own file
  entries, so such a finding has nothing to attach to, and the posting step's path lookup would
  stop on it every time - re-anchoring cannot change a file's absence from the diff. `changed-lines`
  and `reanchor` record `file_in_diff` from every file the diff gives a new side, including one
  with no added line; `plan_review` reads it. The `+++` headers alone do not name them all: a
  rename with no content change, a mode-only change and a binary change print no `+++` line, so
  each file's `diff --git` header counts too, or its `rename to` line for a path that moved, whose
  two halves cannot be split reliably when a path holds a space. The line map keeps only files
  with a `+++` header, since the others have no line to anchor to. A deleted file has no new side
  and is not in the diff. Deciding it there rather
  than at the posting step keeps the preview true: moving a finding into the framing comment after
  approval would publish a body nobody approved. A missing field counts as in the diff, so an
  older ledger plans as before and the posting step still stops on it. GitHub reads nothing of
  this: those findings are off-diff there and go in the body exactly as before.
  *If wrong:* such a finding is read in the framing comment, away from the code it is about,
  though still raised at its own disposition (see below); to post it inline instead, the posting
  step would need a file-level key Reviewable does not list.
- **An off-diff discussion says so in its first line:** `_This comment is about the code on this
  line, which this pull request does not change._` Without it the reader's two readings - the
  reviewer misread the diff, or the comment landed on the wrong line - both end with the finding
  ignored. It states the situation and nothing about how the review reached it, once per site
  rather than per finding, and it is rendered by the plan (which owns every published word), which
  is also what keeps it off GitHub: the GitHub plan has no off-diff sites. An off-diff finding never
  merges into an on-diff site on the same line, or the note would mislabel the on-diff one.
  *If wrong:* one constant.
- **Cross-references count what posts inline on the backend in use.** On Reviewable an off-diff
  site is a comment the reader can find, so the cluster and same-file lines name it; on GitHub they
  do not, as before. Neither backend names a body-bound finding from an inline comment - the
  cluster sibling most likely to be missed - because widening that changes GitHub's published text.
  `{{#n}}` tokens already resolve against everything that publishes, body included, on both.
- **Disposition follows what the RAISER asks for; a responder can meet a request but not create
  one.** Four observations, and only this model fits all of them: `blocking` stayed unresolved when
  the human marked themselves `satisfied` and cleared only once the raiser stood down too;
  `discussing` resolved on the human's `satisfied` while the raiser still held `discussing`;
  `informing` stayed resolved when a third participant published `discussing` on it; and a thread
  went unresolved the moment the raiser replied with `discussing`. So `blocker` -> `blocking`;
  `high`/`medium`/`low` -> `discussing`; `INCONCLUSIVE` -> `discussing` at any severity, since it
  asks for a judgement the reader can settle; a REFUTED finding re-admitted by `--include-refuted`
  -> `informing`, since its own header says it is probably not real; a merged site takes its
  strongest; the framing comment takes the strongest among the findings it carries, and is
  `informing` when it carries none. A human reviewer's `blocking` behaves the same, so
  this asks nothing of the pipeline a colleague's blocker would not. *If wrong:* blockers accumulate
  unresolved on PRs nobody revisits; demote `blocker` to `discussing`.
- **`informing` is right for framing and wrong for a finding, so the framing comment is raised as
  the findings it carries.** On Reviewable the body carries a dead anchor, a finding with no file or
  line, one in a file the pull request does not change, and any `--body-only-verdicts` section.
  Raised `informing`, as it once was, the comment published resolved whatever it held, so a blocker
  whose code moved before posting stopped holding the merge and asked the author for nothing -
  exactly the softening `site_disposition` exists to prevent. So the comment takes the strongest
  disposition among those findings, as a merged site does, and stays `informing` when it carries
  none. The plan records which findings the body carries (`body_findings`) for this, and the
  accounting lines say the comment publishes resolved only when nothing in it asks for a reply.
  GitHub has no disposition and is unaffected. *If wrong:* a framing comment holding a blocker holds
  the merge for everything in it, its context and every lesser finding beside the blocker, until
  the raiser stands down; if that proves too blunt, raise only blockers there and leave the rest
  `informing`.
- **The framing comment names the findings it carries, as a discussion does.** The artifact's
  `framing` has `item_ids`: the ids of the findings the body carries, empty when it carries none.
  Without them the posting step's key map and its final link report covered discussions only, so
  after a failed create it could not say whether a dead-anchor finding had landed, and the report
  had no line or link for it - a dead-anchor blocker included, which holds the merge through the
  framing comment's disposition. The preview header names the ids only when there are some,
  mirroring the discussion header. GitHub's payload is unaffected. *If wrong:* the field is never
  sent to Reviewable - it feeds only the run's key map and report - so a mistake in it misreports
  a finding's landing or link and changes nothing that publishes.
- **Reviewable gets its own accounting lines; GitHub's is untouched.** GitHub's one warning says
  off-diff and dead findings were moved into the body, which is false on Reviewable. The Reviewable
  plan prints a warning naming the findings with no location in this pull request - a dead anchor,
  no file or line, or a file the pull request does not touch - a `--body-only-verdicts` notice that
  names the review-level comment rather than the review body, and a `notice:` naming the off-diff
  ids that post inline.
- **The artifact carries path and line, never a file key or revision.** Both are opaque ids only a
  live call returns, and this module makes no network calls so that a payload can be built and
  approved offline. The posting step resolves them after approval and checks them - the matched
  file entry must carry back exactly the path searched for, and the newest revision's
  `headCommitSha` must equal `anchored_sha`, alongside GitHub's own head - because a wrong key posts
  to the wrong file and a stale revision attaches to superseded code, and neither fails loudly. A
  path with no entry is fatal. *If wrong:* nothing silent - the posting step stops.
- **A line that is not a positive integer is refused when the payload is built, on both
  backends.** On Reviewable, discussions are created one call at a time, so a create rejected
  halfway leaves a partial review in drafts; on GitHub such a line was sent as it was, or two lines
  of different types in one file ended `payload` in a traceback - on `master` too. The plan checks
  every site's line before sorting its sites, which cannot order a string line against an integer
  one, so the refusal is an `error:` line and never a traceback. This deliberately changes GitHub's
  output for a malformed ledger, and only for one: no pipeline step writes such a line.
- **Drafts are identity-scoped in both directions, so the terminal preview stays the approval
  gate.** The agent publishes as a distinct Reviewable identity; its drafts are invisible to the
  human in the file view and the Discussions tab, and the human's return nothing to the agent. The
  preview is the only surface both can read, which is why it shows each discussion's disposition -
  the part of the commitment the text does not carry.
- **Publishing is all-or-nothing per identity, and identities are not sessions.** There is no
  publish-one call, and concurrent sessions share one identity, so a pending draft may be an
  abandoned run's or a live session's - nothing in a draft says which. Pre-existing drafts are
  reported and deleted only on the operator's word, never swept. Reconciling before publishing is
  two checks, not one: `+draft` names comment drafts by key, while `review_state.drafts` also counts
  acknowledgements, dismissals, disposition changes and file marks, all of which publish and none of
  which this review created. After an R5 stop, this run's recorded drafts are reported and left in
  place until the operator chooses; only recorded keys are ever deleted.
- **Any draft still listed after the operator answers is a hard stop before anything is created.**
  A kept comment draft always trips the reconciliation, but only after the run has created its
  whole review as drafts; a kept `-top` is worse, since `-top` is the identity's single top-level
  draft (the delete tool's own description), the framing comment needs it, and the reconciliation
  cannot tell two `-top` drafts apart by key. So R3 lists again after the answer and creates nothing
  until the listing is empty; R5 stays, for drafts that appear after R3.
  *If wrong:* an operator who deliberately staged a draft must clear or publish it before every
  posting run.
- **A failed create is checked, not assumed.** An error Reviewable returned means the draft does
  not exist, but a timeout or a lost response may be a draft whose key never came back, and the
  create call is not idempotent. So after any failed create the run lists `+draft` and adopts an
  unrecorded draft whose path, line and body match the failed call exactly, rather than retrying
  it. Since R3 leaves the listing empty, every unrecorded key is either that or someone else's.
  Publishing after a partial failure would still send half a review as the whole one, so it never
  publishes then. *If wrong:* a duplicate the listing missed is caught as an extra by R5 and
  reported as this run's likely duplicate; nothing publishes until the operator settles it.
- **A lagging Reviewable is reported, not polled.** R1 re-lists the revisions once, straight away,
  and then stops if the newest still is not the anchored head - "One watchdog, no other waiting"
  applies here too. *If wrong:* the operator re-runs the post step by hand after a short delay.
- **Links come from the server, one read settling all of them.** The discussion list returns a key
  and a `reviewable://` URI but no browser address. Every discussion URL observed is
  `<review url>#<key>`, so one resource read whose `url` ends in `#` plus exactly the key asked for
  yields the review URL for the rest; any mismatch falls back to one read per key for the whole
  run, since a wrong link shape would mis-link every finding at once. A hostname is never
  constructed: Reviewable runs on enterprise installs, and a fabricated link fails in a browser
  rather than here. `review_publish` returns the review state, so the confirmation reads it there.
- **Provider selection is a ladder with an explicit unreachable state, kept per repo.** Arguments,
  then this conversation's earlier choice for the same repo, then `providers.json` keyed by
  `owner/repo`, then GitHub for a private repo, then, for a public one, the server's own
  `choose-review-provider` resource. Every rung is per repo, the conversation's included:
  Reviewable can cover one repo and not the next (its free tier covers public repos only), and a
  choice carried across repos would send a private repo's review to a backend that cannot see it,
  where the failure reads as an unreachable server rather than the wrong backend. A connection
  failure is not a detection result, and the test is the Claude Code MCP configuration:
  a `reviewable` server that is configured but failed to connect (a `CONNECT_TIMEOUT` at session
  start, say) stops and asks the operator to reconnect, as does a saved `reviewable` default with
  the server down; only a configuration with no `reviewable` server selects GitHub, for that run
  alone; and when the two cannot be told apart it counts as unreachable - stopping costs a delay,
  guessing costs a review published in the wrong place. A differing saved default is never
  overwritten unasked.
- **A private repo uses GitHub, as a policy rung.** The team reviews its private repos on GitHub
  whatever Reviewable plan covers them: Reviewable's free tier covers only public repos, and the
  team works with private repos through GitHub anyway. So once no argument, earlier choice or saved
  default answers, a repo whose `gh repo view` visibility is anything but `PUBLIC` posts to GitHub,
  connected server or not; it is a rule about the repo, not a fallback from a failed connection.
  With no saved default the operator is asked once whether to record `github` for the repo. A
  visibility `gh` cannot report is not assumed private: the repo goes on to detection and the
  unreachable rules, as a public one does, so a failed lookup never routes a review by itself.
  *If wrong:* a private repo the team does want on Reviewable needs an explicit `reviewable`
  argument and a yes when asked to record it; after that its saved default answers first.
- **A provider is recorded only when detection returned it or the operator agreed.** With no
  saved default, a provider the `choose-review-provider` resource returned is recorded silently.
  One named for a single run, or chosen by the private-repo rung, is recorded only if the operator
  says yes when asked. GitHub chosen because no Reviewable server is configured is used for that
  run and not recorded: nothing was detected, and a recorded `github` would keep the repo off
  Reviewable, with no prompt, after the server is connected. Every write merges into the file and
  sets only this repo's key. A file that is not a JSON object of `github`/`reviewable` values stops
  the run rather than going on down the ladder, which would choose a backend while the recorded
  choice is unreadable. Every entry is checked, not only this repo's, and the `error:` line names
  the entry at fault: each write carries the other entries forward unchanged, so a bad one left
  alone would surface later, on a run for the repo it belongs to. The cost is that one repo's run
  stops on another repo's bad entry until someone fixes or deletes it.
  *If wrong:* one extra question on a repo's first explicit run, against a silent default that
  would send later reviews to the wrong place; and a repo with no Reviewable server reads its
  visibility and the configuration again on every run until a default is recorded.
- **`providers.json` is replaced, never rewritten in place, and not locked.** The writer writes a
  temporary file in the same directory and renames it over the original, as `write_ledger` does,
  so an interrupted write leaves the previous file whole and a reader never sees a half-written
  one; a truncated file would stop every repo's run until repaired. Two sessions recording
  defaults for different repos at the same moment can still lose one entry: each reads the file,
  sets its key and replaces it, and the later replace carries the copy it read. That is accepted:
  the window is milliseconds, and the loss heals itself - the dropped repo is detected or asked
  about again on its next run, and never posts to the wrong place. A lock with stale-lock handling
  for a killed run would add more prompt code than that risk warrants. *If wrong:* an operator
  running many first-time posts in parallel answers a repo's question twice; add the lock then.
- **Paths git quotes are decoded wherever the diff is read.** Git writes a path with a non-ASCII
  byte (under the default `core.quotePath`), a control character, a quote or a backslash as a
  C-quoted string - `+++ "b/caf\303\251.ts"` - in its `+++`, `diff --git` and `rename to` lines.
  Read verbatim, that never matched the plain path a ledger records, so such a finding was judged
  off the diff: moved into GitHub's review body as not on a changed line, or on Reviewable into the
  framing comment as in a file the pull request does not change. Decoding it, with the standard
  library, deliberately changes GitHub's output for those files and no others: their findings now
  post inline. *If wrong:* the decode is one function, `git_path`.
- **A hunk's body is read by its header's counts, so a `+++ ` line inside it is content.** An
  added line whose text starts `++ ` appears in the diff as `+++ ...`, and was read as the next
  file's header: the rest of the real file's added lines were lost, and a file that does not exist
  was recorded as part of the pull request. The diff reader now takes both counts from each
  `@@ -a[,b] +c[,d] @@` header (an omitted count is 1), spends them line by line (a context line
  both, `-` the old, `+` the new, a `\ No newline at end of file` line neither), and reads `+++ `
  as a header only once both are spent. `diff --git` and `@@` are not gated, since no body line
  can start with either, so a hunk whose counts overstate its body still ends at the next file.
  This deliberately changes GitHub's output, for diffs holding such a line only: a finding on one
  of the recovered lines now posts inline. It is one of four such changes, beside the quoted-path
  decode above, the positive-integer line check under "A line that is not a positive integer" and
  the line splitting in the next entry; GitHub's output matches `master` everywhere else, and the
  golden file is unchanged.
  *If wrong:* a diff whose header counts are wrong reads `+++ ` lines as content until the next
  `diff --git`; the counting is confined to `_parse_diff`.
- **Lines are numbered as git numbers them, at `\n` alone, everywhere.** Git, GitHub and the
  review agents' tools end a line at `\n` only. The diff reader, the worktree reader behind
  `excerpt` and the excerpt checks, and `reanchor`'s matching all split with `str.splitlines`,
  which also breaks at `\r`, `\v`, `\f`, `\x1c`-`\x1e`, U+0085, U+2028 and U+2029, and opened
  their files with universal newlines, which turn a bare `\r` into a line end. A source line
  holding any of these became two: every later line was numbered one too high, a correct excerpt
  read as not verbatim, a diff hunk's counts were spent early, and a tail reading `+++ ...` was
  taken for a file header naming a file that does not exist. All of them now go through
  `split_lines` and `read_text_lines` in `ledger_tools`, which open with `newline=""`, split on
  `\n` alone and drop a trailing `\r`, so a CRLF file or diff reads as before. This deliberately
  changes GitHub's output, for files and diffs holding such a character only: a finding after one
  is judged on its own line. *If wrong:* a tool that rewrites line ends to bare `\r` yields one
  unreadable line; git and `gh pr diff` never do, and the splitting is confined to `split_lines`.
- **Reviewable discussions are GitHub review comments** (keyed `gh-<comment id>`), and an agent's
  comment mirrors to GitHub authored by the human with a note naming the agent, so attribution and
  the S2 read of prior feedback are unchanged. GitHub does not carry Reviewable's resolution state;
  a re-review that wants to skip settled points must read it from Reviewable.
- **Known gaps.** `report`'s "N inline + M in the body" line counts GitHub's placement, since the
  backend is chosen later, in `/pr-review-post`, and labels it so (`on GitHub:`); on Reviewable,
  off-diff findings in files the diff touches move from the second number to the first. The key
  `review_discussions_create` returns cannot be confirmed by a read-only call, so the prompt stops
  rather than guesses if a create response names none.
