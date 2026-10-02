---
description: Post selected PR review findings as inline comments, after re-anchoring against the current head
argument-hint: <ids | clusters | all> [github|reviewable] [constraints]
---

Post the selected findings: $ARGUMENTS

Tool output does not reach the user's screen - only your own message text does. Wherever this
command says to report, relay, show or name something a tool printed, put it in your message
text, copied verbatim where it says so.

## 1. Load

1. Resolve this session's scratchpad directory - the ledger and every file written below live
   under it:

   ```bash
   SCRATCH=$(~/.claude/pr-review/pr-review-wt.sh scratchpad)
   ```

   There is **no `$CLAUDE_SCRATCHPAD` environment variable** in this harness - do not reference
   one. The subcommand honors it if it is ever set, and otherwise derives the path from
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

   A `schema` error means the ledger predates this version: re-run `/pr-review`.

   A `fix_verdict` error means the ledger was written before the fix was checked - either by an
   older `/pr-review`, or by a run whose refuter never answered. **Re-run `/pr-review`; never hand-add
   the field.** Writing `"fix_verdict": "SOUND"` to clear the error publishes an unchecked fix under
   the user's account, which is precisely what the guard exists to prevent. The same goes for a
   `hedged` error: reword the `Fix:` slot into the concrete edit or the constraint, do not delete
   the finding to get past it.
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
7. Resolve the selection. `--select` takes ids, ranges (`3-7`), cluster ids, commas and `all`.
   **Refuse any REFUTED id** unless the user explicitly overrides it. To post an overruled item,
   use `payload`'s `--include-refuted <ids>` (see step 3) - a per-id, not blanket, override: the
   id must ALSO be in `--select`, and `--include-refuted` errors (exit 1) on an id that's unknown
   in the ledger or that isn't actually `REFUTED`. This preserves the ledger's
   `verdict`/`verdict_reason` as recorded; do not hand-edit `verdict` to work around this - that
   destroys the refuter's recorded reasoning.

   `DOWNGRADED` and `INCONCLUSIVE` need no override and must not be given one - but not because
   they are held back: **placement follows anchoring, not verdict.** Every selected finding that
   can be anchored posts inline whatever verdict it reached, carrying the severity it settled at
   and a plain-English status in its own header - `checked and confirmed` or `needs a human call`.
   Only a finding that cannot anchor falls back to the body.

   `DOWNGRADED` deliberately renders the same as `CONFIRMED`. With the severity revision no longer
   published, a downgraded finding IS a real finding at the severity shown, and whatever its
   downgrade reasoning held that the author needs was folded into its `Why it matters:` text back
   in `/pr-review` S6.

   Anchoring is a mechanical property of the diff; the verdict is a judgment about the finding.
   Routing on the verdict buries most of a review in one wall of body text, where a reader cannot
   see which findings land in the file they are already reading - which is the whole reason inline
   comments exist. `--include-refuted` still rejects these two verdicts by design: it lifts the
   REFUTED exclusion only.

   `--body-only-verdicts DOWNGRADED INCONCLUSIVE` restores the old verdict-based routing if a
   particular review wants it. It is opt-in and off by default.

   **Surface every `UNVERIFIABLE` fix in the selection before going further.** These post like any
   other finding - the claim was verified, only the remedy was not - but the author is being handed
   a fix nobody could establish, so the user decides whether it goes out as-is, gets reworded to a
   constraint, or is dropped from the selection. List them by id with the refuter's `fix_reason`,
   and do not fold the question into the step 3 approval, where it reads as one line in a long
   preview.

## 1.5 Choose the backend

Two backends can carry this review: GitHub's own review API, and Reviewable through its MCP server.
The choice decides the artifact `payload` builds in step 3 and the whole of step 4, so settle it
here, before anything is built. Take the first of these that answers:

1. **A provider named in $ARGUMENTS** (`github` or `reviewable`). An explicit instruction wins
   over everything below it.
2. **A provider already chosen earlier in this conversation for this same `$REPO`.** Re-deriving
   it mid-session is how a second posting run lands somewhere the first did not. A choice made for
   another repo does not carry over: whether Reviewable covers a repo is decided per repo (its free
   tier, for one, covers only public ones), so a later repo in the same conversation goes on down
   this list.
