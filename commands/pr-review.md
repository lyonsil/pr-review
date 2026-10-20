---
description: Deep PR review - generate, split, dedupe, cluster, and pressure-test findings into a triage table
argument-hint: <PR-url | PR-number | .> [extra instructions]
---

Run the review pipeline for: $ARGUMENTS

Work through these stages in order. Do not skip ahead to posting - this command stops at the
triage table.

Tool output does not reach the user's screen - only your own message text does. Wherever this
command says to report, relay, show or name something a tool printed, put it in your message
text, copied verbatim where it says so.

## S0. Setup

1. Resolve this session's scratchpad directory. Every path below hangs off it:

   ```bash
   SCRATCH=$("${CLAUDE_PLUGIN_ROOT}/pr-review-wt.sh" scratchpad)
   ```

   There is **no `$CLAUDE_SCRATCHPAD` environment variable** in this harness - do not reference
   one. The subcommand honors it if it is ever set, and otherwise derives the path from
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
2. Run `"${CLAUDE_PLUGIN_ROOT}/pr-review-wt.sh" reap` and report any `reaped`, `orphaned`, or
   `stranded-branch` lines. A `kept ... dirty-N-changes` line is normal, not an error: the reaper
   never removes a worktree with uncommitted or untracked changes, and never deletes branches -
   it only reports them. `kept ... current-session` / `kept ... locked-by-live-pid-N` lines mean
   exactly what they say and need no action. `kept ... not-a-review-worktree` lines are suppressed
   by default (there is one per worktree the reaper is not responsible for, the repo's own main
   worktree included, and in a real repo they bury the lines that matter); pass `reap -v` if you
   need to see them; `reap` rejects any other argument with a usage line and a non-zero exit, so a
   mistyped flag fails rather than silently running non-verbose. Report any `error ...` line:
   `error <wt> remove-failed` means git could not remove that worktree, and on Windows
   `error <wt> links-remain` means a link inside it could not be unlinked, so it was left in place
   rather than deleted through the link. Both leave the worktree for the user to inspect.
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
   BASE_TIP=$(git rev-parse FETCH_HEAD)
   MERGE_BASE=$(git merge-base "$HEAD_SHA" "$BASE_TIP")
   ```

   Capture `HEAD_SHA` (step 4) **before** this fetch: `FETCH_HEAD` is a single per-checkout ref and
   this second fetch overwrites it. For `.`, `BASE_REF` is already a local remote-tracking branch,
   so `BASE_TIP=$(git rev-parse "origin/$BASE_REF")` and
   `MERGE_BASE=$(git merge-base HEAD "$BASE_TIP")` are enough.

   Store `MERGE_BASE` in the draft ledger as `merge_base` (S4). `brief` hands it to every refuter
   in S5 - establishing what is pre-existing versus introduced is central to their job, and they
   need the SHA to check it.

   **Check whether the base was rebased.** `main` moving ahead of this branch is the normal case and
   needs nothing. A rebased base shows up as commits the base already carries under a different
   SHA:

   ```bash
   git cherry "$BASE_TIP" "$HEAD_SHA" | grep -q '^-' && echo "base was rebased - derive the fork point"
   ```

   No output (exit status 1 from `grep -q`) is that normal case. When it prints, the merge-base
   sits below the entire base stack and the diff wrongly contains another PR's work - GitHub's own
   Files tab shows the same thing, so the inflation is easy to mistake for the real scope. Find the
   real fork point in the full `git cherry "$BASE_TIP" "$HEAD_SHA"` output: the `+` commits are the ones with no patch-equivalent in the base, and the
   review scope starts at the parent of the first `+` commit that belongs to THIS PR's work (read
   the subjects - the leading `+` commits are often rebase leftovers of the base's own history).
   Confirm the range with a diffstat before accepting it, and state the scope explicitly when you
   relay the S6 report and in the review body. Store that fork point as the ledger's `merge_base`
   in place of `MERGE_BASE`: it is the review base below, and `merge_base` is what `brief` hands
   the refuters.

   **This yields two different bases, and they are not interchangeable:**

   | | which SHA | used for |
   | --- | --- | --- |
   | review base | the fork point you derived | what S5 refuters diff against - "did THIS PR introduce it" |
   | posting base | GitHub's merge-base | what can anchor - GitHub accepts comments only on lines in ITS diff |

   The posting base is a superset, so a finding can sit outside the review scope and still be
   postable. `on_changed_line` is judged against the **posting** base, never the review base: that
   is why the diff S6's `changed-lines` reads is `gh pr diff`, not a diff from the fork point.
6. Create the worktree and capture the path with a **direct command substitution** - never through
   a pipe, and never wrapped in `bash -c`:

   ```bash
   WT=$("${CLAUDE_PLUGIN_ROOT}/pr-review-wt.sh" create "$SCRATCH/wt-pr<n>" "$HEAD_SHA")
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
7. Snapshot the primary checkout so S6 can prove nothing touched it:
   `"${CLAUDE_PLUGIN_ROOT}/pr-review-wt.sh" primary-status "$WT" > "$SCRATCH/primary.before"`
