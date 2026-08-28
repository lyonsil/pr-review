---
description: Deep PR review - generate, split, dedupe, cluster, and pressure-test findings into a triage table
argument-hint: <PR-url | PR-number | .> [extra instructions]
---

Run the review pipeline for: $ARGUMENTS

Work through these stages in order. Do not skip ahead to posting - this command stops at the
triage table.

## S0. Setup

1. Resolve this session's scratchpad directory. Every path below hangs off it:

   ```bash
   SCRATCH=$(~/.claude/pr-review/pr-review-wt.sh scratchpad)
   ```

   There is **no `$CLAUDE_SCRATCHPAD` environment variable** in this harness - do not reference
   one. The subcommand honours it if it is ever set, and otherwise derives the path from
   `$CLAUDE_CODE_SESSION_ID` (`pr-review-wt.sh`'s `scratchpad_path`). Use the subcommand rather
   than deriving the path yourself: `reap`'s current-session exclusion calls the same function, so
   a worktree placed under `$SCRATCH` is exactly the set the reaper protects. It prints an
   `error: ...` line and exits non-zero whenever it cannot produce a valid absolute path (neither
   variable set, or `$CLAUDE_SCRATCHPAD` holding something like `/` or a relative path) - it never
   exits 0 with empty stdout, so `SCRATCH=$(...)` fails loudly instead of silently yielding `""`
   and expanding every path below to `/wt-pr<n>`. Stop there rather than creating a worktree at a
   path the reaper does not protect.

   `$SCRATCH` does not survive between tool calls (each runs in a fresh shell), and the derivation
   depends on the **current directory** - so either re-run the subcommand from the same repo root
   in every later shell, or resolve it once here and substitute the literal path into the commands
   below. Do not resolve it from inside `$WT` or any other directory.
2. Run `~/.claude/pr-review/pr-review-wt.sh reap` and report any `reaped`, `orphaned`, or
   `stranded-branch` lines. A `kept ... dirty-N-changes` line is normal, not an error: the reaper
   never removes a worktree with uncommitted or untracked changes, and never deletes branches -
   it only reports them. `kept ... current-session` / `kept ... locked-by-live-pid-N` lines mean
   exactly what they say and need no action. `kept ... not-a-review-worktree` lines are suppressed
   by default (there is one per worktree the reaper is not responsible for, the repo's own main
   worktree included, and in a real repo they bury the lines that matter); pass `reap -v` if you
   need to see them; `reap` rejects any other argument with a usage line and a non-zero exit, so a
   mistyped flag fails rather than silently running non-verbose.
3. Resolve the target from the arguments:
   - A PR URL or bare number: run
     `gh pr view <n> --json number,headRefOid,state,title,url,baseRefName`.
     **If `state` is not `OPEN`, stop and say so.** Record `NUMBER` = `<n>`, `REPO` = the
     `owner/repo` string (e.g. `gh repo view --json nameWithOwner --jq .nameWithOwner`, or parse it
     from the PR URL), and `BASE_REF` = `baseRefName` - the branch this PR actually targets, which
     is **not** always `main`: it is `master` in some repos, and another PR's branch in a stacked
     PR. `NUMBER`/`REPO` become the ledger's `target.number` / `target.repo`.
   - `.`: the current branch versus its merge-base with the repo's default branch, committed work
     only. HEAD_SHA is `git rev-parse HEAD`. Detect the default branch rather than assuming `main`:

     ```bash
     BASE_REF=$(git symbolic-ref -q --short refs/remotes/origin/HEAD | sed 's#^origin/##')
     ```

     If that prints nothing, `origin/HEAD` was never set locally; run `git remote set-head origin -a`
     once and retry, or fall back to the first of `main`/`master` that
     `git rev-parse --verify -q refs/remotes/origin/<b>` resolves. There is no PR, so S3 is skipped
     and posting is unavailable. Use the literal id `local` wherever `<n>` appears in paths below
     (worktree `wt-prlocal`, ledger dir `pr-review/prlocal/`) - the reaper's coverage pattern is
     `*/scratchpad/wt-*` (`pr-review-wt.sh`'s `is_review_path`), so it's the full
     `.../scratchpad/wt-` path that matters, not just the `wt-` prefix; relocating the worktree out
     from under `$SCRATCH` drops reaper coverage even if the basename still starts with `wt-`. The
     ledger's `target` is `{"kind": "local"}`.
4. For a PR, fetch the head and resolve it to a concrete SHA before anything downstream depends on
   it:

   ```bash
   git fetch origin pull/<n>/head
   HEAD_SHA=$(git rev-parse FETCH_HEAD)
   ```

   For `.`, `HEAD_SHA` is already `git rev-parse HEAD` from step 3.
5. Now that HEAD_SHA's commit is fetched and locally resolvable, compute the merge-base against
   `BASE_REF` - never a hardcoded `origin/main`:

   ```bash
   git fetch origin "$BASE_REF"
   MERGE_BASE=$(git merge-base "$HEAD_SHA" "$(git rev-parse FETCH_HEAD)")
   ```

   Capture `HEAD_SHA` (step 4) **before** this fetch: `FETCH_HEAD` is a single per-checkout ref and
   this second fetch overwrites it. For `.`, `BASE_REF` is already a local remote-tracking branch,
   so `MERGE_BASE=$(git merge-base HEAD "origin/$BASE_REF")` is enough.

   Store `MERGE_BASE` in the ledger as `merge_base` (S6), and pass it to every refuter in S5 -
   "pre-existing on the merge-base" is one of their standing refute triggers and they need the SHA
   to check it.
6. Create the worktree and capture the path with a **direct command substitution** - never through
   a pipe, and never wrapped in `bash -c`:

   ```bash
   WT=$(~/.claude/pr-review/pr-review-wt.sh create "$SCRATCH/wt-pr<n>" "$HEAD_SHA")
   ```

   Still use this direct-substitution form - it's the reliable way to capture `$WT` cleanly - but
   don't rely on the lock's `$PPID` for protection. Measured in this harness: each tool call runs
   in its own short-lived shell, and the recorded pid does not survive to the next invocation under
   any shape (direct substitution, pipe, or `bash -c`) - so the `locked-by-live-pid-N` guard is
   inert here regardless of which form `create` was called with. The lock is provenance only (which
   session/pid created the worktree, for a human reading `git worktree list`), not a live guard.
   What actually protects the worktree mid-review, in `pr-review-wt.sh`'s `consider()`: first the
   scratchpad path exclusion (the `scratchpad_path` resolution from step 1 - `kept ...
   current-session`), then the live-lock check itself (which only ever keeps, given the pid is dead
   by the next call); only after that, and after the dirty-worktree guard, does the 24h age floor
   run (`kept ... age-Nh`) - a fallback for when the scratchpad cannot be resolved for the `reap`
   call, not something checked ahead of the lock.

## S1. Generate

Snapshot the worktree registry **before** invoking the skill:

```bash
mkdir -p "$SCRATCH"
git worktree list --porcelain > "$SCRATCH/worktrees.before"
```

Invoke the `code-review` skill at level `max` against the worktree. Tell it explicitly:

- Analyze the changes in the existing worktree at `$WT`. **Do not create a worktree of your own.**
- Raise the per-subagent issue cap to 16 (from 8) and the final-report cap to 30 (from 15).
- Present the final report as a numbered list.

Then snapshot again and report any delta to the user:

```bash
git worktree list --porcelain > "$SCRATCH/worktrees.after"
diff "$SCRATCH/worktrees.before" "$SCRATCH/worktrees.after"
```

That the skill honours "do not create a worktree of your own" is an assumption, not a guarantee.
Any added worktree is one this pipeline does not own and will not tear down in S5 of
`/pr-review-post`; if the diff is non-empty, **say so explicitly** and name the added path so the
user can decide whether to remove it, rather than letting an unowned worktree leak silently.

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

1. Write `$SCRATCH/pr-review/pr<n>/ledger.json` with every field named below - `validate`
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
         "on_changed_line": true,              // from the S5 refuter; false = body-only, can't post inline.
                                               // `payload` excludes false items itself, with a
                                               // stderr warning - GitHub rejects the WHOLE review
                                               // over one off-diff anchor. Missing/null is treated
                                               // as "not excluded", so fill it in for every item.
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