3. **The repo-scoped default in `~/.claude/pr-review/providers.json`** - a flat JSON object keyed by
   `owner/repo`, the same string step 1 read into `$REPO`:

   ```bash
   python3 -c "
import json, os, sys
path = os.path.expanduser('~/.claude/pr-review/providers.json')
if not os.path.exists(path):
    sys.exit(0)
try:
    data = json.load(open(path, encoding='utf-8'))
except (OSError, ValueError) as e:
    sys.exit('error: %s is not readable JSON: %s' % (path, e))
if not isinstance(data, dict):
    sys.exit('error: %s must be a JSON object keyed by owner/repo' % path)
for repo, v in sorted(data.items()):
    if v not in ('github', 'reviewable'):
        sys.exit('error: %s: the entry for %s is %r, not github or reviewable' % (path, repo, v))
print(data.get(sys.argv[1], ''))
" "$REPO"
   ```

   A missing file or an empty line means no default is recorded - fall through to detection.
   Neither is an error. An `error:` line means the file is damaged: **stop**, show the line to the
   user and ask them to fix or delete the file. The line names the entry at fault, which may be
   another repo's: every entry is checked, because every write carries the others forward
   unchanged. Do not fall through to detection, which would choose a backend while the user's
   recorded choice is unreadable, and do not rewrite the file unasked. The key is per repo because
   the answer is per repo: a team on Reviewable for one repo reviews another on GitHub, and one
   global default would carry the wrong answer across silently.
4. **Detection.** Read the Reviewable MCP server's own `reviewable://skills/choose-review-provider`
   resource and follow it. Read it - do not reconstruct it from memory, and do not infer the answer
   from whether some tool call happens to work. It is the server's statement about its own
   configuration, and this command does not restate its rules so that they stay in one place.

**A connection failure is not a detection result.** If the Reviewable tools are unavailable, decide
which of two situations this is before going any further:

- **Configured but unreachable** - the server is set up for this repo and is not answering (a
  timeout, a failed session-start handshake, an auth error). **Stop.** Say the server is
  unreachable, ask the user to reconnect it, and wait. Do NOT fall back to GitHub: this server
  fails at session start often enough that a silent fallback would be the routine outcome, and its
  symptom is a review published in the wrong place under the user's own account, found by the PR
  author.
- **Not configured at all** - Reviewable is not in use here. That, and only that, selects GitHub.

An unreachable server and an absent one both look like "the tools do not work", so tell them apart
deliberately, using the recorded default and the `choose-review-provider` resource. When you still
cannot, treat it as unreachable and ask: stopping costs a delay, guessing costs a published review.

Then record and say so:

- If detection produced the provider and `providers.json` records nothing for this repo, write it
  so the next run does not re-derive it.
- If $ARGUMENTS produced the provider and `providers.json` records nothing for this repo, use it
  for this run and **ask once whether to record it as this repo's default**; write only on a yes.
  A provider named for one run is a decision about one review until the user says otherwise, and
  a default recorded from it would send every later plain run there without a word.
- If `providers.json` records a different provider for this repo, use the chosen one for this run
  and **ask before rewriting the file. Never change a saved default silently.** A one-off
  `/pr-review-post all reviewable` against a GitHub repo is a decision about one review, not about
  every review after it, and the user has no other way to notice the default moved.
- State the chosen provider, which rule above chose it, and - when you wrote anything - the
  absolute path you wrote to. When nothing was written, name no path.

Every write goes through this, which keeps every other repo's entry, sets only `$REPO`'s, and
creates the file and its directory if needed. Pass `github` or `reviewable` as the second argument;
it prints the path it wrote:

```bash
python3 -c "
import json, os, sys
path = os.path.expanduser('~/.claude/pr-review/providers.json')
data = {}
if os.path.exists(path):
    data = json.load(open(path, encoding='utf-8'))
data[sys.argv[1]] = sys.argv[2]
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, 'w', encoding='utf-8') as fh:
    json.dump(data, fh, indent=2, sort_keys=True)
    fh.write('\n')
print(path)
" "$REPO" "<provider>"
```

Run rule 3's lookup before any of these bullets, even when rule 1 or 2 chose the provider: the
bullets turn on what the file records, and the write assumes a file the lookup accepted, so it
does not re-check it.

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
  --worktree "$WT" --diff "$SCRATCH/pr-review/pr$NUMBER/head.diff" --head "$NOW"
