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
4. If the ledger has no `pr` field (a `.` local-diff review), stop: say posting is unavailable for
   a local-diff review and print the selected findings for manual use instead.
5. Read `WT` from the ledger's `worktree` field, not a shell variable - `/pr-review` and
   `/pr-review-post` are separate invocations, so nothing set in the earlier session's shell
   survives into this one:

   ```bash
   WT=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['worktree'])" <ledger>)
   ```

   Likewise read `<n>`, `<owner>`, `<repo>` from the ledger's `pr` object for every `gh` command
   below.
6. Resolve the selection. **Refuse any REFUTED id** unless the user explicitly overrode it. Note:
   there is no selection-time override - `payload` (step 3) unconditionally drops any item whose
   `verdict` is `REFUTED`, regardless of `--select`. To actually post an overruled item, edit that
   item's `verdict` in the ledger away from `REFUTED` (keep `verdict_reason` as the record of the
   original call) before running `payload`.

## 2. Re-anchor against the current head

The PR head moves during review. A review POST fails **wholesale** if any one anchor is not on a
changed line, so re-anchor before building anything.

```bash
NOW=$(gh pr view <n> --json headRefOid --jq .headRefOid)
```

If `NOW` differs from the ledger's `head_sha`, say so, then:

```bash
git fetch origin pull/<n>/head
git -C "$WT" checkout --detach FETCH_HEAD
gh pr diff <n> > "$CLAUDE_SCRATCHPAD/pr-review/pr<n>/head.diff"
python3 ~/.claude/pr-review/pr-review-anchor.py reanchor <ledger> \
  --worktree "$WT" --diff "$CLAUDE_SCRATCHPAD/pr-review/pr<n>/head.diff"
```

`reanchor` rewrites the ledger in place (updating `line`, `anchor_status`, `anchor_note` per item)
and always exits 0 - a `dead` count is reported, not fatal, so check the printed summary rather
than the exit code. Report the `ok` / `moved` / `dead` counts. For each dead anchor, ask whether to
demote it to the review body or drop it. Do not proceed with a dead anchor in the selection.

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

Write the review body to `$CLAUDE_SCRATCHPAD/pr-review/pr<n>/body.md`. The body is **bare framing
plus the authorized items only**. No side observations, no verification summary, no findings the
user did not select - those go back to chat, never to the PR. **Do not append the attribution
string yourself** - `payload` adds `_(AI-assisted, with my guidance)_` to the body exactly once
and only there, never to an inline thread; the body file you write should not contain it at all.

```bash
python3 ~/.claude/pr-review/pr-review-anchor.py payload <ledger> \
  --select <tokens> --body-file "$CLAUDE_SCRATCHPAD/pr-review/pr<n>/body.md" \
  > "$CLAUDE_SCRATCHPAD/pr-review/pr<n>/payload.json" \
  2> "$CLAUDE_SCRATCHPAD/pr-review/pr<n>/payload.stderr"
```

Check the exit code first. `payload` exits 1 (and writes nothing usable to stdout) on: an
unrecognized selection token, an id - or a cluster-referenced id - not present in the ledger, or a
selected item whose `file`/`line` is missing or null. Treat any of these as fatal: report the
`error: ...` line and stop rather than posting a partial or empty review.

On success, **read `payload.stderr` and relay every line to the user before presenting the plan**.
`payload` warns there (never on stdout) naming each id it silently excluded from the JSON for being
REFUTED or dead-anchored. The user must see exactly what was dropped, or they will believe they
posted findings that never made it into the payload.

Present the plan and **stop for approval**: the body verbatim, any relayed warnings, and per thread
the `file:line`, the item ids it carries, and its exact text.

Honor any constraints in $ARGUMENTS - for example "do not say whether anything is blocking".

## 4. Post

Only after approval. One atomic review:

```bash
cat "$CLAUDE_SCRATCHPAD/pr-review/pr<n>/payload.json" \
  | gh api "repos/<owner>/<repo>/pulls/<n>/reviews" --input -
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
