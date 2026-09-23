# Ledger tooling and refuter contract (pipeline v2, sub-project A)

Status: approved design, 2026-09-22. Implementation plan to follow.

## Background

An audit of eight `/pr-review` runs between 2026-09-18 and 2026-09-22 (scripture-editors #13 x2,
#14; paranext-core #2825 x2, #2838, #2653, #2844) found that most defects reaching the approval
gate came from mechanical work the orchestrator redid by hand every run:

- Seven of eight runs wrote throwaway scripts (`changed_lines.py`, `build_ledger.py`,
  `items_a..d.py`, `merge.py`, `renumber.py`, `regen_excerpts.py`) to assemble the ledger.
- Hand-typed `anchor_excerpt` values (paraphrased, or containing `...`) made `reanchor` report
  20/20 and 16/22 items dead at post time.
- Homemade `on_changed_line` logic disagreed with `reanchor`'s first-line rule and demoted high
  findings to the body after the S6 report had promised them inline.
- 25 of 42 `FIX_REWRITE`s in one run were never applied to `detail`; `severity_in` was overwritten
  in another.
- Refuter replies arrived as prose or "the block is above" in five runs; only the hand-back
  message reaches the orchestrator.
- Refuters sharing one worktree clobbered each other's mutation tests, and ran tests in the user's
  primary checkout, dirtying it.
- Process text ("Note for the refuter:", "item #8 in this batch", "#4's rewrite") and stale `#n`
  cross-references posted to authors.
- 31-82 KB previews were truncated by the harness, so the user approved text they had not seen.
- Waiting was done with `sleep`, `echo`, and `ScheduleWakeup` - one wakeup replayed the whole
  command three times.

Sub-projects B (re-review mode) and C (companion-PR support) are separate specs and depend on this
one.

## Goal and success criteria

Reviews post correct, author-facing findings without the user catching leaks, stale anchors or
mis-scoped claims at approval time, and the orchestrator stops re-implementing mechanical steps.

The next live `/pr-review` run is the acceptance test. It succeeds when:

1. No ad-hoc ledger, diff, or renumbering script appears in the transcript.
2. `reanchor` at post time changes nothing that S6's `reanchor --dry-run` did not already report.
3. No refuter is re-dispatched for reply format.
4. No process vocabulary or stale cross-reference reaches the preview.
5. The user reads the whole preview in the terminal before approving.

## Non-goals

- Re-review mode, prior-thread classification, id continuation across rounds (sub-project B).
- `--related` companion PRs, cross-PR dedupe, merge-order dependency rule (sub-project C).
- Changing the generator (`code-review max`) or its run time.

## Rulings

| # | Question | Ruling |
| --- | --- | --- |
| R1 | How the user sees the preview | `preview --page N`, relayed page by page as assistant message text; one approval at the end |
| R2 | Cheaper-model refuters | Dropped. Every refuter runs on Sonnet |
| R3 | Cross-references between findings | Symbolic `{{#id}}` tokens, resolved by `renumber` and rendered by `payload`; bare references rejected |
| R4 | Refuter mutation isolation | Shared `$WT` is read-only; a refuter that mutates creates its own `wt-pr<n>-r<key>` on demand |
| R5 | Stale `-r` worktrees | Disposable by name: the reaper skips the dirty guard for them; current-session and 24h floors still apply |
| R6 | Dispatch granularity | One refuter per cluster or unclustered item, unchanged, now enforced by `ingest`; at most 15 concurrent |
| R7 | Orchestrator activity while waiting | Preparation only; no independent review, no polling. One exception: a refuter watchdog |
| R8 | Structure | Deterministic subcommands; `pr-review-anchor.py` stays the sole CLI, new logic in `ledger_tools.py` |

## Architecture

`pr-review-anchor.py` remains the only Python entry point the prompts call, so command paths do not
change. New logic lives in a sibling module, `ledger_tools.py`, imported by it (the file is already
822 lines). Tests load both via `importlib` as they load `anchor` today. `pr-review-wt.sh` gains
`remove-refuters` and the reaper exception.

Run shape (tool steps marked ⚙):

```
S0  setup, reap, resolve target, fetch, worktree
    primary-checkout status snapshot ⚙
    link-deps ⚙ (moved here from S5; pnpm/yarn-aware)
    discover the repo's real test/typecheck/lint commands
S1  launch generator (background); while waiting, preparation only:
      S3 comment fetch, obligation table, posting diff ⚙, brief context file (ctx.json)
S2  split -> items with stable ids 1..N (not renumbered again until S6)
S4  cluster
S5  brief ⚙ per cluster / unclustered item; dispatch <= 15 at a time, refill; arm watchdog
      each refuter writes verdicts/<key>.txt and hands back only the path
      remove-refuters ⚙
S6  ingest ⚙ -> excerpt ⚙ -> changed-lines ⚙ -> (fold notes, quantifier sweep: judgement)
    -> renumber ⚙ -> validate --worktree ⚙ -> reanchor --dry-run ⚙
    -> primary-checkout status compare ⚙ -> report ⚙
```

The model's remaining judgement work: splitting, clustering, building the obligation table, folding
downgrade reasons into `Why it matters:` (recorded as `fold_note`), and the quantifier sweep.
Anything that moves text between files is a tool.

`/pr-review-post` keeps its shape, with: `reanchor --head "$NOW"` (records `anchored_sha`; step 4
compares against it instead of `head_sha`), `preview --page N` relayed in messages, and teardown
that also removes any leftover `wt-pr<n>-r*`.

## Refuter contract

### Brief template

`refuter-brief.tmpl.md` ships in the repo and carries, verbatim: the three jobs (verdict, claim
match, fix), the verdict definitions, the finding-writing contract, the worktree rules, and the
reply format. Its placeholders:

`{{WT}}`, `{{HEAD_SHA}}`, `{{MERGE_BASE}}`, `{{LINK_DEPS}}`, `{{COMMANDS}}`, `{{OBLIGATIONS}}`,
`{{CLAUDE_MD_PATHS}}` (a list of paths under `$WT`), `{{ITEMS}}` (JSON), `{{VERDICT_PATH}}`,
`{{KEY}}`, `{{OWNS}}`, `{{BRIEF_HASH}}`, `{{RUN_ID}}`. Placeholders are filled in one pass, so text
inside a value (an item's claim, the obligation table) is never itself substituted.

Briefs are rendered by a subcommand, not by hand:
`brief <ledger> --key KEY --context ctx.json` fills every placeholder - `WT`, SHAs and `ITEMS` from
the ledger; `LINK_DEPS`, `COMMANDS`, `OBLIGATIONS`, `CLAUDE_MD_PATHS` and the scratch path from a
small context file the orchestrator writes once - and writes `briefs/<key>.md` plus
`briefs/<key>.json` (`key`, `owns`, `hash`, `run`). `KEY` must be a cluster id or `i<id>` of an
unclustered item, so batching unrelated items cannot be expressed. `brief` also rejects an
obligation-table row whose last cell does not cite a repo file, and any placeholder left unfilled.

The template hash (first 12 hex digits of its sha256) is written into the brief; the refuter copies
it into its verdict file header. `ingest` rejects a verdict file whose header lacks the current
hash, and takes the dispatched set from `briefs/*.json`.

The run id (first 12 hex digits of the sha256 of `head_sha` plus every item's id and claim, in
ledger order) ties a brief to one draft. `brief` writes it into the brief and its meta; the refuter
copies it into the `RUN` header line. `ingest` rejects a meta or reply whose run id is not the
ledger's, so a brief or verdict left from an earlier run in the same session is never merged. S4
also clears `briefs/`, `verdicts/` and `edits/` before writing the draft.

### Verdict file

Path: `$SCRATCH/pr-review/pr<n>/verdicts/<key>.txt`, where `<key>` is the cluster id (`C3`) or
`i<id>` for an unclustered item. Header lines:

```
BRIEF: <template hash>
RUN: <run id>
KEY: C3
OWNS: 4, 9, 17
```

followed by one block per owned id in the existing format (`ITEM`, `VERDICT`, `SEVERITY`,
`ON_CHANGED_LINE`, `REASON`, `CLAIM_VERDICT`, `CLAIM_REASON`, `CLAIM_REWRITE`, `FIX_VERDICT`,
`FIX_REASON`, `FIX_REWRITE`). Multi-line values continue until the next recognized field label.
The template shows the format unindented in a fenced block; the parser also accepts indented
labels and one fence wrapping the whole reply. The hand-back message is the path and nothing else.

### Worktree rules (in the template)

- `$WT` is read-only: no tracked edits, no new files.
- Probe files go under `$SCRATCH/probes/<key>/`.
- A refuter needing a mutation test or an in-package probe runs
  `pr-review-wt.sh create "$SCRATCH/wt-pr<n>-r<key>" <HEAD_SHA>` and `link-deps`, works there,
  and runs `pr-review-wt.sh remove` on it before handing back.
- Never cd into, build in, or test in any path outside `$SCRATCH`.

### Verdict semantics (enforced by `validate`)

- `DOWNGRADED` requires `severity < severity_in`. A narrowed scope at the same severity is
  `CONFIRMED` with `CLAIM_VERDICT: REWRITTEN`.
- `REFUTED` carries no `CLAIM_REWRITE` or `FIX_REWRITE`.
- A `Fix:` that asks for no change or defers ("no code change", "not required for this PR",
  "if ... then downgrade", "should be closed") is a hedge: the finding is `REFUTED` or gets a
  constraint.
- Every obligation-table row cites a repo file. A personal preference (e.g. spelling) is not an
  obligation and is not presented to the author as a project rule.

### Model and dispatch

Refuters always run on Sonnet (R2). At most 15 run concurrently; the rest queue and launch as
notifications arrive. Task-notifications whose result says the report was delivered as a message
are ignored.

### Watchdog (R7 exception)

After S5 dispatch the orchestrator arms one `ScheduleWakeup` of ~1200 s whose prompt is
`pr-review watchdog: check refuter progress` - never the command text. On wake it stats (does not
read) each outstanding refuter's output file. One unchanged for 15 minutes is stopped with
`TaskStop` and re-dispatched once; a second stall is closed with `ingest --inconclusive` and an
author-facing reason ("Not verified: the automated check did not complete."). It re-arms until
every verdict file exists, then stops the wakeup; a refuter re-dispatched after an `ingest` error
re-arms it. The generator has no
watchdog. No other `ScheduleWakeup`, `/loop`, `sleep`, or placeholder tool call is permitted
anywhere in either command.

## Ledger schema 2

`validate` accepts only `"schema": 2`; a schema-1 ledger fails with `schema 1 - re-run /pr-review`.

New item fields:

| Field | Written by | Purpose |
| --- | --- | --- |
| `claim_original` | `ingest` | generator's claim before any `CLAIM_REWRITE` |
| `fix_original` | `ingest` | generator's `Fix:` text before any `FIX_REWRITE` |
| `refuter_key` | `ingest` | which verdict file answered this item |
| `old_id` | `renumber` | the stable S2 id |
| `fold_note` | orchestrator via `edit` | for DOWNGRADED: `folded` or `bookkeeping`, per S5 |
| `unanswered` | `ingest --inconclusive` | the refuter never answered; see validate exemption |

New ledger-root field: `anchored_sha`, written by `reanchor --head` - the head the anchors were
resolved against.

Invariants: `anchor_excerpt` begins at `line`; when `on_changed_line` is true, `line` is a `+`
line in the posting diff. Existing fields are unchanged.

## Subcommands

All ledger-writing subcommands write atomically (temp file + rename) and leave
`ledger.json.bak-<subcommand>` behind. All fail with `error: ...` on stderr and exit 1.

**The draft ledger.** After S2/S4 the orchestrator writes `ledger.json` with schema 2, target,
SHAs, worktree, clusters, and each item's generator fields (`id`, `severity_in`, `severity`,
`cluster`, `file`, `line`, `line_end`, `claim`, `detail`, `evidence`, `origin`, `dedupe`), plus an
optional `refuter_note` per item. `refuter_note` is the only place orchestrator guidance to a
refuter may go: it is rendered into `{{ITEMS}}` and deleted by `ingest`, and `payload` never emits
it. Verdict fields are absent until `ingest`. This is the last point at which the orchestrator
writes ledger JSON directly, and it does so until `excerpt` and `validate --draft` pass on the
draft: a bad `file`/`line`, a non-integer or duplicate id, an unknown `severity_in`, a missing
`head_sha`/`merge_base`/`worktree`, or a `cluster` and `item_ids` that disagree is fixed here,
before any refuter runs, while a direct write is still allowed.

- **`edit <ledger> <id> --field claim|detail|fold_note|verdict_reason|fix_reason|fix_verdict
  --text-file FILE`** - replaces one field from a file (no shell quoting). Refuses any other
  field. `fix_verdict` accepts only `UNVERIFIABLE` - a stale fix can be demoted, never promoted.
  Used for the judgement steps (folding downgrade reasons into `Why it matters:`, quantifier-sweep
  rewording, lint fixes) and for `/pr-review-post`'s post-rebase re-verification, where
  `verdict_reason`/`fix_reason` are replaced with the original text plus a re-check line:
  `Re-checked at <sha>: ...` in `fix_reason`, which never posts, and an author-facing
  `Re-checked against the latest push: ...` (no SHA, no markdown) in `verdict_reason`, which posts
  inside the italic footer. `validate` runs again after these edits, before `payload`.
- **`ingest <ledger> [--inconclusive IDS --reason TEXT]`** - merges verdict files into items,
  reading `briefs/` and `verdicts/` from the ledger's own directory. Saves
  `claim_original`/`fix_original`, then applies `CLAIM_REWRITE` into `claim` and `FIX_REWRITE`
  into the `Fix:` slot of `detail`. Never writes `severity_in`. Fails on: a dispatched id with no
  answer, an id answered twice (in one file or across files), a brief meta whose key is not a
  refuter unit of the ledger or whose `owns` differs from that unit, a meta or reply from another
  draft (run id), an `OWNS` header that differs from the meta, a missing required field, a rewrite
  in the wrong slot, a stale or missing `BRIEF` hash. `--inconclusive` records INCONCLUSIVE with
  the given reason for ids whose refuter failed twice - the only sanctioned way to close an
  unanswered item. A unit whose ids are all named is not read at all, and a block error for a
  named id is dropped, so a reply that exists but cannot be parsed still closes. The reason
  publishes on an INCONCLUSIVE item, so it is author-facing.
- **`excerpt <ledger> --worktree WT`** - sets each `anchor_excerpt` to lines `line..line_end` read
  from the file in `WT`.
- **`changed-lines <ledger> --diff FILE --worktree WT`** - sets `on_changed_line` from
  `parse_changed_lines`, using the same rule as `reanchor`. For a range whose first line is not
  changed but a later line in `[line, line_end]` is, moves `line` to the first changed line and
  re-cuts the excerpt. Prints every move and every flip.
- **`renumber <ledger>`** - sorts by severity then file, assigns 1..N, records `old_id`, rewrites
  cluster `item_ids` and every `{{#id}}` token in `claim`/`detail`/`verdict_reason`, prints the
  old -> new map.
  Fails on a `{{#id}}` naming an unknown id.
- **`report <ledger>`** - writes `report.md` and prints the terminal report, both generated from
  the ledger alone (it reads `on_changed_line`/`anchor_status`, which `changed-lines` already
  wrote, rather than a diff): triage table with a verdict column and `severity_in -> severity`
  arrows; verdict summary with a calibration warning when REFUTED is 0 or raised is 0; claim audit
  (original -> rewrite); fix audit; downgraded (with `fold_note`); unresolved; refuted; previously
  reported; dispatch line (refuters / items / largest cluster); files; ready-to-run post commands
  with real counts, stating that placement follows anchoring. The post line's
  "N inline + M in the body" split is computed with the same demotion predicate `payload` uses
  (`demotion_reason`), so it is the off-diff count; there is no separate one.

## `validate` additions

- `--worktree WT`: each `anchor_excerpt` must match the file verbatim at `line`; `...` and `…`
  lines are rejected outright.
- Process-vocabulary lint on `claim` and `detail`, with code spans stripped first: `refuter`,
  `pressure-test`, `Note for the`, `this batch`, `\bitems? #\d+`, and any `#\d+` outside a
  `{{#...}}` token. The same lint applies to `verdict_reason` on CONFIRMED and INCONCLUSIVE items,
  where it publishes as the footer, except a bare `#\d+` (reasons cite PRs and issues that way).
  `payload` renders `{{#id}}` tokens in that footer too.
- `--draft`: the checks a draft ledger must pass before any brief - integer, unique ids;
  `severity_in` in the vocabulary; `head_sha`, `merge_base` and `worktree` present; and every
  item's `cluster` agreeing with the clusters' `item_ids` in both directions. (`rewrite` and `downgrad` were dropped after checking the audited ledgers: they
  occur in legitimate author-facing fixes - "rewrite the `@returns`", "the never-downgrade rule".)
- Extended `HEDGE_PATTERNS`: `no code change`, `not required for/in this PR|change`,
  `should be downgraded|closed|dropped`, `if ... then downgrade|close|drop`. (A bare `optional` or
  `not required` was dropped: both occur in type-level prose.)
- An anchor-excerpt line consisting only of an ellipsis (`...`, `…`, `// ...`) is rejected without
  `--worktree`; a spread operator (`...args`) is not an ellipsis line.
- `DOWNGRADED` requires `fold_note` of `folded` or `bookkeeping`.
- An item with `unanswered: true` (written only by `ingest --inconclusive`) and verdict
  `INCONCLUSIVE` is exempt from `claim_verdict`; it carries `fix_verdict: UNVERIFIABLE`.
- Verdict-semantics rules above.
- `claim` is a single sentence of at most 40 words.
- When `claim_verdict` / `fix_verdict` is `REWRITTEN`, `claim_original` / `fix_original` must be
  present and differ from the current text (proves the rewrite was applied).

## Changes to existing subcommands

- **`reanchor`** - new status `off-diff` (excerpt found, but not on a changed line) distinct from
  `dead` (excerpt gone). An excerpt mismatch no longer flips `on_changed_line` or emits the
  "corrected on_changed_line" notice. Range items are handled as in `changed-lines`.
  `--head SHA` records `anchored_sha`; `--dry-run` reports without writing.
- **`payload`** - renders `{{#id}}` as `#n`; substitutes `{count}` in the body file with the number
  of findings posted; labels demotions `off-diff` or `dead anchor` accordingly; emits the
  "carried in body by verdict" notice only under `--body-only-verdicts`.
- **`--select`** - accepts `a-b` ranges alongside ids, clusters, commas and `all`.
- **`preview`** - always writes `preview.txt` beside the payload; `--page N` prints page N of M,
  each at most 15 KB and split only on comment boundaries, headed `page N/M`.

## `pr-review-wt.sh` changes

- `link-deps`: lockfile guard covers `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`,
  `bun.lockb`; links each tracked package directory's `node_modules` that exists in the primary
  checkout, not only the root.
- Refuter worktrees are named `wt-pr<n>-r<key>` with the key lowercased (`wt-pr13-rc3`,
  `wt-pr13-ri12`).
- `remove-refuters <scratch> <n>`: removes every `<scratch>/wt-pr<n>-r[ci][0-9]*` unconditionally.
- `primary-status <wt>`: prints `git status --porcelain` of the primary checkout behind `<wt>`, for
  the S0 snapshot and S6 comparison.
- Reaper: a path matching `*/scratchpad/wt-pr*-r[ci][0-9]*` skips the dirty guard (R5). Current-session
  exclusion and the 24h age floor still apply to it; every other review path keeps the dirty guard.

## Prompt changes

`pr-review.md` and `pr-review-post.md` are rewritten around the tools; prose that explained how to
do a now-tooled step by hand is removed. Additionally:

- Remove the stale "DOWNGRADED/INCONCLUSIVE carried in the body" wording (`pr-review.md` S5 note
  and S6 "To post"; `pr-review-post.md` step 3 notice).
- Rebase check: flag only when `git cherry "$BASE_TIP" "$HEAD_SHA"` shows `-` lines; state that the
  base advancing is the normal case.
- Quantifier sweep regex uses `(?<![\w-])...(?![\w-])` so `read-only` does not match; hits and
  their disposition are written to `quantifiers.md` beside the ledger and relayed after the report.
- Worktree snapshot delta: additions only, under `$SCRATCH`, via `comm -13` on sorted paths; the
  "before" snapshot is taken after `create`.
- Waiting rule (R7) and watchdog, stated in S1 and S5.
- Remove the cheaper-model section and its report line (R2).
- Preview: remove "the tool output itself is shown in the transcript"; relay `preview --page`
  output page by page as message text, then ask once for approval.
- All scratch files go under `$SCRATCH`, never `/tmp`.
- American English throughout both prompts and the template.
- README: note that `~/.claude/commands/*.md` are symlinks; edit `commands/` in this repo.

## Error handling

| Failure | Tool behavior | Orchestrator response |
| --- | --- | --- |
| Verdict file malformed / id missing | `ingest` names key and field | re-dispatch once with the error; then `ingest --inconclusive` |
| Refuter stalls | watchdog detects 15 min without growth | `TaskStop`, re-dispatch once; then `ingest --inconclusive` with an author-facing reason |
| Concurrency limit | Agent tool errors | queue; wait for a notification; never retry in a loop |
| Excerpt not verbatim | `validate --worktree` | re-run `excerpt`; never retype |
| Process-vocabulary / hedge lint | `validate` names id and phrase | reword that text; never delete the finding to pass |
| Primary checkout changed | S6 status compare | report the files and stop before `report`; no restore without user approval |
| Leftover `-r` worktree | `remove-refuters`, post teardown, reaper exception | none |
| Head moved after approval | step 4 compares `anchored_sha` | return to step 2; never hand-edit `head_sha` |
| Schema-1 ledger at post time | `validate` fails | stop; re-run `/pr-review` |

After the draft ledger is written, no ledger field is edited except through a subcommand, and
none is ever edited to clear a tool error.

## Testing

Every new guard is mutation-proven: a test that still passes with its guard removed is a defect.

- **New tests** in `test_pr_review_anchor.py` (or a new `test_ledger_tools.py` for the new module),
  built on the existing `item()` helper updated for schema 2:
  - `ingest`: parse matrix - good, missing field, missing id, duplicate id, multi-unit key,
    rewrite in wrong slot, stale brief hash, multi-line values, `--inconclusive`.
  - `renumber`: token round-trip, cluster `item_ids`, unknown-token failure, `old_id`.
  - `excerpt` / `changed-lines`: against a temporary git repo and a real `git diff`, including the
    range-moves-to-changed-line case.
  - `report`: each section present; calibration warnings fire at 0 refuted / 0 raised.
  - `preview --page`: comment-boundary splits, no page over 15 KB, `preview.txt` written.
  - `validate`: reject and accept case per rule; `read-only`, CSS `#fff`, and code spans must not
    trip the lints.
  - `--select` ranges.
  - `edit`: refuses non-text fields; preserves apostrophes and backticks byte-for-byte.
  - `ingest` drops `refuter_note`; `payload` never emits it even if present.
- **Real-data regression**: any surviving ledgers from the eight audited runs are copied to
  `tests/fixtures/`. `validate` must flag exactly the audited defects (`...` excerpts, "Note for
  the refuter", same-severity DOWNGRADED, REFUTED with a claim rewrite). Where no ledger survives,
  a synthetic one reproduces each defect.
- **`pr-review-wt.test.sh`**: `remove-refuters`; reaper removes a dirty `wt-pr5-r3` older than 24h,
  keeps a dirty `wt-pr5` at any age, keeps a current-session `wt-pr5-r3`; `link-deps` declines on a
  changed `pnpm-lock.yaml`.
- **Audit of existing tests.** The refactor retires behaviors that current tests pin. Every
  existing test in `test_pr_review_anchor.py` (147 tests, 21 classes) and `pr-review-wt.test.sh`
  is classified as one of:
  - *keep* - its guard is still in force;
  - *rewrite* - the behavior changed and the test must pin the new behavior (expected: off-diff vs
    dead labels in `TestReanchorFixesOnChangedLine` / `TestDemotedFindings`, the corrected-notice
    text, schema-1 fixtures, the body-by-verdict notice outside `--body-only-verdicts`);
  - *delete* - the behavior no longer exists, so the test guards nothing (candidates: anything
    asserting the cheaper-model report line or schema-1 acceptance);
  - *fix* - the test is flawed regardless (`test_each_fix_verdict_is_accepted` loops over the
    constant it guards; it must list verdicts literally).
  The classification is recorded in the implementation plan, and each deletion is named with its
  reason in the commit message.

## Rollout

- Branch `ledger-tooling-v2` in `~/.claude/pr-review`; commits at milestones: tooling, wt script,
  prompts + template, DECISIONS.
- DECISIONS.md entries: schema 2; R2 (cheaper-model allowance dropped); R5 (reaper exception);
  R7 (waiting rule and watchdog); verdict-semantics tightening; the "v1 covers fresh reviews only"
  line updated to point at sub-project B.
- Memory updates: paranext-core `feedback_show_user_output_not_tool_output.md` -> `preview --page`
  relayed in messages; `project_pr_review_anchor_excerpt_verbatim.md` -> the `excerpt` subcommand.
- Acceptance: the next live `/pr-review` run, judged against the success criteria above.