```

`--head "$NOW"` records the ledger's `anchored_sha` - the head these anchors were resolved
against, which step 4 checks after approval.

Fetch and check out **inside the worktree** (`git -C "$WT" fetch ...` / `git -C "$WT" checkout
...`), never in whatever directory this command happens to run from. `FETCH_HEAD` is per-checkout:
a fetch run in a different checkout leaves the worktree's own `FETCH_HEAD` untouched, and
`git -C "$WT" checkout --detach FETCH_HEAD` then fails outright (confirmed: it errors immediately
when the fetch and the checkout target different checkouts).

`reanchor` exits 1 only on a ledger or diff **load** failure (e.g. `error: diff file not found`) or
a ledger **write** failure after re-anchoring (e.g. a read-only ledger file) - check that and stop
before trusting anything downstream. Otherwise it exits 0 regardless of per-item results, so from
there judge outcomes by the printed `ok`/`moved`/`off-diff`/`dead` summary, not the exit code.
Report the counts. `off-diff` means the excerpt was found but not on a line this PR changed;
`dead` means the excerpt is gone.

**Dead or off-diff anchors need no decision from the user - never ask.** A finding whose anchor is
dead or off-diff is still a real, verified, selected finding. On GitHub it simply cannot attach to
a line, and one off-diff anchor would fail the entire review POST, so `payload` moves every dead or
off-diff item into the review body automatically, under its own heading, and keeps it out of
`comments[]`. On Reviewable only a dead anchor, a finding with no file or line, or a finding in a
file the PR does not touch goes into the review body; any other off-diff finding posts inline as
its own discussion, opening with a line telling the reader the PR does not change that code.
Reviewable keys a discussion to one of the PR's own files, so a file outside the diff has nothing to
key it to. `reanchor` records which case each finding is as `file_in_diff`, true when the diff
names the finding's file at all - a rename, a mode change or a binary change included, though
none of those prints a `+++` line. Report which ids were demoted or posted off the diff and why,
then carry on. Refuted items are the opposite
case and are never demoted: they were judged not real, so they appear nowhere unless re-admitted
per-id with `--include-refuted`. `DOWNGRADED` and `INCONCLUSIVE` items are NOT routed by verdict -
they post inline like anything else when they anchor (see step 1), so they appear in the demotion
warning only when their own anchor failed.

`reanchor` also recomputes `on_changed_line` from the diff and overwrites what `/pr-review` S6's
`changed-lines` recorded, printing a `notice:` naming every id whose value changed. Do not treat
that as noise: the diff moved under that finding since the review, which is worth checking before
you trust the rest of its text. A wrong `true` fails the **entire** review POST, not just its own
comment; on Reviewable it posts a comment on unchanged code without the line that says so.

Also scan the per-item output (or each item's `anchor_note`) for `ambiguous:` - it means the anchor
excerpt matched more than one location and the nearest-to-old-line match was chosen automatically.
**Surface every ambiguous anchor to the user before proceeding**: a wrong duplicate match anchors
a comment to the wrong line, and re-anchoring alone cannot tell the difference between the right
site and a coincidentally identical one.

### When the head moved, re-verify the claims and the fixes - not just the anchors

`reanchor` checks that each finding's **line** still exists. It cannot check that the finding is
still **true**, and it never reads the `Fix:` slot at all. After a rebase or restack, neither
holds. Three separate passes, all required whenever `$NOW` differed from the ledger's `head_sha`:

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
3. **The fixes.** A fix names symbols, files and line numbers in the *head* it was verified
   against. A rebase that renames a symbol, moves it between modules, changes an export, or
   regenerates the file the fix points at turns a verified instruction into a wrong one, and nothing
   downstream notices. Re-check every selected item's fix against the new head with the same three
   properties S5 used: does every symbol it names still exist and stay reachable from the site, does
   the mechanism still apply, does the obligation table still bind the same way. Downgrade anything
   you cannot re-establish to a constraint rather than posting a stale prescription, and demote
   its `fix_verdict` to `UNVERIFIABLE` (the edits are below).

Every change here goes through `edit`, never a hand edit of the JSON. `edit` replaces a whole field
with a file's contents, so write each file under `$SCRATCH/pr-review/pr$NUMBER/edits/` with the
Write tool:

```bash
A=~/.claude/pr-review/pr-review-anchor.py; L=<ledger>
python3 $A edit $L <id> --field detail --text-file <f>        # the reworded Fix: slot, whole detail
python3 $A edit $L <id> --field fix_verdict --text-file <f>   # the file holds exactly UNVERIFIABLE
python3 $A edit $L <id> --field fix_reason --text-file <f>    # or --field verdict_reason
```

`fix_verdict` accepts only `UNVERIFIABLE`: a stale fix can be demoted, never promoted. Do not
silently rewrite a refuter's recorded `verdict_reason` or `fix_reason` to paper over any of this:
the file you pass is the original text unchanged, plus one new last line, so the original
reasoning and the correction are both visible. The two fields differ in who reads that line:

- `fix_reason` never posts. End it with `Re-checked at <sha>: <what changed and what you
  found>.`
- `verdict_reason` posts on a `CONFIRMED` or `INCONCLUSIVE` finding, inside the italic
  `_How this was checked: ..._` / `_What could not be checked: ..._` footer. Write the new line for
  the author, with no SHA and no underscores or other markdown (they would break the footer's
  italics): `Re-checked against the latest push: <what still holds, with file:line>.` No review
  vocabulary - `validate` rejects it there.

After the last edit, re-run `validate` before building the payload, and stop on any `error:`:

```bash
python3 ~/.claude/pr-review/pr-review-anchor.py validate <ledger>
```

## 3. Build the posting plan

Anchoring is **per distinct code site**, not per cluster and not per item. A cluster shares a
*fix*, but the fix must land at every site - the dominant failure mode on a past PR was a fix
landing on one of the two sites that needed it. The payload builder already merges items that
share a `(file, line)` and cross-references cluster siblings at other sites.

Write the review body to `$SCRATCH/pr-review/pr$NUMBER/body.md`. The body is **bare
framing plus the authorized items only**. No side observations, no verification summary, no
findings the user did not select - those go back to chat, never to the PR.

The framing is two or three sentences, written for someone opening the PR cold: what was reviewed,
how many findings there are, and how to read the labels each comment carries - that the severity
shown is the severity the reviewer settled on, that `checked and confirmed` means the finding was
verified against the code, and that `needs a human call` means it could not be settled either way
and the author has to judge it. State the number of findings as `{count}`; `payload` fills it in.
Do not explain the pipeline that produced any of this: no stages, no refuters, no severity
revisions. A reader should be able to act on the review without knowing how it was made.

**Do not append the attribution string yourself** - `payload` adds
`_(AI-assisted, with my guidance)_` to the body exactly once and only there, never to an
inline thread; the body file you write should not contain it at all.

```bash
python3 ~/.claude/pr-review/pr-review-anchor.py payload <ledger> \
  --provider <provider> \
  --select <tokens> \
  --include-refuted <overruled-ids> \
  --body-file "$SCRATCH/pr-review/pr$NUMBER/body.md" \
  > "$SCRATCH/pr-review/pr$NUMBER/payload.json" \
  2> "$SCRATCH/pr-review/pr$NUMBER/payload.stderr"
