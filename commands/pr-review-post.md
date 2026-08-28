---
description: Post selected PR review findings as inline comments, after re-anchoring against the current head
argument-hint: <ids | clusters | all> [constraints]
---

Post the selected findings: $ARGUMENTS

## 1. Load

1. Run `~/.claude/pr-review/pr-review-wt.sh reap`.
2. Find the newest `ledger.json` under `$CLAUDE_SCRATCHPAD/pr-review/`. If none exists, stop: the
   review did not run in this session, and the ledger is session-scoped by design.
3. `python3 ~/.claude/pr-review/pr-review-anchor.py validate <ledger>` - stop on any `error: ...`
   line (exit 1). Do not proceed on a failing validate; if the ledger is malformed, that is a bug
   in how `/pr-review` wrote it, not something to work around here.
4. If the ledger's `target.kind` is not `pr` (a `.` local-diff review has `target.kind` `local`),
   stop: say posting is unavailable for a local-diff review and print the selected findings for
   manual use instead.
5. Read `WT`, `NUMBER`, and `REPO` from the ledger - not shell variables - since `/pr-review` and
   `/pr-review-post` are separate invocations and nothing set in the earlier session's shell
   survives into this one. Handle a ledger that predates or omits `worktree` rather than letting a
   raw `KeyError` surface:

   ```bash
   WT=$(python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
wt = d.get('worktree')
if not wt:
    sys.exit('error: ledger has no worktree field - re-run /pr-review')
print(wt)
" <ledger>)
   NUMBER=$(python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
n = d.get('target', {}).get('number')
if not n:
    sys.exit('error: ledger has no target.number - re-run /pr-review')
print(n)
" <ledger>)
   REPO=$(python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
r = d.get('target', {}).get('repo')
if not r:
    sys.exit('error: ledger has no target.repo - re-run /pr-review')
print(r)
" <ledger>)
   ```

   If any of these three commands prints an `error: ...` line instead of a value, stop - there is
   nothing safe to re-anchor or post against. Before any `gh` call below, also assert `$REPO` is
   non-empty in the shell that will run it (`[ -n "$REPO" ] || { echo "REPO is empty, aborting"; exit 1; }`)
   - an empty `GH_REPO` would send the request to whatever repo the ambient `gh` context falls back
   to, not necessarily this one. Every `gh` command below must not depend on this invocation's cwd
   matching the target repo - prefix each with `GH_REPO="$REPO"` so `gh`'s own `{owner}/{repo}`
   placeholder resolves correctly regardless of where the command happens to run.
6. Resolve the selection. **Refuse any REFUTED id** unless the user explicitly overrides it. To
   post an overruled item, use `payload`'s `--include-refuted <ids>` (see step 3) - a per-id, not
   blanket, override: the id must ALSO be in `--select`, and `--include-refuted` errors (exit 1)
   on an id that's unknown in the ledger or that isn't actually `REFUTED`. This preserves the
   ledger's `verdict`/`verdict_reason` as recorded; do not hand-edit `verdict` to work around this
   - that destroys the refuter's recorded reasoning.

## 2. Re-anchor against the current head

The PR head can move during review, and a review POST fails **wholesale** if any one anchor is not
on a changed line. Re-anchor **unconditionally, every time** - not only when the head has moved -
so `anchor_status` is set on every item before `payload` runs. Skipping this on the unchanged-head
path is exactly the gap that lets a body-only or already-dead item stay selectable into step 3.

```bash
NOW=$(GH_REPO="$REPO" gh pr view "$NUMBER" --json headRefOid --jq .headRefOid)
if [ "$NOW" != "<ledger head_sha>" ]; then
  echo "head moved: <ledger head_sha> -> $NOW"
fi
git -C "$WT" fetch origin "pull/$NUMBER/head"
git -C "$WT" checkout --detach FETCH_HEAD
GH_REPO="$REPO" gh pr diff "$NUMBER" > "$CLAUDE_SCRATCHPAD/pr-review/pr$NUMBER/head.diff"
python3 ~/.claude/pr-review/pr-review-anchor.py reanchor <ledger> \
  --worktree "$WT" --diff "$CLAUDE_SCRATCHPAD/pr-review/pr$NUMBER/head.diff"
```

Fetch and check out **inside the worktree** (`git -C "$WT" fetch ...` / `git -C "$WT" checkout
...`), never in whatever directory this command happens to run from. `FETCH_HEAD` is per-checkout:
a fetch run in a different checkout leaves the worktree's own `FETCH_HEAD` untouched, and
`git -C "$WT" checkout --detach FETCH_HEAD` then fails outright (confirmed: it errors immediately
when the fetch and the checkout target different checkouts).