8. Link dependencies now, not in S5: `"${CLAUDE_PLUGIN_ROOT}/pr-review-wt.sh" link-deps "$WT" "$MERGE_BASE"`.
   Record the `linked ...` / `declined ...` line. It links the primary checkout's installed
   `node_modules` entries (per package, for npm, pnpm, yarn and bun), with workspace-package links
   pointed at `$WT` so tests run the head under review, only when no tracked lockfile differs between
   `MERGE_BASE` and the head - a stale dependency tree would make "I ran it and it passed" worse
   than no answer. The line goes to every refuter through the brief context file (S1).
9. Find this repo's real commands - read the root `package.json` scripts, `nx.json`/`project.json`,
   `*.csproj`/`*.sln`, `Makefile` - and record the single-file test, single-project typecheck and
   single-file lint command. Never assume `npm run ...` exists.

## S1. Generate

Snapshot the worktree registry after creating `$WT` and before invoking the skill:

```bash
mkdir -p "$SCRATCH"
git worktree list --porcelain > "$SCRATCH/worktrees.before"
```

**Two different skills are named `code-review`. Invoke the BUILT-IN one, never the plugin.**

- Built-in, the one to use: skill name exactly `code-review`. It takes a level
  (`low`/`medium`/`high`/`max`/`ultra`) and honors issue-cap instructions.
- Plugin, never use: skill name `code-review:code-review`. It has no level concept, always spawns
  exactly 5 Sonnet agents, ignores cap instructions, and **posts a comment to the PR on its own** -
  which would bypass this pipeline's triage gate and publish unreviewed findings under the user's
  account. Its description ("Code review a pull request") reads like a better match for this task
  than the built-in's. It is not. Do not select it.

Invoke it with the level as the FIRST token of the arguments. The built-in reuses the last level
the user typed when none is given, so an unspecified level silently downgrades the review:

```
Skill(skill="code-review", args="high <PR-url-or-target> - <the instructions below>")
```

Tell it explicitly:

- Analyze the changes in the existing worktree at `$WT`. **Do not create a worktree of your own.**
- Raise the per-subagent issue cap to 16 (from 8) and the final-report cap to 30 (from 15).
- Present the final report as a numbered list.
- Write each finding to the contract in "How every finding is written" below - a one-sentence
  `claim` in observable terms, then `**What happens:**` / `**Why it matters:**` / `**Fix:**`.
  Quote the section into the arguments rather than referring to it; the skill cannot read this
  file. Findings that arrive in that shape need no rewriting in S2.

**Verify the right one ran before continuing.** A `high` review spawns well more than 5 subagents and
posts nothing. If you see exactly 5 parallel Sonnet reviewers, or anything gets posted to the PR at
this stage, the plugin ran: STOP, tell the user, and say whether a comment was posted so they can
delete it. Do not proceed to S2 on a plugin-produced report - it is capped and filtered differently
and its findings will not carry the fields the ledger needs.

