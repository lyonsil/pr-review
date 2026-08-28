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
     **If `state` is not `OPEN`, stop and say so.** Record `headRefOid` as HEAD_SHA. Record the
     `owner`/`repo` (from the URL or `gh repo view --json owner,name`) - the ledger needs them.
   - `.`: the current branch versus its merge-base with `main`, committed work only. HEAD_SHA is
     `git rev-parse HEAD`. There is no PR, so S3 is skipped and posting is unavailable.
3. Compute MERGE_BASE: `git merge-base HEAD_SHA origin/main`.
4. For a PR, fetch the head: `git fetch origin pull/<n>/head` and use `FETCH_HEAD` as HEAD_SHA.
5. Create the worktree and capture the path with a **direct command substitution** - never through
   a pipe, and never wrapped in `bash -c`:

   ```bash
   WT=$(~/.claude/pr-review/pr-review-wt.sh create "$CLAUDE_SCRATCHPAD/wt-pr<n>" <HEAD_SHA>)
   ```

   The lock `create` writes records `$PPID`. Under a direct substitution in the running shell,
   `$PPID` resolves to this persistent shell and stays alive for the rest of the session. Piped or
   `bash -c`-wrapped invocations run in a short-lived subshell whose pid is already dead by the
   time anything checks it - which makes the reaper's live-worktree guard (`locked-by-live-pid-N`)
   silently non-functional and the worktree eligible for reaping mid-review.

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
claim, its `file:line`, the worktree path `$WT`, and the relevant CLAUDE.md paths.

Instruct each refuter:

> Try to REFUTE this finding. Default to REFUTED when you cannot positively verify it. Cite
> `file:line` evidence for your verdict either way. Refute if the issue is pre-existing on the
> merge-base, already handled elsewhere in the diff, intentional per an adjacent comment or ADR,
> something a linter/compiler/CI would catch, or not on a line this PR changed. You may revise
> the severity up or down; give your reason. Also report whether the anchor line is on a line
> this PR changed.

Record `verdict`, `verdict_reason`, and `on_changed_line` per item. Apply any severity revisions,
then **re-sort and renumber** so the 1..N ordering still holds.

Note for later: a REFUTED verdict is sticky downstream. `/pr-review-post`'s `payload` step
unconditionally excludes any item whose `verdict` is `REFUTED` from what gets posted - there is no
selection-time override. If the user later wants to overrule a refuter and post the item anyway,
the ledger's `verdict` field itself must be changed away from `REFUTED` (keep `verdict_reason` as
the record of the original call).

## S6. Report

1. Write `$CLAUDE_SCRATCHPAD/pr-review/pr<n>/ledger.json` in the Task 3 schema, plus the fields
   `/pr-review-post` needs since it runs as a separate invocation with no shared shell state:

   ```json
   {
     "pr": {"number": <n>, "owner": "<owner>", "repo": "<repo>"},
     "head_sha": "<HEAD_SHA>",
     "worktree": "<WT>",
     "items": [ ... ],
     "clusters": [ ... ]
   }
   ```

   Omit `pr` (or set it `null`) for a `.` target - that is how `/pr-review-post` recognizes a
   local-diff ledger it cannot post. Capture `anchor_excerpt` as the verbatim source lines the
   finding sits on - that is what survives head drift.
2. Validate it, and fix any error it reports before continuing - do not proceed on a failing
   validate:
   `python3 ~/.claude/pr-review/pr-review-anchor.py validate <ledger>`
   Failures print `error: ...` lines to stderr and exit 1 (letter-suffixed ids, non-contiguous or
   duplicate ids, wrong severity ordering, a cluster/item referencing an unknown id, or a
   non-dict ledger root are all rejected this way). Fix the ledger and re-run validate; never
   report a ledger as ready while validate is failing.
3. Write `report.md` alongside it.
4. Print, in this order:
   - the triage table: id, severity, cluster, `file:line`, one-line claim
   - collapsed: REFUTED items with their reasons, so they can be overruled
   - collapsed: previously-reported items with links
   - the count of items not on a changed line (body-only, cannot be posted inline)
   - `Worktree: $WT` and the teardown one-liner
     `~/.claude/pr-review/pr-review-wt.sh remove $WT`

Then **stop**. Do not post anything.
