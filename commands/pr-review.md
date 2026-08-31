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
   establishing what is pre-existing versus introduced is central to their job, and they need the
   SHA to check it.
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

**Two different skills are named `code-review`. Invoke the BUILT-IN one, never the plugin.**

- Built-in, the one to use: skill name exactly `code-review`. It takes a level
  (`low`/`medium`/`high`/`max`/`ultra`) and honours issue-cap instructions.
- Plugin, never use: skill name `code-review:code-review`. It has no level concept, always spawns
  exactly 5 Sonnet agents, ignores cap instructions, and **posts a comment to the PR on its own** -
  which would bypass this pipeline's triage gate and publish unreviewed findings under the user's
  account. Its description ("Code review a pull request") reads like a better match for this task
  than the built-in's. It is not. Do not select it.

Invoke it with the level as the FIRST token of the arguments. The built-in reuses the last level
the user typed when none is given, so an unspecified level silently downgrades the review:

```
Skill(skill="code-review", args="max <PR-url-or-target> - <the instructions below>")
```

Tell it explicitly:

- Analyze the changes in the existing worktree at `$WT`. **Do not create a worktree of your own.**
- Raise the per-subagent issue cap to 16 (from 8) and the final-report cap to 30 (from 15).
- Present the final report as a numbered list.

**Verify the right one ran before continuing.** A `max` review spawns well more than 5 subagents and
posts nothing. If you see exactly 5 parallel Sonnet reviewers, or anything gets posted to the PR at
this stage, the plugin ran: STOP, tell the user, and say whether a comment was posted so they can
delete it. Do not proceed to S2 on a plugin-produced report - it is capped and filtered differently
and its findings will not carry the fields the ledger needs.

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
claim, its `detail` and `evidence`, its `file:line`, the worktree path `$WT`, `MERGE_BASE`, and the
relevant CLAUDE.md paths.

First make verification actually possible, so a refuter is never reduced to guessing:

```bash
~/.claude/pr-review/pr-review-wt.sh link-deps "$WT" "$MERGE_BASE"
```

It prints `linked <path>` or `declined <reason>`, and links the primary checkout's `node_modules`
only when no tracked lockfile differs between `MERGE_BASE` and the head - a stale dependency tree
would make "I ran it and it passed" worse than no answer. **Pass the result into every refuter
prompt**: on `linked`, say that `npm run typecheck`, `npm run lint` and `npm test` are available in
`$WT`; on `declined`, say so and name the reason. Never tell a refuter it cannot verify anything -
`git`, `grep` and the full history are always available, and most claims are settled with those.

### The refuter's job

A refuter's task is to reach the truth, not to win. Instruct each one:

> Establish whether this finding is true, and how much it matters. You are the last check before it
> reaches the PR author, so an unexamined "I could not tell" costs as much as a wrong answer.
>
> **Verify before you judge.** Work the ladder as far as the claim needs: read the anchored code
> and everything it calls; `git diff <MERGE_BASE> HEAD -- <file>` to see what this PR actually
> changed; `git log -S` / `git show <MERGE_BASE>:<file>` to establish what is pre-existing; grep for
> every producer and consumer before asserting a value can or cannot occur; and, if dependencies
> were linked, RUN the check - typecheck, lint, or the single test file that would settle it.
> Naming a command you did not run is not evidence.
>
> Report one of four verdicts:
>
> - `CONFIRMED` - the claim is true and matters as stated.
> - `DOWNGRADED` - the observation is true, but smaller than claimed: unreachable on any current
>   path, an instance of a pre-existing pattern the PR merely continues, intentional per an
>   adjacent comment or ADR, or simply less severe than first rated. **This is the right verdict
>   whenever you find yourself writing "the mechanics are right, but ..." - say what survives, and
>   downgrade it. Do not refute a finding you have just agreed with.**
> - `REFUTED` - the claim itself is false. Reserved for a claim you have positively disproved:
>   the code does not do what the finding says, or the consequence provably cannot follow from it.
> - `INCONCLUSIVE` - you could not settle it. Say exactly what you could not check and what would
>   settle it. Use this rather than defaulting to a verdict you cannot support.
>
> Give `file:line` evidence for the verdict either way. You may revise the severity; give your
> reason. Also report whether the anchor sits on a line this PR changed.

Reply format, exactly and with nothing before it:

```
VERDICT: CONFIRMED|DOWNGRADED|REFUTED|INCONCLUSIVE
SEVERITY: blocker|high|medium|low
ON_CHANGED_LINE: true|false
REASON: <3-6 sentences with file:line evidence>
```

**Do not refute on grounds that are about posting rather than truth.** Whether the anchor sits on a
changed line is recorded in `on_changed_line` and enforced by `payload` on its own; it is never a
reason for a verdict. Neither is "a linter or CI would catch it" - that speaks to who reports the
problem, not whether it exists.

### Model

Refuters run on the **same tier as the generator by default**. A refuter overturns a finding the
generator raised, and a weaker model's characteristic failure is exactly the one that matters here:
unable to trace a path, it reads that as absence of a path.