When it finishes, snapshot again and report any added worktree under `$SCRATCH` to the user:

```bash
git worktree list --porcelain > "$SCRATCH/worktrees.after"
comm -13 <(grep '^worktree ' "$SCRATCH/worktrees.before" | sort) \
         <(grep '^worktree ' "$SCRATCH/worktrees.after" | sort) | grep -F "$SCRATCH" || true
```

That the skill honors "do not create a worktree of your own" is an assumption, not a guarantee.
Any added worktree is one this pipeline does not own and will not tear down in step 5 of
`/pr-review-post`; if the command prints a path, **say so explicitly** and name it so the user can
decide whether to remove it, rather than letting an unowned worktree leak silently.

Any extra instructions in $ARGUMENTS ride along verbatim - for example seeding extra findings
from a file and placing them above the cap.

### While the generator runs

The skill runs in the background; a task-notification arrives when it finishes. Until then do
preparation only:

- the S3 comment fetch,
- the obligation table (S5),
- the posting diff: `mkdir -p "$SCRATCH/pr-review/pr<n>" && GH_REPO="$REPO" gh pr diff <n> > "$SCRATCH/pr-review/pr<n>/head.diff"`
  (for a `.` target: `git -C "$WT" diff "$MERGE_BASE" HEAD > "$SCRATCH/pr-review/prlocal/head.diff"`),
- the brief context file `$SCRATCH/pr-review/pr<n>/ctx.json`, written with the Write tool:
  `{"link_deps": "...", "commands": "...", "obligations": "<the table>", "claude_md_paths":
  ["$WT/CLAUDE.md", ...], "scratch": "$SCRATCH"}` - `link_deps` is the S0 step 8 line, `commands`
  the S0 step 9 commands, and every path literal and absolute.

Then end your turn. Do not review the code yourself, do not run the test suite, and never use
`ScheduleWakeup`, `/loop`, `sleep`, or a placeholder command to wait.

## S2. Split and number

One item = one defect, with one fix, at one site. Break every composite item apart.

Then number them **globally, 1..N, ordered by severity descending then file path**. Severity
vocabulary is exactly `blocker`, `high`, `medium`, `low`.

Ids assigned here are stable until S6's `renumber`. Refer to another finding only as `{{#id}}`.

The generator does emit values outside that set - `medium-high`, `low-medium`. Normalize each to
the **lower** neighbor (`medium-high` -> `medium`, `low-medium` -> `low`): the hyphenated form is
a hedge, and a hedge should not inflate a severity on its way to an author. Record the normalized
value as `severity_in` and, when you relay the S6 report, say which ones you normalized.

**Letter suffixes are forbidden.** Never produce `2a` or `30g`: a split item's parts each get
their own integer id. `validate` (S6) rejects letter-suffixed, duplicate and non-contiguous ids.

## How every finding is written

The contract for the text that reaches the PR author - `claim`, `detail`, and the refuter's
`REASON`. It applies from S2 through S6, whatever stage produces the words.

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
- `**Why it matters:**` (always required, minor findings included) - the
  consequence, how often it is hit, and who hits it. A finding without this slot reads as one too
  minor to raise, so a minor finding says what it costs instead: "test-only, with no behavior
  effect", "maintenance only". `validate` (draft and full) rejects a missing or empty slot. A pressure-test's gating reason belongs here
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
   no refuters, no "the generator flagged", no severity revisions, no item numbers except as a
   `{{#id}}` token, which `payload` renders as `#n`. The comment header already carries a plain-English
   status - the prose neither repeats nor explains it.