STATUS=$?
```

`--provider` takes the backend settled in step 1.5, `github` or `reviewable`. Pass it every time
rather than leaning on the default: the default is `github`, so a forgotten flag on a Reviewable
review does not fail - it silently builds the GitHub artifact, with every off-diff finding in the
body. Both backends run the same selection, exclusions and warnings; they differ in where an
off-diff finding lands and in the shape of the file.

On Reviewable the review body becomes the review-level comment, and it carries every finding that
has nowhere else to go: a dead anchor, a finding with no file or line, a finding in a file the pull
request does not change, and a verdict routed by `--body-only-verdicts`. The comment is raised at
the strongest disposition among those findings, as a merged discussion is, and at `informing` when
it carries none. So it publishes resolved only when nothing in it asks for a reply, and a blocker
in it makes the whole comment `blocking`, holding the merge for everything it carries. Say which
applies when the body carries findings.

Omit `--include-refuted` entirely when nothing is being overruled. Always read
`payload.stderr` next - both the fatal-error path and the warning path write there, never to
stdout:

```bash
cat "$SCRATCH/pr-review/pr$NUMBER/payload.stderr"
```

If `$STATUS` is nonzero, that file holds the fatal `error: ...` line. `payload` exits 1 on: an
unrecognized selection token, a selected id (or cluster-referenced id) absent from the ledger, an
`--include-refuted` id that's unknown or that isn't actually `REFUTED`, a selected item whose
`file`/`line` is missing or null, or a selection that leaves **nothing to post** - no inline
comment and no finding moved into the body (every selected item REFUTED, an empty ledger, or a
cluster with no working `item_ids`). A review like that would POST bare framing plus the
attribution footer and is never intended. Treat any of these as fatal: report the error and stop
rather than posting a partial or empty review.

If `$STATUS` is 0, that same file may still hold `warning: ...` lines - naming ids excluded from
the JSON (`REFUTED` without a matching `--include-refuted`), ids moved into the review body with
the label `dead anchor` or `not on a changed line` (GitHub rejects the whole review over one
off-diff anchor), ids named by `--include-refuted` that were **not** in `--select` and so
re-admitted nothing, ids re-admitted **because** of `--include-refuted`, and `{{#id}}`
cross-references to a finding that is not in this review. With `--provider reviewable` the
body-bound line reads `moved into the review-level comment, which publishes resolved only when
nothing in it asks for a reply` and names only findings with nowhere in the pull request to attach
a comment (`dead anchor`, `no file or line`, `file not in the pull request`); the
`--body-only-verdicts` notice says the same of the review-level comment; and a `notice:` line names
the off-diff ids that post inline instead. **Relay every line to the user, as message
text, before presenting the plan.** They must see exactly what was dropped, moved or overridden, or they will
believe they posted findings that never made it into the payload, or that a refuted finding they
never asked to override went out anyway.

**The plan is printed to a terminal: plain text and GitHub-flavored markdown only, never raw
HTML.** No `<details>`, `<summary>`, or `<br>` - the terminal prints those literally. This applies
only to what you print for the user; the review body and comment bodies you are quoting are bound
for GitHub and keep whatever markdown they carry.

Then present the plan: every page of the preview first, and only then **stop for approval** -
once, after the last page, never after page 1. Do NOT retype or summarize the payload - render it
from the file that will actually be sent, so what the user approves is byte-for-byte what posts.

Show the preview page by page, as your own message text - tool output does not reach the
user's screen:

```bash
python3 ~/.claude/pr-review/pr-review-anchor.py preview "$SCRATCH/pr-review/pr$NUMBER/payload.json" --page 1
```

Its first line says `page 1/M`. Paste each page verbatim into a message of its own (copy the
tool output; never retype or summarize it), fetch the next with `--page 2`, and continue to
page M. Only after the last page, ask for approval once. Also give the path of `preview.txt`:
`preview` always writes the whole preview to `$SCRATCH/pr-review/pr$NUMBER/preview.txt`, beside
`payload.json`, and prints no line saying so.

`preview` renders whichever artifact the file holds. A Reviewable one shows the framing comment,
under a `framing disposition:` line, and then each discussion as
`DISCUSSION n/N - <path>:<line> [<disposition>] - #<ids>`. The disposition
is part of what is being approved: `blocking` (raised only for a `blocker`) holds the merge until the
raiser stands down, `discussing` clears when the author responds, and `informing` asks nothing. On
Reviewable the preview is also the only place the user can read the review before it publishes:
drafts are scoped to the identity that wrote them, so the agent's drafts never appear in the
user's own Reviewable view.

Show every page, **every time**, however long it runs. Never abbreviate it, never show only the
first few comments, never replace a comment body with a description of it. A user approving a
summary is approving text they have not read, which then posts under their own GitHub account. If
the output is long, that is the correct amount of output. What is never acceptable is a
hand-written précis standing in for the preview.

Before the first page, relay the `payload.stderr` lines from above as message text, and state the
head SHA the anchors were resolved against (`$NOW`, recorded as `anchored_sha`).

Honor any constraints in $ARGUMENTS - for example "do not say whether anything is blocking".

## 4. Post

Take the branch for the backend settled in step 1.5. Neither starts before the approval in step 3,
and both re-read the head before anything is sent.

### GitHub

Only after approval. **Re-read the head first** - approval takes human time, and a rebase landing
in that window is exactly what leaves the anchors stale:

```bash
NOW=$(GH_REPO="$REPO" gh pr view "$NUMBER" --json headRefOid --jq .headRefOid)
[ "$NOW" = "<ledger anchored_sha>" ] || { echo "head moved to $NOW - go back to step 2"; exit 1; }
```

Compare against the ledger's `anchored_sha` (written by step 2's `reanchor --head`), not
`head_sha`: the anchors in the payload were resolved at `anchored_sha`. Do not skip this because
step 2 already re-anchored. Step 2 runs before the approval pause; this runs after it. On a
mismatch, return to step 2 **and** re-run the step 2 claim re-verification - a restack can fix
the very things the body asserts are broken.

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

### Reviewable

Every call below takes `owner`, `repo` and `prNumber` as separate arguments, not the `owner/repo`
string the ledger carries: split `$REPO` on the `/` and pass `$NUMBER` as `prNumber`. The steps are
lettered R1-R6 so that "step 2" still means the re-anchor step above.

`payload.json` holds a `framing` comment and a `discussions[]` array, each discussion carrying
`path`, `line`, `markdownBody`, `disposition` and the `item_ids` it publishes. It deliberately holds
no Reviewable file key and no revision: those are opaque ids only the live API can supply, so they
are resolved here, after approval, and checked, because they are the one part of the review nobody
saw in the preview.

**R1. Re-read the head, on both sides.** Run the same check as the GitHub branch first:

```bash
NOW=$(GH_REPO="$REPO" gh pr view "$NUMBER" --json headRefOid --jq .headRefOid)
[ "$NOW" = "<ledger anchored_sha>" ] || { echo "head moved to $NOW - go back to step 2"; exit 1; }
```

Then `mcp__reviewable__review_revisions_list`, which returns revisions oldest to newest, each with a
`key`, a `headCommitSha` and a `baseCommitSha`. The LAST entry's `headCommitSha` must also equal the
ledger's `anchored_sha`. If GitHub matches and Reviewable does not, Reviewable has not yet recorded
a revision for this head. Call `mcp__reviewable__review_revisions_list` once more, straight away -
no sleep, no `ScheduleWakeup`, no loop. If the last entry still differs, **stop**: tell the user
"GitHub head is <NOW> but Reviewable's newest revision is <its headCommitSha>; Reviewable has not
recorded this head yet", post nothing, and ask them to re-run once Reviewable has caught up. Never
attach to the older revision. Nothing further
down this branch rejects a stale anchor for you. GitHub's wholesale rejection is blunt, but it is a
guard; Reviewable attaches a comment to whatever revision and line it is handed, so a finding against
superseded code posts quietly. Carry that `headCommitSha` forward as the `commitSha` of every
discussion in R4.

**R2. Resolve every discussion's `path` to a Reviewable file key.**
`mcp__reviewable__review_files_list` returns one entry per file with a `key` and a `path`. Build the
map from it, look up each `discussions[]` entry's `path`, and **check that the entry you matched
carries back exactly the path you searched for.** A wrong key does not fail - it posts the comment
to another file, on a line number that will resolve to something. The listing also holds virtual
entries (`-commits`, whose `path` is `-- commits`, marked `virtual: true`), so match the full path
exactly, never by containment.

**A path with no entry is fatal: report it and stop.** Do not fall back to a near match and do not
post the rest without it. `payload` has already moved into the framing comment every finding whose
file `reanchor` found outside the diff, so a discussion whose path Reviewable does not list means
the two disagree about what this PR changes: the head moved or a file was renamed since step 2, or
the ledger was never re-anchored and carries no `file_in_diff`. Say which files are missing, then
go back to step 2 and build and approve the payload again in step 3 - not a review published with
the finding that noticed it quietly missing.

**R3. Enumerate the identity's drafts before creating anything:**
`mcp__reviewable__review_discussions_list` with `query` `+draft`.

Nothing in this run has been created yet, so nothing this returns is yours. **That does not make it
abandoned.** Drafts belong to the Reviewable *identity*, not to the session, and every agent session
shares that identity - so a draft here may be a posting run that died between creating and
publishing, a different review, or a session working **right now**, one call away from publishing it
itself. Nothing in a draft says which session made it, so report the possibilities as
possibilities and never state which one it is. A `-top` key is the identity's single top-level
draft - the slot a review-level comment occupies, which R4's framing comment will need.

The listing gives each draft a `key` and a `resource` URI and nothing else; read each draft's
`resource` over the same MCP connection for its path, line and body.

This query also sees less than the publish sends: `+draft` names comment drafts only, so a staged
disposition change, acknowledgement, dismissal or file mark is not here at all. R5's
`review_state` is the first place those surface.

`mcp__reviewable__review_publish` sends every pending draft the identity owns and there is no
publish-one call, so a stray draft would go out attached to this review, mirrored to GitHub under
the user's own name. The user cannot catch it either: the agent's drafts are invisible to them in
Reviewable. So **report exactly what is there** - key, path, line and the opening line of each body -
and **ASK before deleting anything.** Delete only on the user's word, with
`mcp__reviewable__review_discussions_delete`, one `discussionKey` per call. Never publish over a
draft you did not create and never delete one unasked: a concurrent session's staged work is as
unrecoverable as an abandoned run's, and likelier to be missed.

**Then list `+draft` again, and create nothing until it comes back empty.** After the user has
answered and every deletion they approved is done, run the same query once more. If any draft is
still listed - a kept one, one whose delete did not take, one that arrived meanwhile, and a `-top`
draft included - **stop before R4**: report each remaining key, tell the user to publish, delete or
otherwise settle those drafts first, and ask them to re-run. A kept comment draft is an extra that R5 always stops on, after this run has already created its whole
review as drafts. A kept `-top` is worse: the framing comment needs that single slot, and R5
cannot tell two `-top` drafts apart by key, so the check has to happen here, before anything is
created.

**R4. Create the discussions, framing comment first.** The framing comment is a
`mcp__reviewable__review_discussions_create` call with **no `location`** - that absence is what makes
it review-level:

```
markdownBody: <framing.markdownBody, verbatim>
disposition:  <framing.disposition>
```

It goes first so that a run failing partway leaves the review's context standing rather than
findings with nothing explaining them. Then one call per `discussions[]` entry, in the order the
array gives - the order the preview showed:

```
markdownBody: <markdownBody, verbatim>
disposition:  <disposition>
location:
  file:     { key: <key from R2>, path: <path> }
  line:     <line>
  revision: { commitSha: <headCommitSha from R1> }
```

`markdownBody` posts **verbatim** - any rewrapping, added heading or signature makes what posts
differ from what was approved. `payload` has already put `_(AI-assisted, with my guidance)_` in the
framing body exactly once; do not add it to a discussion. `disposition` comes from the payload and is
not yours to revise.

Record each created discussion's key against the `item_ids` the payload gave it. That map is what
R5 reconciles against and what a partial failure is reported from - without it, a failed run leaves
drafts nobody can name. Take the key from the create call's own response; if a response names no
key, stop and report rather than guessing which new draft is which from a later listing.

**If any create fails, do NOT publish.** Everything that succeeded is an unpublished draft, and
`review_publish` would send that half-review as though it were the whole one.

**A failed create may still have landed.** An error Reviewable returned means it did not; a timeout
or a lost response may be a draft Reviewable made whose key never came back, and from here the two
look alike. So after ANY failed create, before retrying anything, list `+draft` again. R3 left the
listing empty, so every key in it that you have not recorded is unexplained. Read each unexplained
key's `resource`. One whose path and line are the failed site's (no location, for the framing
comment) and whose body is the failed call's `markdownBody` exactly is this run's own: record its
key against that site's `item_ids` as if the create had returned it, and do not create that site
again. Retry only the sites with no such draft. Any unexplained key that does not match - or whose
resource does not show a path, line and body to match against - is not yours: stop, and report it
as R5 would.

Then report which finding ids landed and which did not, and either retry the ones still missing or
delete this run's drafts and start R4 again. Delete **only the keys you recorded**, never everything
`+draft` returns: the rest is not this run's to clean up, and some of it may belong to a session
still working.

This check is not the GitHub branch's retry rule carried over. There, a 5xx may be a review the
author can already see. Here nothing reaches the author until `review_publish`, so a lost response
can only leave a duplicate draft, and the listing finds it before a retry makes a second one.

**R5. Reconcile before sending.** Two checks, both against what R4 recorded:

- `mcp__reviewable__review_discussions_list` with `query` `+draft` must list **exactly** the keys
  you recorded - no more, no fewer.
- `mcp__reviewable__review_state` must show `drafts.comments` equal to the number you created (the
  framing comment plus one per `discussions[]` entry), and `drafts.acknowledgements`,
  `drafts.dismissals`, `drafts.dispositionChanges` and `drafts.marks` all 0.

Two checks and not one because they see different things: `+draft` enumerates comment drafts by
key, while `review_state.drafts` also counts the kinds of pending change `+draft` cannot name, every
one of which `review_publish` also sends and none of which this review created. Anything extra in
either means something appeared that this run did not make - most likely the concurrent session R3
could not rule out - so **stop and report, do not publish**, and treat it as live work until the
user says otherwise. One extra is reported differently, though the run still stops: a draft whose
path, line and body match a create that failed in this run is most likely this run's own
duplicate from a lost response, so say so - and still ask before deleting it. Anything missing means a create did not land despite appearing
to.

On either stop, also report this run's own drafts: every key R4 recorded, with its finding ids.
Say that they remain unpublished on the shared identity and that the next publish by any session
would send them along with anything else pending, and ask whether to delete them or leave them for
now - leaving them is a fair answer if the user means to settle the extras and publish. Ask about
this run's keys on their own, never bundled with the extras. Delete only recorded keys, one
`mcp__reviewable__review_discussions_delete` per key and only on the user's word; never delete a
key that `+draft` or `review_state` showed and this run did not create.

**R6. Publish once, then report links.** `mcp__reviewable__review_publish`, with `publishOnPush`
left off. It is all-or-nothing for the identity, so there is nothing to batch and nothing to repeat.
It returns the review state: confirm from that response that `drafts.comments` is 0 rather than
calling `review_state` again to repeat what the server just said. A publish that leaves drafts
behind has not posted the whole review - say so rather than reporting it as posted.

Then report every finding as a link, since a discussion key is not something anyone can click.
`mcp__reviewable__review_discussions_list` with no `query` returns the shared list, each entry a
`key` and a `resource` URI (`reviewable://reviews/<owner>/<repo>/<prNumber>/discussions/<key>`), and
no browser address. **Read ONE of them** - any key R4 recorded - over the same MCP connection:

- If it carries a `url` ending with `#` followed by **exactly** the key you asked for, take
  everything before that final `#` as this review's URL and build the rest as
  `<review url>#<key>`, one per recorded key. That is the server's own answer for this very review,
  reused, with the one relationship the reuse depends on checked on the spot against a key you
  chose - one read instead of one per discussion.
- If it carries no `url`, or one that does not end with `#<that key>`, the relationship does not hold
  here: **read every recorded key's resource individually** and take each link from its own `url`. A
  wrong assumption about link shape would mis-link every finding at once, so one mismatch retires the
  shortcut for the whole run.
- On either path, report any finding whose link you still do not have with no link, and say the link
  was unavailable.

**Never construct a hostname.** Reviewable runs on enterprise installs as well as reviewable.io, so
every character of every link must have come back from this review's own server in this run. An id
with no link is a small gap; a fabricated link reads exactly like a real one and fails in a browser.

Report the number of discussions published, the head SHA they were attached to, and one line per
discussion:

```
#12, #15  src/renderer/foo.component.tsx:88  [discussion](<that discussion's url>)
```

Then go to step 5.

## 5. Tear down

```bash
~/.claude/pr-review/pr-review-wt.sh remove-refuters "$SCRATCH" "$NUMBER"
~/.claude/pr-review/pr-review-wt.sh remove "$WT"
```

`remove-refuters` removes any refuter worktree (`wt-pr<n>-r*`) `/pr-review` S5 left behind.

`remove` deletes the worktree unconditionally - unlike `reap`, it does **not** check for
uncommitted or untracked changes first. If there is working-tree state in `$WT` worth keeping,
save it before calling `remove`. It refuses, with an `error` line on stderr and a non-zero exit,
a path that is not a linked worktree, and on Windows a worktree holding a link it could not
unlink (`error <path> links-remain`); report either and leave the directory alone.

Report the posted review URL (on Reviewable, the per-discussion links from R6) and confirm the
worktree is gone.
