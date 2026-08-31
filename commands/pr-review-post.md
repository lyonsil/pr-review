---
description: Post selected PR review findings as inline comments, after re-anchoring against the current head
argument-hint: <ids | clusters | all> [constraints]
---

Post the selected findings: $ARGUMENTS

## 1. Load

1. Resolve this session's scratchpad directory - the ledger and every file written below live
   under it:

   ```bash
   SCRATCH=$(~/.claude/pr-review/pr-review-wt.sh scratchpad)
   ```

   There is **no `$CLAUDE_SCRATCHPAD` environment variable** in this harness - do not reference
   one. The subcommand honours it if it is ever set, and otherwise derives the path from
   `$CLAUDE_CODE_SESSION_ID`; it is the same resolution `reap` uses for its current-session
   exclusion. It prints `error: ...` and exits non-zero whenever it cannot produce a valid
   absolute path - never exits 0 with empty stdout - so stop there.

   `$SCRATCH` does not survive between tool calls (each runs in a fresh shell), and the derivation
   depends on the **current directory** - so either re-run the subcommand from the same repo root
   in every later shell, or resolve it once here and substitute the literal path into the commands
   below. Do not resolve it from inside `$WT` or any other directory.
2. Run `~/.claude/pr-review/pr-review-wt.sh reap`. `kept ... not-a-review-worktree` lines are
   suppressed by default; pass `reap -v` if you want them.
3. Find the newest `ledger.json` under `$SCRATCH/pr-review/`. If none exists, stop: the
   review did not run in this session, and the ledger is session-scoped by design.
4. `python3 ~/.claude/pr-review/pr-review-anchor.py validate <ledger>` - stop on any `error: ...`
   line (exit 1). Do not proceed on a failing validate; if the ledger is malformed, that is a bug
   in how `/pr-review` wrote it, not something to work around here.
5. If the ledger's `target.kind` is not `pr` (a `.` local-diff review has `target.kind` `local`),
   stop: say posting is unavailable for a local-diff review and print the selected findings for
   manual use instead.
6. Read `WT`, `NUMBER`, and `REPO` from the ledger - not shell variables - since `/pr-review` and
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
7. Resolve the selection. **Refuse any REFUTED id** unless the user explicitly overrides it. To
   post an overruled item, use `payload`'s `--include-refuted <ids>` (see step 3) - a per-id, not
   blanket, override: the id must ALSO be in `--select`, and `--include-refuted` errors (exit 1)
   on an id that's unknown in the ledger or that isn't actually `REFUTED`. This preserves the
   ledger's `verdict`/`verdict_reason` as recorded; do not hand-edit `verdict` to work around this
   - that destroys the refuter's recorded reasoning.

   `DOWNGRADED` and `INCONCLUSIVE` need no override and must not be given one - but not because
   they are held back: **placement follows anchoring, not verdict.** Every selected finding that
   can be anchored posts inline whatever verdict it reached, carrying `<severity_in> -> <severity>`,
   its status (`confirmed` / `downgraded on review` / `unresolved`) and the refuter's reasoning in
   its own header. Only a finding that cannot anchor falls back to the body.

   Anchoring is a mechanical property of the diff; the verdict is a judgement about the finding.
   Routing on the verdict buries most of a review in one wall of body text, where a reader cannot
   see which findings land in the file they are already reading - which is the whole reason inline
   comments exist. `--include-refuted` still rejects these two verdicts by design: it lifts the
   REFUTED exclusion only.

   `--body-only-verdicts DOWNGRADED INCONCLUSIVE` restores the old verdict-based routing if a
   particular review wants it. It is opt-in and off by default.

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
GH_REPO="$REPO" gh pr diff "$NUMBER" > "$SCRATCH/pr-review/pr$NUMBER/head.diff"
python3 ~/.claude/pr-review/pr-review-anchor.py reanchor <ledger> \
  --worktree "$WT" --diff "$SCRATCH/pr-review/pr$NUMBER/head.diff"
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
counts.

**Dead anchors need no decision from the user - never ask.** A finding whose anchor is dead is
still a real, verified, selected finding; it simply cannot attach to a line, and one off-diff
anchor would fail the entire review POST. `payload` therefore moves every dead or off-diff item
into the review body automatically, under its own heading, and keeps it out of `comments[]`.
Report which ids were demoted and why, then carry on. Refuted items are the opposite case and are
never demoted: they were judged not real, so they appear nowhere unless re-admitted per-id with
`--include-refuted`. `DOWNGRADED` and `INCONCLUSIVE` items are NOT routed by verdict - they post
inline like anything else when they anchor (see step 1), so they appear in the demotion warning
only when their own anchor failed.