5. **A fix must be complete under the rules the next review will apply.** Whatever the author
   writes to satisfy this finding gets reviewed in turn - by this pipeline again, or by a human
   reading the same rule files. A fix that works but omits an obligation those rules attach to it
   buys a second round for nothing: the `// CUSTOM:` annotation on a shadcn edit, the catalog entry
   for a new keyboard handler, every locale file for a new string, the test for a behavior change.
   Name each obligation the change triggers as part of the `Fix:` line. The repo's obligation
   table (S5) goes to every refuter in its brief.

### Worked example

```jsonc
"claim": "The Comments tab shows a stale scroll position when you switch back to it after navigating.",
"detail": "**What happens:** rc-dock keeps an inactive tab mounted but hidden with `display: none`. The effect at `use-bcv-sync-scroll.hook.ts:44` still fires on every `scrRef` change, but a display-none iframe has no layout, so the `scrollIntoView` at line 51 reads zero and silently no-ops. The request is lost rather than deferred.\n\n**Why it matters:** in Simple mode only one tab per stack is visible, so this is the common path, not an edge case.\n\n**Fix:** wrap the scroll in `useRunWhenVisible` (`lib/platform-bible-react/src/hooks/use-run-when-visible.hook.ts`) so repeats collapse into one catch-up that runs on activation."
```

## S3. Dedupe against what is already on the PR

Skip entirely for a `.` target, or if the PR has no comments.

Pull existing feedback (done while the generator runs, S1):

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

**Cluster carefully - S5 dispatches one refuter per cluster.** A cluster is no longer only a way to
present findings: it asserts that its items rest on the same premise, and the whole cluster is
verified by one agent in one context. Two items that merely look alike, or land in the same file
for unrelated reasons, are NOT a cluster - grouping them makes one agent's judgment on the first
color its answer on the rest. If you cannot write a `shared_fix` that is literally the same change
at each site, leave the items unclustered and let them get their own refuters.

First clear what an earlier run in this session left beside the ledger - `ctx.json` and
`head.diff` stay:

```bash
rm -rf "$SCRATCH/pr-review/pr<n>/briefs" "$SCRATCH/pr-review/pr<n>/verdicts" \
       "$SCRATCH/pr-review/pr<n>/edits"
```

Then write the draft ledger to `$SCRATCH/pr-review/pr<n>/ledger.json`, with the fields marked
`draft` below and nothing else. Match every field name exactly - a cluster **must** use `item_ids`
(not `items`, not `ids`), and every item's `cluster` must agree with the `item_ids` that list it.

Check the draft before any refuter sees it:

```bash
A="${CLAUDE_PLUGIN_ROOT}/pr-review-anchor.py"; L="$SCRATCH/pr-review/pr<n>/ledger.json"
python3 "$A" excerpt $L --worktree "$WT" && python3 "$A" validate $L --draft
```

`excerpt` fails on a `file` or `line`/`line_end` that does not exist in `$WT`; `validate --draft`
fails on a non-integer or duplicate id, a `severity_in` outside the vocabulary, a missing
`head_sha`/`merge_base`/`worktree`, or a `cluster` and `item_ids` that disagree. On any `error:`,
fix the draft JSON directly and re-run both. This is the last time you write ledger JSON directly:
once both pass, every field is written by a subcommand, and none is ever edited to clear a tool
error.