A cheaper model is allowed only for an item that is **single-file, single-site, and settled by
reading that file** - a localization string, a TSDoc omission, a comment that misdescribes adjacent
code, a catalog entry. Such a refuter may not return `REFUTED` on a reachability argument: any
verdict resting on "nothing anywhere in the repo produces this" requires a whole-repo search, so
escalate it to the default tier instead. Say in the report which items ran on a cheaper model.

### Recording the result

Record `verdict`, `verdict_reason`, and `on_changed_line` per item. Record the generator's severity
as `severity_in` and the refuter's as `severity` - **never overwrite `severity_in`**. A refuter that
downgrades and refutes in one step otherwise leaves a triage table where every dropped finding reads
as low-severity noise, hiding that a `high` was dropped.

**Every item must end S5 with one of the four verdicts.** A refuter that returns prose without a
parseable `VERDICT:` line has not answered - re-dispatch it once with the format restated, and if it
still will not answer, record `INCONCLUSIVE` with that as the reason. Never leave a placeholder:
`payload`'s exclusion is `REFUTED`-only, so a finding left in any other state posts to the PR as
though it had been confirmed. `validate` rejects the ledger for this, but do not rely on it to
catch what this step should not produce.

Any item left `INCONCLUSIVE` after one escalation must be **named to the user in the S6 report**,
with what could not be checked. Do not close out the round silently holding an unanswered finding.

Then apply any severity revisions and **re-sort and renumber** so the 1..N ordering still holds.

Note for later: `REFUTED` is sticky downstream but not final. `/pr-review-post`'s
`payload --include-refuted <ids>` lets the user overrule specific REFUTED ids, per id, at post time -
without touching the ledger's `verdict`/`verdict_reason`, so the refuter's recorded reasoning stays
intact. `DOWNGRADED` and `INCONCLUSIVE` need no overrule: they are carried into the review body
automatically.

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
         "severity": "blocker|high|medium|low",   // AFTER the S5 refuter's revision
         "severity_in": "blocker|high|medium|low",// what the GENERATOR assigned; never overwritten
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
         "verdict": "CONFIRMED|DOWNGRADED|REFUTED|INCONCLUSIVE",  // no placeholders; see S5
         "verdict_reason": "the S5 refuter's reasoning, whichever verdict it reached",
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
   duplicate ids, wrong severity ordering, a cluster/item referencing an unknown id, a missing or
   unknown `severity_in`, an empty `verdict_reason`, a `verdict` outside the four terminal values -
   a leftover `PENDING` included - or a non-dict ledger root are all rejected this way; a wrong
   cluster key name like `items` instead of `item_ids` is NOT, since it just reads as an empty
   cluster). Fix the ledger and re-run
   validate; never report a ledger as ready while validate is failing.
3. Write `report.md` alongside it.
4. Print, in this order. **This goes to a terminal, not to GitHub. Emit plain text and
   GitHub-flavoured markdown only - never raw HTML.** No `<details>`, `<summary>`, `<br>`, or entities:
   the terminal prints those literally as tags instead of rendering them. Use a plain heading and
   an indented or bulleted list wherever you want a section to read as secondary.

   - the triage table: id, severity, cluster, `file:line`, one-line claim. Where the refuter
     changed the severity, show it as `<severity_in> -> <severity>`, never just the survivor - a
     table that prints only the revised value hides that a `high` was downgraded on its way out.
   - a `Verdict summary` line: counts of CONFIRMED / DOWNGRADED / REFUTED / INCONCLUSIVE, and the
     refutation rate as `REFUTED / total`. State it plainly; it is the number that tells you
     whether the pressure-test is calibrated.
   - a `Carried in the review body` section: every DOWNGRADED item - id, `<severity_in> -> <severity>`,
     claim, and the refuter's reason. These post automatically with `all`; they are not dropped and
     need no `--include-refuted`.
   - an `Unresolved` section: every INCONCLUSIVE item, with what the refuter could not check and
     what would settle it. **Never omit this section when it is non-empty**, and say plainly that
     these need a human judgement.
   - a `Refuted (not posted unless you name them)` section: each id, its claim, and the refuter's
     reason, so they can be overruled
   - a `Previously reported` section: each id with a link to the existing comment
   - the count of items not on a changed line (body-only, cannot be posted inline)
   - which items, if any, ran their refuter on a cheaper model (S5)
   - a `Files` section giving the absolute paths, so they can be opened directly:
     ```
     Ledger:   <SCRATCH>/pr-review/pr<n>/ledger.json
     Report:   <SCRATCH>/pr-review/pr<n>/report.md
     Worktree: $WT
     ```
   - a `To post` section giving ready-to-run commands, with the real counts substituted:

     ```
     /pr-review-post all          # N confirmed inline + M carried in the body; refuted excluded
     /pr-review-post 1 3 7        # a chosen subset
     /pr-review-post all --include-refuted 4   # also post refuted #4, per id
     ```

     Say explicitly what `all` covers: CONFIRMED findings post inline; DOWNGRADED and INCONCLUSIVE
     findings are carried in the review body automatically; REFUTED items are excluded unless named
     with `--include-refuted`; dead anchors are demoted to the body and never posted inline.
   - the teardown one-liner, if abandoning the round:
     `~/.claude/pr-review/pr-review-wt.sh remove $WT`

Then **stop**. Do not post anything.