`reanchor` also recomputes `on_changed_line` from the diff and overwrites whatever S5 recorded,
printing a `notice:` naming every id it corrected. Do not treat that as noise: it means a refuter
asserted something the diff contradicts, which is worth weighing against that refuter's other
answers. A wrong `true` fails the **entire** review POST, not just its own comment.

Also scan the per-item output (or each item's `anchor_note`) for `ambiguous:` - it means the anchor
excerpt matched more than one location and the nearest-to-old-line match was chosen automatically.
**Surface every ambiguous anchor to the user before proceeding**: a wrong duplicate match anchors
a comment to the wrong line, and re-anchoring alone cannot tell the difference between the right
site and a coincidentally identical one.

### When the head moved, re-verify the claims - not just the anchors

`reanchor` checks that each finding's **line** still exists. It cannot check that the finding is
still **true**, and after a rebase or restack it often isn't. Two separate passes, both required
whenever `$NOW` differed from the ledger's `head_sha`:

1. **Standing claims in the body.** Anything the body asserts about the current state of the branch
   - a previously-reported item still being unfixed, a red CI step, a stale generated artifact -
   must be re-run against the new head. A restack that regenerates a bundle or reformats a file
   turns those sentences into false accusations, and CI claims are the ones an author will
   reasonably take personally. Drop or rewrite whatever no longer holds, and say in the body that
   it was re-verified at the new SHA.
2. **The premises under each verdict.** A DOWNGRADED verdict usually rests on something being
   unreachable, pre-existing, or gated. If the head moved, ask whether the gate is still there.
   A finding whose only two gates were "the shipped config supplies the key" and "the committed
   build output still marks it required" becomes materially more serious the moment that build
   output is regenerated - the finding did not change, the reason for holding it low did.

Do not silently rewrite a refuter's recorded `verdict_reason` to paper over this. Append a dated
`_Re-checked at <sha>: ..._` note so the original reasoning and the correction are both visible.

## 3. Build the posting plan

Anchoring is **per distinct code site**, not per cluster and not per item. A cluster shares a
*fix*, but the fix must land at every site - the dominant failure mode on a past PR was a fix
landing on one of the two sites that needed it. The payload builder already merges items that
share a `(file, line)` and cross-references cluster siblings at other sites.

Write the review body to `$SCRATCH/pr-review/pr$NUMBER/body.md`. The body is **bare
framing plus the authorized items only**. No side observations, no verification summary, no
findings the user did not select - those go back to chat, never to the PR. **Do not append the
attribution string yourself** - `payload` adds `_(AI-assisted, with my guidance)_` to the body
exactly once and only there, never to an inline thread; the body file you write should not contain
it at all.

```bash
python3 ~/.claude/pr-review/pr-review-anchor.py payload <ledger> \
  --select <tokens> \
  --include-refuted <overruled-ids> \
  --body-file "$SCRATCH/pr-review/pr$NUMBER/body.md" \
  > "$SCRATCH/pr-review/pr$NUMBER/payload.json" \
  2> "$SCRATCH/pr-review/pr$NUMBER/payload.stderr"
STATUS=$?
```

Omit `--include-refuted` entirely when nothing is being overruled. Always read
`payload.stderr` next - both the fatal-error path and the warning path write there, never to
stdout:

```bash
cat "$SCRATCH/pr-review/pr$NUMBER/payload.stderr"
```

If `$STATUS` is nonzero, that file holds the fatal `error: ...` line. `payload` exits 1 on: an
unrecognized selection token, a selected id (or cluster-referenced id) absent from the ledger, an
`--include-refuted` id that's unknown or that isn't actually `REFUTED`, a selected item whose
`file`/`line` is missing or null, or a selection that leaves **no comments at all** (every selected
item excluded, an empty ledger, or a cluster with no working `item_ids`) - a review with zero
comments would POST bare framing plus the attribution footer and is never intended. Treat any of
these as fatal: report the error and stop rather than posting a partial or empty review.

If `$STATUS` is 0, that same file may still hold `warning: ...` lines - naming ids excluded from
the JSON (`REFUTED` without a matching `--include-refuted`, a dead anchor, or `on_changed_line:
false`, which `payload` excludes unconditionally because GitHub rejects the whole review over one
off-diff anchor), ids named by `--include-refuted` that were **not** in `--select` and so
re-admitted nothing, or ids re-admitted **because** of `--include-refuted`. A `notice:` line lists
the items carried in the body by verdict (`DOWNGRADED`/`INCONCLUSIVE`) - relay it too, so the user
knows those went out in the review body rather than as inline comments. **Relay every line to
the user before presenting the plan.** They must see exactly what was dropped or overridden, or they will believe they posted
findings that never made it into the payload, or that a refuted finding they never asked to
override went out anyway.

**The plan is printed to a terminal: plain text and GitHub-flavoured markdown only, never raw
HTML.** No `<details>`, `<summary>`, or `<br>` - the terminal prints those literally. This applies
only to what you print for the user; the review body and comment bodies you are quoting are bound
for GitHub and keep whatever markdown they carry.

Present the plan and **stop for approval**. Do NOT retype or summarize the payload - render it
from the file that will actually be sent, so what the user approves is byte-for-byte what posts:

```bash
python3 ~/.claude/pr-review/pr-review-anchor.py preview "$SCRATCH/pr-review/pr$NUMBER/payload.json"
```

Show that output **in full, every time**, however long it runs. Never abbreviate it, never show
only the first few comments, never replace a comment body with a description of it. A user
approving a summary is approving text they have not read, which then posts under their own GitHub
account. If the output is long, that is the correct amount of output.

The requirement is that the user sees the bytes that will post - not that you retype them. At two
dozen comments the preview runs to tens of KB, and transcribing it by hand risks the one failure
worse than not showing it: an approved text that differs from what posts. So render it (the tool
output itself is shown in the transcript) and give the absolute path to `payload.json` and
`preview.txt` so it can be reopened. What is never acceptable is a hand-written précis standing in
for the preview.

Alongside it, relay any warnings `payload` wrote to stderr (excluded refuted ids, dead anchors,
re-admitted ids), and state the head SHA the anchors were resolved against.

Honor any constraints in $ARGUMENTS - for example "do not say whether anything is blocking".

## 4. Post

Only after approval. **Re-read the head first** - approval takes human time, and a rebase landing
in that window is exactly what leaves the anchors stale:

```bash
NOW=$(GH_REPO="$REPO" gh pr view "$NUMBER" --json headRefOid --jq .headRefOid)
[ "$NOW" = "<ledger head_sha>" ] || { echo "head moved to $NOW - go back to step 2"; exit 1; }
```

Do not skip this because step 2 already re-anchored. Step 2 runs before the approval pause; this
runs after it. On a mismatch, return to step 2 **and** re-run the step 2 claim re-verification -
a restack can fix the very things the body asserts are broken.

Then one atomic review:

```bash
cat "$SCRATCH/pr-review/pr$NUMBER/payload.json" \
  | GH_REPO="$REPO" gh api "repos/{owner}/{repo}/pulls/$NUMBER/reviews" --input -
```

Pipe via **stdin**. `gh` cannot read scratchpad paths from this sandbox.

**A failed call does not mean nothing was written.** A 5xx (502 in particular) can be a lost
response to a request GitHub already applied - the review exists, and re-running step 4 would post
every comment a second time under the user's own account. So on ANY error, check what landed before
doing anything else:

```bash
GH_REPO="$REPO" gh api "repos/{owner}/{repo}/pulls/$NUMBER/reviews" --paginate \
  --jq '.[] | select(.user.login=="<the posting user>") | {id, submitted_at, body_start: (.body[0:60])}'
```

If a review matching this payload's body is there, the POST succeeded: verify its comment count
(`select(.pull_request_review_id==<id>)` over `pulls/$NUMBER/comments`) and go to step 5. Only if
nothing landed should you retry, and then re-run step 2 first, since a genuine wholesale rejection
usually means the head moved.

## 5. Tear down

```bash
~/.claude/pr-review/pr-review-wt.sh remove "$WT"
```

`remove` deletes the worktree unconditionally - unlike `reap`, it does **not** check for
uncommitted or untracked changes first. If there is working-tree state in `$WT` worth keeping,
save it before calling `remove`.

Report the posted review URL and confirm the worktree is gone.