```jsonc
{
  "schema": 2,                                   // draft
  "target": {"kind": "pr", "repo": "<owner>/<repo>", "number": <n>},  // draft; kind "local", repo/number omitted, for a `.` review
  "head_sha": "<HEAD_SHA>",                      // draft
  "merge_base": "<MERGE_BASE>",                  // draft
  "worktree": "<WT>",                            // draft
  "caps": {"subagent": 16, "report": 30},        // draft; the caps actually used, per S1
  "anchored_sha": "<sha>",                       // reanchor --head, in /pr-review-post: the head the anchors were resolved against
  "items": [
    {
      "id": 1,                                   // draft; global 1..N, integer only, no letter suffixes
      "severity_in": "blocker|high|medium|low",  // draft; what the GENERATOR assigned, never overwritten
      "severity": "blocker|high|medium|low",     // draft (= severity_in); ingest writes the refuter's revision
      "cluster": "C3",                           // draft; or null - must match a "clusters[].id" below
      "file": "src/foo.ts",                      // draft
      "line": 142,                               // draft; changed-lines may move it onto the first changed line in the range
      "line_end": 148,                           // draft
      "claim": "one sentence in observable terms - see How every finding is written",  // draft
      "detail": "**What happens:** / **Why it matters:** / **Fix:** - same section",   // draft
      "evidence": ["src/bar.ts:88"],             // draft
      "origin": "code-review|seed:<path>",       // draft
      "dedupe": null,                            // draft; or {"prior_url": "...", "note": "..."} from S3
      "refuter_note": "...",                     // draft, optional; your guidance to this item's refuter - deleted by ingest, never posted
      "anchor_excerpt": "...",                   // excerpt: lines line..line_end read verbatim from $WT
      "on_changed_line": true,                   // ingest records the refuter's answer; changed-lines overwrites it from the diff
      "file_in_diff": true,                      // changed-lines, and reanchor in /pr-review-post: the diff names `file` at all, even with no hunk; missing counts as true
      "verdict": "CONFIRMED|DOWNGRADED|REFUTED|INCONCLUSIVE",  // ingest
      "verdict_reason": "...",                   // ingest
      "claim_verdict": "SOUND|REWRITTEN",        // ingest; absent on REFUTED
      "claim_reason": "...",                     // ingest
      "claim_original": "...",                   // ingest; the generator's claim before a CLAIM_REWRITE
      "fix_verdict": "SOUND|REWRITTEN|UNVERIFIABLE",  // ingest; absent on REFUTED
      "fix_reason": "...",                       // ingest
      "fix_original": "...",                     // ingest; the generator's Fix: text before a FIX_REWRITE
      "refuter_key": "C3",                       // ingest; which verdict file answered this item
      "unanswered": true,                        // ingest --inconclusive; the refuter never answered
      "fold_note": "folded|bookkeeping",         // edit, S6; for DOWNGRADED only
      "old_id": 7,                               // renumber; the stable S2 id
      "anchor_status": null,                     // reanchor: ok|moved|off-diff|dead
      "anchor_note": null                        // reanchor: its reason string
    }
  ],
  "clusters": [
    {"id": "C3", "title": "...", "shared_fix": "...", "item_ids": [1, 12, 27]}  // draft
  ]
}
```

Verdict fields are written by `ingest` at the end of S5, never by hand.

## S5. Pressure-test

For every refuter key - each cluster id, and `i<id>` for each unclustered item - render its brief:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/pr-review-anchor.py" brief <ledger> --key <KEY> \
  --context "$SCRATCH/pr-review/pr<n>/ctx.json"
```

It prints the brief's path. `brief` refuses a key that is not one cluster or one unclustered item,
an obligation row that cites no repo file, or a placeholder it could not fill. Guidance you want a
refuter to have goes in that item's `refuter_note` in the draft ledger - never in `claim` or
`detail`. The brief carries the refuter's whole contract - the three jobs, the verdict definitions,
the worktree rules and the reply format - from `refuter-brief.tmpl.md`; do not restate any of it in
the dispatch prompt.

Dispatch one Agent per key, `model: "sonnet"`, with exactly this prompt:
`Read <brief path> and follow it exactly.` Refuters always run on Sonnet. Run at most 15 at once;
launch the next as each finishes. Ignore task-notifications whose result says the report was
delivered as a message.

After dispatching, arm the watchdog - the only wakeup this command may use:
`ScheduleWakeup(delaySeconds=1200, prompt="pr-review watchdog: check refuter progress")`.
On waking, stat (never read) each outstanding refuter's output file. One unchanged for 15 minutes:
`TaskStop` it and dispatch it again once. A second stall: `TaskStop` it, do not dispatch it again,
and count it as done - its items are closed with `ingest --inconclusive` below. Re-arm only while
some refuter has neither written its verdict file nor stalled twice; once every refuter has done
one or the other, do not re-arm.

When every refuter has answered or stalled twice:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/pr-review-anchor.py" ingest <ledger>
"${CLAUDE_PLUGIN_ROOT}/pr-review-wt.sh" remove-refuters "$SCRATCH" <n>
```

