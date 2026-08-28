---
description: Deep PR review - generate, split, dedupe, cluster, and pressure-test findings into a triage table
argument-hint: <PR-url | PR-number | .> [extra instructions]
---

Run the review pipeline for: $ARGUMENTS

Work through these stages in order. Do not skip ahead to posting - this command stops at the
triage table.

## S0. Setup

1. Run `~/.claude/pr-review/pr-review-wt.sh reap` and report any `reaped`, `orphaned`, or
   `stranded-branch` lines. A `kept ... dirty-N-changes` line is normal, not an error: the reaper
   never removes a worktree with uncommitted or untracked changes, and never deletes branches -
   it only reports them. `kept ... current-session` / `kept ... locked-by-live-pid-N` lines mean
   exactly what they say and need no action.
2. Resolve the target from the arguments:
   - A PR URL or bare number: run `gh pr view <n> --json number,headRefOid,state,title,url`.
     **If `state` is not `OPEN`, stop and say so.** Record `NUMBER` = `<n>` and `REPO` = the
     `owner/repo` string, e.g. `gh repo view --json nameWithOwner --jq .nameWithOwner` (or parse it
     from the PR URL). These become the ledger's `target.number` / `target.repo`.
   - `.`: the current branch versus its merge-base with `main`, committed work only. HEAD_SHA is
     `git rev-parse HEAD`. There is no PR, so S3 is skipped and posting is unavailable. Use the
     literal id `local` wherever `<n>` appears in paths below (worktree `wt-prlocal`, ledger dir
     `pr-review/prlocal/`) - the reaper's coverage pattern is `*/scratchpad/wt-*`
     (`pr-review-wt.sh:9`'s `is_review_path`), so it's the full `.../scratchpad/wt-` path that
     matters, not just the `wt-` prefix; relocating the worktree out from under `$CLAUDE_SCRATCHPAD`
     drops reaper coverage even if the basename still starts with `wt-`. The ledger's `target` is
     `{"kind": "local"}`.
3. For a PR, fetch the head and resolve it to a concrete SHA before anything downstream depends on
   it:

   ```bash
   git fetch origin pull/<n>/head
   HEAD_SHA=$(git rev-parse FETCH_HEAD)
   ```

   For `.`, `HEAD_SHA` is already `git rev-parse HEAD` from step 2.
4. Now that HEAD_SHA's commit is fetched and locally resolvable, compute the merge-base:

   ```bash
   MERGE_BASE=$(git merge-base "$HEAD_SHA" origin/main)
   ```

   Store it in the ledger as `merge_base` (S6), and pass it to every refuter in S5 - "pre-existing
   on the merge-base" is one of their standing refute triggers and they need the SHA to check it.
5. Create the worktree and capture the path with a **direct command substitution** - never through
   a pipe, and never wrapped in `bash -c`:

   ```bash
   WT=$(~/.claude/pr-review/pr-review-wt.sh create "$CLAUDE_SCRATCHPAD/wt-pr<n>" "$HEAD_SHA")
   ```

   Still use this direct-substitution form - it's the reliable way to capture `$WT` cleanly - but
   don't rely on the lock's `$PPID` for protection. Measured in this harness: each tool call runs
   in its own short-lived shell, and the recorded pid does not survive to the next invocation under
   any shape (direct substitution, pipe, or `bash -c`) - so the `locked-by-live-pid-N` guard is
   inert here regardless of which form `create` was called with. The lock is provenance only (which
   session/pid created the worktree, for a human reading `git worktree list`), not a live guard.
   What actually protects the worktree mid-review, in `pr-review-wt.sh`'s `consider()`: first the
   `$CLAUDE_SCRATCHPAD` path exclusion (`pr-review-wt.sh:37-38` - `kept ... current-session`), then
   the live-lock check itself (which only ever keeps, given the pid is dead by the next call); only
   after that, and after the dirty-worktree guard, does the 24h age floor run
   (`pr-review-wt.sh:53-54` - `kept ... age-Nh`) - a fallback for when `$CLAUDE_SCRATCHPAD` isn't
   set for the `reap` call, not something checked ahead of the lock.

## S1. Generate

Invoke the `code-review` skill at level `max` against the worktree. Tell it explicitly:

- Analyze the changes in the existing worktree at `$WT`. **Do not create a worktree of your own.**
- Raise the per-subagent issue cap to 16 (from 8) and the final-report cap to 30 (from 15).
- Present the final report as a numbered list.

Any extra instructions in $ARGUMENTS ride along verbatim - for example seeding extra findings
from a file and placing them above the cap.

## S2. Split and renumber

One item = one defect, with one fix, at one site. Break every composite item apart.

Then renumber **globally, 1..N, ordered by severity descending then file path**. Severity
vocabulary is exactly `blocker`, `high`, `medium`, `low`.

**Letter suffixes are forbidden.** Never produce `2a` or `30g`. If an item splits, every
downstream number shifts. The validator (S6 step 2) enforces this and will reject the ledger
otherwise - along with duplicate ids, non-contiguous ids, and wrong severity ordering.

## S3. Dedupe against what is already on the PR

Skip entirely for a `.` target, or if the PR has no comments.

Pull existing feedback:

```bash
gh api "repos/{owner}/{repo}/pulls/<n>/comments" --paginate \
  --jq '.[] | {path, line, body: .body[0:400], url: .html_url}'
gh pr view <n> --json comments --jq '.comments[] | {author: .author.login, body: .body[0:400]}'
```

Match on file/line proximity plus claim similarity. Set `dedupe` to `{prior_url, note}` on any
item already raised. Exclude those from the main table but list them under "previously reported" -
the goal is to know, not to silently drop.

## S4. Cluster

Group items whose fix is the same or near-identical into `C1..Cn`. Give each a title and a
one-line `shared_fix`. Items keep their global ids; a cluster is a view over items, never a
renumbering.

## S5. Pressure-test

Dispatch **one independent refuter subagent per item**, in parallel batches. Give each: the item's
claim, its `file:line`, the worktree path `$WT`, `MERGE_BASE`, and the relevant CLAUDE.md paths.

Instruct each refuter:

> Try to REFUTE this finding. Default to REFUTED when you cannot positively verify it. Cite
> `file:line` evidence for your verdict either way. Refute if the issue is pre-existing on the
> merge-base, already handled elsewhere in the diff, intentional per an adjacent comment or ADR,
> something a linter/compiler/CI would catch, or not on a line this PR changed. You may revise
> the severity up or down; give your reason. Also report whether the anchor line is on a line
> this PR changed.

Record `verdict`, `verdict_reason`, and `on_changed_line` per item. Apply any severity revisions,
then **re-sort and renumber** so the 1..N ordering still holds.

Note for later: a REFUTED verdict is sticky downstream but not final. `/pr-review-post`'s
`payload --include-refuted <ids>` lets the user overrule specific REFUTED ids, per id, at post
time - without touching the ledger's `verdict`/`verdict_reason`, so the refuter's recorded
reasoning stays intact.

## S6. Report

1. Write `$CLAUDE_SCRATCHPAD/pr-review/pr<n>/ledger.json` with every field named below - `validate`
   and `payload` only check a subset of these keys, so a wrong or missing key elsewhere fails
   silently rather than loudly. In particular: a cluster **must** use `item_ids` - not `items`,
   not `ids`. `validate` does not catch a wrong key name (an absent `item_ids` defaults to empty,
   which is legal), and `payload --select C3` against a cluster with no working `item_ids` silently
   emits `"comments": []` at exit 0. Match every field name exactly:

   ```jsonc
   {
     "schema": 1,
     "target": {"kind": "pr", "repo": "<owner>/<repo>", "number": <n>},  // kind "local", repo/number omitted, for a `.` review
     "head_sha": "<HEAD_SHA>",
     "merge_base": "<MERGE_BASE>",
     "worktree": "<WT>",
     "caps": {"subagent": 16, "report": 30},        // the caps actually used, per S1
     "items": [
       {
         "id": 1,                              // global 1..N, integer only, no letter suffixes
         "severity": "blocker|high|medium|low",
         "cluster": "C3",                      // or null - must match a "clusters[].id" below
         "file": "src/foo.ts",
         "line": 142,
         "line_end": 148,
         "anchor_excerpt": "verbatim source lines this finding sits on - survives head drift",
         "on_changed_line": true,              // from the S5 refuter; false = body-only, can't post inline
         "claim": "one sentence",
         "detail": "full text for the author",
         "evidence": ["src/bar.ts:88"],
         "verdict": "CONFIRMED|REFUTED",
         "verdict_reason": "the S5 refuter's reasoning either way",
         "origin": "code-review|seed:<path>",
         "dedupe": null,                       // or {"prior_url": "...", "note": "..."} from S3
         "anchor_status": null,                 // ok|moved|dead - written by `reanchor` in /pr-review-post, not here
         "anchor_note": null                    // reanchor's reason string - written alongside anchor_status
       }
     ],
     "clusters": [
       {"id": "C3", "title": "...", "shared_fix": "...", "item_ids": [1, 12, 27]}
     ]
   }
   ```

2. Validate it, and fix any error it reports before continuing - do not proceed on a failing
   validate:
   `python3 ~/.claude/pr-review/pr-review-anchor.py validate <ledger>`
   Failures print `error: ...` lines to stderr and exit 1 (letter-suffixed ids, non-contiguous or
   duplicate ids, wrong severity ordering, a cluster/item referencing an unknown id, or a
   non-dict ledger root are all rejected this way - a wrong cluster key name like `items` instead
   of `item_ids` is NOT, since it just reads as an empty cluster). Fix the ledger and re-run
   validate; never report a ledger as ready while validate is failing.
3. Write `report.md` alongside it.
4. Print, in this order:
   - the triage table: id, severity, cluster, `file:line`, one-line claim
   - collapsed: REFUTED items with their reasons, so they can be overruled
   - collapsed: previously-reported items with links
   - the count of items not on a changed line (body-only, cannot be posted inline)
   - `Worktree: $WT` and the teardown one-liner
     `~/.claude/pr-review/pr-review-wt.sh remove $WT`

Then **stop**. Do not post anything.