`reanchor` exits 1 only on a ledger or diff **load** failure (e.g. `error: diff file not found`) or
a ledger **write** failure after re-anchoring (e.g. a read-only ledger file) - check that and stop
before trusting anything downstream. Otherwise it exits 0 regardless of per-item results, so from
there judge outcomes by the printed `ok`/`moved`/`dead` summary, not the exit code. Report the
counts. For each dead anchor, ask whether to demote it to the review body or drop it. Do not
proceed with a dead anchor in the selection.

Also scan the per-item output (or each item's `anchor_note`) for `ambiguous:` - it means the anchor
excerpt matched more than one location and the nearest-to-old-line match was chosen automatically.
**Surface every ambiguous anchor to the user before proceeding**: a wrong duplicate match anchors
a comment to the wrong line, and re-anchoring alone cannot tell the difference between the right
site and a coincidentally identical one.

## 3. Build the posting plan

Anchoring is **per distinct code site**, not per cluster and not per item. A cluster shares a
*fix*, but the fix must land at every site - the dominant failure mode on a past PR was a fix
landing on one of the two sites that needed it. The payload builder already merges items that
share a `(file, line)` and cross-references cluster siblings at other sites.

Write the review body to `$CLAUDE_SCRATCHPAD/pr-review/pr$NUMBER/body.md`. The body is **bare
framing plus the authorized items only**. No side observations, no verification summary, no
findings the user did not select - those go back to chat, never to the PR. **Do not append the
attribution string yourself** - `payload` adds `_(AI-assisted, with my guidance)_` to the body
exactly once and only there, never to an inline thread; the body file you write should not contain
it at all.

```bash
python3 ~/.claude/pr-review/pr-review-anchor.py payload <ledger> \
  --select <tokens> \
  --include-refuted <overruled-ids> \
  --body-file "$CLAUDE_SCRATCHPAD/pr-review/pr$NUMBER/body.md" \
  > "$CLAUDE_SCRATCHPAD/pr-review/pr$NUMBER/payload.json" \
  2> "$CLAUDE_SCRATCHPAD/pr-review/pr$NUMBER/payload.stderr"
STATUS=$?
```

Omit `--include-refuted` entirely when nothing is being overruled. Always read
`payload.stderr` next - both the fatal-error path and the warning path write there, never to
stdout:

```bash
cat "$CLAUDE_SCRATCHPAD/pr-review/pr$NUMBER/payload.stderr"
```

If `$STATUS` is nonzero, that file holds the fatal `error: ...` line. `payload` exits 1 on: an
unrecognized selection token, a selected id (or cluster-referenced id) absent from the ledger, an
`--include-refuted` id that's unknown or that isn't actually `REFUTED`, or a selected item whose
`file`/`line` is missing or null. Treat any of these as fatal: report the error and stop rather
than posting a partial or empty review.

If `$STATUS` is 0, that same file may still hold `warning: ...` lines - naming ids silently
excluded from the JSON (`REFUTED` without a matching `--include-refuted`, or dead-anchored), or ids
re-admitted **because** of `--include-refuted`. **Relay every line to the user before presenting
the plan.** They must see exactly what was dropped or overridden, or they will believe they posted
findings that never made it into the payload, or that a refuted finding they never asked to
override went out anyway.

Present the plan and **stop for approval**: the body verbatim, any relayed warnings, and per thread
the `file:line`, the item ids it carries, and its exact text.

Honor any constraints in $ARGUMENTS - for example "do not say whether anything is blocking".

## 4. Post

Only after approval. One atomic review:

```bash
cat "$CLAUDE_SCRATCHPAD/pr-review/pr$NUMBER/payload.json" \
  | GH_REPO="$REPO" gh api "repos/{owner}/{repo}/pulls/$NUMBER/reviews" --input -
```

Pipe via **stdin**. `gh` cannot read scratchpad paths from this sandbox.

If the POST fails, do not retry blindly - re-run step 2, since a wholesale failure almost always
means the head moved again between re-anchoring and posting.

## 5. Tear down

```bash
~/.claude/pr-review/pr-review-wt.sh remove "$WT"
```

`remove` deletes the worktree unconditionally - unlike `reap`, it does **not** check for
uncommitted or untracked changes first. If there is working-tree state in `$WT` worth keeping,
save it before calling `remove`.

Report the posted review URL and confirm the worktree is gone.