`ingest` reads the dispatched set from `briefs/*.json` and the answers from `verdicts/<KEY>.txt`,
both beside the ledger, and applies every `CLAIM_REWRITE` and `FIX_REWRITE` itself. It refuses a
brief or reply from another draft (the run id each brief carries, echoed in the reply's `RUN:`
line) and an item answered by more than one refuter. It names every problem at once and changes
nothing if there is one. For each refuter it names, re-dispatch once with the error lines appended
to the prompt, and arm the watchdog again for the re-dispatched refuters exactly as above - the
earlier wakeup may already have stopped re-arming. If the same refuter fails again - or stalled
twice - close its items, then run `remove-refuters` as above:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/pr-review-anchor.py" ingest <ledger> --inconclusive <ids> \
  --reason "Not verified: the automated check did not complete."
```

`<ids>` is comma-separated (`4,9,17`). `--inconclusive` closes those items even when their reply
file exists but cannot be read. The reason is the item's `verdict_reason`, and on an INCONCLUSIVE
item that publishes under **What could not be checked**, so write it for the PR author: say what
was not verified, never "refuter", "stalled" or other review vocabulary - `validate` rejects it.
Use the sentence above for a stall or a reply that never parsed. Never edit the ledger to get
past `ingest`.

### The obligation table

Build this once, while the generator runs (S1), and put it in `ctx.json`'s `obligations`; `brief`
pastes it into every refuter's brief. It is what makes a fix survive the *next* review rather than
merely working.

Enumerate the rules this repo's reviewers actually read - `.claude/rules/**`, the standards table in
`CLAUDE.md`, and any `AGENTS.md` - and reduce them to a table of *triggers*: the shape of a change,
and what that shape obliges. Derive it per repo on every run rather than carrying one between runs:
these commands are used against more than one repository, the rule sets differ, and a stale table is
worse than none because it asserts obligations that no longer exist. A dozen rows is the right size;
it is a lookup, not a summary of the standards.

Every row's last cell names the repo file the obligation comes from. A preference of yours or of
the user's that no repo file states is not an obligation.

Shape it like this (the rows are illustrative - read the repo's own rules for the real ones):

| If the fix ... | it must also ... | rule |
| --- | --- | --- |
| edits a vendored component | carry the annotation that convention requires | `shadcn-discipline.md` |
| adds or changes a keyboard handler | update the shortcuts catalog | `keyboard-shortcuts-catalog.md` |
| adds user-facing strings | land in every locale file | `localization-locale-files.md` |
| changes behavior | come with a test | `testing/tdd-discipline.md` |
| suppresses a tool warning | justify why fixing the code is worse | `eslint-disable-discipline.md` |
| adds a comment | be forward-facing, no PR or ticket narration | `forward-facing-comments.md` |
| chooses among viable approaches | be recorded in the decisions log | `CLAUDE.md` |

A fix that satisfies the claim but not the table is the cheapest kind of review churn there is: the
author implements it, the next reviewer flags the omission, and a second round buys nothing that
this one could not have said.

## S6. Report

Run, in order, stopping on any `error:`:

```bash
set -e; A="${CLAUDE_PLUGIN_ROOT}/pr-review-anchor.py"; L=<ledger>
python3 "$A" excerpt $L --worktree "$WT"
python3 "$A" changed-lines $L --diff "$SCRATCH/pr-review/pr<n>/head.diff" --worktree "$WT"
```

`excerpt` copies each anchor verbatim from `$WT`, so an excerpt is never retyped; `changed-lines`
overwrites the refuters' `on_changed_line` from the posting diff, since a wrong `true` fails the
**entire** review POST. Relay every `moved` and `flipped` line it prints: a flip means a refuter
asserted something the diff contradicts, which is worth weighing against its other answers.

Each tool call runs in a fresh shell, so start every later block with the same
`A=...; L=...` line. Then the two judgment steps, each written back with `edit` (never
`python -c`, never a JSON edit). `edit` replaces the whole field with the file's contents, so
write the complete new text to a file under `$SCRATCH/pr-review/pr<n>/edits/` with the Write
tool, never under `/tmp`:

1. For each DOWNGRADED item, fold what its `verdict_reason` means for the author into
   `Why it matters:` (`python3 "$A" edit $L <id> --field detail --text-file <f>`), and record
   `python3 "$A" edit $L <id> --field fold_note --text-file <f>` with `folded` or `bookkeeping`. Neither
   `severity_in -> severity` nor the refuter's reason publishes, so when the reason changes what
   the author needs to know - the path is gated, unreachable, pre-existing, or intentional - fold it
   in, in the author's terms, so the severity it posts at reads as sensible rather than arbitrary.
   When it is only bookkeeping ("less severe than first rated"), leave `detail` alone and record
   `bookkeeping`.
2. The quantifier sweep:
   `grep -inP '(?<![\w-])(every|all|any|always|never|only|whole|entire|cannot|no [a-z]+)(?![\w-])'`
   over the claims - case-insensitive, since a claim often opens with `Every` or `No`. For each
   hit, cite the evidence or reword via `python3 "$A" edit $L <id> --field claim --text-file <f>`.
   Write each hit and its disposition into `$SCRATCH/pr-review/pr<n>/quantifiers.md`. To see
   each hit with its id:

   ```bash
   A="${CLAUDE_PLUGIN_ROOT}/pr-review-anchor.py"; L=<ledger>
   grep -P '^\s*"(id|claim)": ' $L \
     | grep -B1 -iP '^\s*"claim": .*(?<![\w-])(every|all|any|always|never|only|whole|entire|cannot|no [a-z]+)(?![\w-])'
   ```

   These are the stable S2 ids; `renumber` below prints the old -> new map (and records each
   item's `old_id`), so give both in `quantifiers.md`. The net
   is one-directional: overstatement needs a quantifier, understatement usually does not, so a
   clean sweep says nothing about understated claims - that half rests on `claim_verdict`.

Then:

```bash
set -e; A="${CLAUDE_PLUGIN_ROOT}/pr-review-anchor.py"; L=<ledger>
python3 "$A" renumber $L
python3 "$A" validate $L --worktree "$WT"
python3 "$A" reanchor $L --worktree "$WT" --diff "$SCRATCH/pr-review/pr<n>/head.diff" --dry-run
"${CLAUDE_PLUGIN_ROOT}/pr-review-wt.sh" primary-status "$WT" | diff "$SCRATCH/primary.before" -
```

On a `validate` error: an excerpt mismatch means re-run `excerpt`; a wording error (process
vocabulary in a `claim`, `detail` or published `verdict_reason`, a hedged `Fix:`, a claim over one
sentence) means `edit` that text; then re-run from `renumber`. Never delete a finding or hand-edit the ledger to get past it. `reanchor --dry-run`
writes nothing; it shows how each anchor will resolve at post time.

If the primary-status diff is non-empty, stop and show the user the changed paths; do not restore
anything without approval. Otherwise:

```bash
A="${CLAUDE_PLUGIN_ROOT}/pr-review-anchor.py"; L=<ledger>
python3 "$A" report $L
```

Relay `report`'s output in full as your own message text - tool output does not reach the
user's screen - copied verbatim, never retyped or summarized. Then, also as message text, the
quantifier dispositions from `quantifiers.md`, then the teardown one-liner in case the user
abandons the round: `"${CLAUDE_PLUGIN_ROOT}/pr-review-wt.sh" remove $WT`.
Then **stop**. Do not post anything.
