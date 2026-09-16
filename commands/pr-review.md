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

   **Check for a stacked PR whose base was rebased before you trust that scope.** If the base
   branch has been rebased since this branch forked, the merge-base sits below the entire base
   stack and the diff wrongly contains another PR's work - GitHub's own Files tab shows the same
   thing, so the inflation is easy to mistake for the real scope:

   ```bash
   BASE_TIP=$(git rev-parse FETCH_HEAD)
   git merge-base --is-ancestor "$BASE_TIP" "$HEAD_SHA" \
     || echo "base was rebased - MERGE_BASE spans the base stack, derive the fork point"
   ```

   When it is not an ancestor, find the real fork point with `git cherry "$BASE_TIP" "$HEAD_SHA"`:
   the `+` commits are the ones with no patch-equivalent in the base, and the review scope starts
   at the parent of the first `+` commit that belongs to THIS PR's work (read the subjects - the
   leading `+` commits are often rebase leftovers of the base's own history). Confirm the range
   with a diffstat before accepting it, and state the scope explicitly in S6 and in the review body.

   **This yields two different bases, and they are not interchangeable:**

   | | which SHA | used for |
   | --- | --- | --- |
   | review base | the fork point you derived | what S5 refuters diff against - "did THIS PR introduce it" |
   | posting base | GitHub's merge-base | what can anchor - GitHub accepts comments only on lines in ITS diff |

   The posting base is a superset, so a finding can sit outside the review scope and still be
   postable. Judge `on_changed_line` against the **posting** base (S6), never the review base.
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
- Write each finding to the contract in "How every finding is written" below - a one-sentence
  `claim` in observable terms, then `**What happens:**` / `**Why it matters:**` / `**Fix:**`.
  Quote the section into the arguments rather than referring to it; the skill cannot read this
  file. Findings that arrive in that shape need no rewriting in S2.

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

The generator does emit values outside that set - `medium-high`, `low-medium`. Normalize each to
the **lower** neighbour (`medium-high` -> `medium`, `low-medium` -> `low`): the hyphenated form is
a hedge, and a hedge should not inflate a severity on its way to an author. Record the normalized
value as `severity_in` and say in the S6 report that you normalized.

**Letter suffixes are forbidden.** Never produce `2a` or `30g`. If an item splits, every
downstream number shifts. The validator (S6 step 2) enforces this and will reject the ledger
otherwise - along with duplicate ids, non-contiguous ids, and wrong severity ordering.

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

### `detail` - three labelled slots, always in this order

- `**What happens:**` (required) - the causal chain one step at a time, each step carrying the
  `file:line` where it happens, naming the real symbols. The mechanism goes here.
- `**Why it matters:**` (required unless the severity is self-evident from the claim) - the
  consequence, how often it is hit, and who hits it. A pressure-test's gating reason belongs here
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
   no refuters, no "the generator flagged", no severity revisions, no item numbers beyond the `#n`
   cross-references `payload` adds itself. The comment header already carries a plain-English
   status - the prose neither repeats nor explains it.
5. **A fix must be complete under the rules the next review will apply.** Whatever the author
   writes to satisfy this finding gets reviewed in turn - by this pipeline again, or by a human
   reading the same rule files. A fix that works but omits an obligation those rules attach to it
   buys a second round for nothing: the `// CUSTOM:` annotation on a shadcn edit, the catalog entry
   for a new keyboard handler, every locale file for a new string, the test for a behaviour change.
   Name each obligation the change triggers as part of the `Fix:` line. S5 derives the repo's
   obligation table and hands it to every refuter.

### Worked example

```jsonc
"claim": "The Comments tab shows a stale scroll position when you switch back to it after navigating.",
"detail": "**What happens:** rc-dock keeps an inactive tab mounted but hidden with `display: none`. The effect at `use-bcv-sync-scroll.hook.ts:44` still fires on every `scrRef` change, but a display-none iframe has no layout, so the `scrollIntoView` at line 51 reads zero and silently no-ops. The request is lost rather than deferred.\n\n**Why it matters:** in Simple mode only one tab per stack is visible, so this is the common path, not an edge case.\n\n**Fix:** wrap the scroll in `useRunWhenVisible` (`lib/platform-bible-react/src/hooks/use-run-when-visible.hook.ts`) so repeats collapse into one catch-up that runs on activation."
```

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

**Cluster carefully - S5 dispatches one refuter per cluster.** A cluster is no longer only a way to
present findings: it asserts that its items rest on the same premise, and the whole cluster is
verified by one agent in one context. Two items that merely look alike, or land in the same file
for unrelated reasons, are NOT a cluster - grouping them makes one agent's judgement on the first
colour its answer on the rest. If you cannot write a `shared_fix` that is literally the same change
at each site, leave the items unclustered and let them get their own refuters.

## S5. Pressure-test

Dispatch **one refuter subagent per cluster, and one per unclustered item**, in parallel batches,
passing the Agent tool's `model: "sonnet"` override on each call (see Model below). Give each: every
item it owns (id, claim, `detail`, `evidence`, `file:line`), the cluster's `shared_fix` where there
is one, the worktree path `$WT`, `MERGE_BASE`, the relevant CLAUDE.md paths, and the obligation
table (below).

Grouping by cluster, not by item, is what keeps this stage affordable. A refuter pays the same fixed
cost - its own context, the repo's CLAUDE.md and rules, the files it reads - whether it answers one
claim or six, and on a real 68-item review that constant was being paid 68 times to examine 13
files. Cluster siblings share a premise and usually share the code that settles it, so verifying the
second site costs a fraction of the first.

**Group by cluster and by nothing else.** Do not batch unrelated items to make the agent count
smaller: that pays the full cost of a shared context - one verdict priming the next, attention
thinning down a long list - to save only the fixed overhead. Where the S4 clustering is honest, the
correlation a shared context introduces is correlation that *should* exist; where it is not, this
stage is where that shows up.

Refuters running in parallel share ONE worktree, so tell each: write probe files to your own temp
directory, never to `$WT`; revert any tracked file you mutate for a mutation test; and **leave
untracked files you did not create alone** - they belong to a sibling refuter still running.
Without this they spend tokens reporting each other's scratch files as unexplained repo state, and
one tidying up "stray" files will delete another's probe mid-run.

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

### The obligation table

Build this once, before dispatching, and paste it into every refuter prompt. It is what makes a fix
survive the *next* review rather than merely working.

Enumerate the rules this repo's reviewers actually read - `.claude/rules/**`, the standards table in
`CLAUDE.md`, and any `AGENTS.md` - and reduce them to a table of *triggers*: the shape of a change,
and what that shape obliges. Derive it per repo on every run rather than carrying one between runs:
these commands are used against more than one repository, the rule sets differ, and a stale table is
worse than none because it asserts obligations that no longer exist. A dozen rows is the right size;
it is a lookup, not a summary of the standards.

Shape it like this (the rows are illustrative - read the repo's own rules for the real ones):

| If the fix ... | it must also ... | rule |
| --- | --- | --- |
| edits a vendored component | carry the annotation that convention requires | `shadcn-discipline.md` |
| adds or changes a keyboard handler | update the shortcuts catalog | `keyboard-shortcuts-catalog.md` |
| adds user-facing strings | land in every locale file | `localization-locale-files.md` |
| changes behaviour | come with a test | `testing/tdd-discipline.md` |
| suppresses a tool warning | justify why fixing the code is worse | `eslint-disable-discipline.md` |
| adds a comment | be forward-facing, no PR or ticket narration | `forward-facing-comments.md` |
| chooses among viable approaches | be recorded in the decisions log | `CLAUDE.md` |

A fix that satisfies the claim but not the table is the cheapest kind of review churn there is: the
author implements it, the next reviewer flags the omission, and a second round buys nothing that
this one could not have said.

### The refuter's job

A refuter's task is to reach the truth, not to win. Instruct each one:

> Establish whether each of these findings is true, and how much it matters. You are the last check
> before they reach the PR author, so an unexamined "I could not tell" costs as much as a wrong
> answer.
>
> **Answer every finding separately, and expect them to differ.** The findings you were given share
> a fix, which means they share a premise - not a verdict. The single most common defect this review
> exists to catch is a fix that landed at one of the two sites that needed it, so a premise that
> holds at the first site tells you nothing about whether the second site was actually changed. Go
> to each site and check it. If you find yourself about to answer the fourth finding from what you
> learned on the first, you have stopped verifying.
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

### The refuter's second job: the fix

The block above settles whether the finding is **true**. It says nothing about whether its
`**Fix:**` slot **works**, and nothing upstream has checked that either: the generator wrote the fix
in S1 and it posts to the author verbatim. On paranext-core #2821, five of twenty fix lines were
unworkable while every claim under them was sound - `structuredClone` on a menu entry carrying
`click` closures (it throws), a symbol that exists but is not exported from the package index, a
memo whose dependency array does not include the state the fix depends on, a moved element that was
carrying the layout's flex sizing, and a hook named for a module-level call site. Four of those five
named a **real symbol that was real in the wrong place**. Reading that a symbol exists is not
checking that it is reachable from where the fix lands.

Instruct each refuter, for every item it owns:

> Answer separately whether this finding's `Fix:` line is something the author can actually carry
> out, and whether carrying it out would survive the next review. Three properties:
>
> - **Evidenced.** The fix climbs the same ladder the claim does, and the same rule applies: naming
>   a command you did not run is not evidence. Check that every symbol it names exists **and is
>   reachable from the site the fix lands on** - exported from the package index, not merely present
>   in the repo; in scope at that call site; importable under the repo's module restrictions. Check
>   that the API it invokes accepts what is being handed to it. Check that the mechanism actually
>   changes the behaviour in question - a memo that does not depend on the state, an effect that
>   does not re-run, a cache keyed on something that does not vary, all compile clean and fix
>   nothing. Check that the site is not carrying a responsibility the fix would drop. Where the
>   question is settled by a probe, **run the probe**: one `node -e` line established that
>   `structuredClone` refuses a closure.
> - **Complete.** Apply the obligation table above. If the change trips a row, the fix must say so.
> - **Honest.** If you cannot verify a concrete edit, do not rewrite it into a vaguer one. State the
>   **constraint** any fix must satisfy - "must preserve the `click` closures, and must land at both
>   `:207` and `:220`" - which is imperative, actionable, and cannot be wrong in the way a guessed
>   prescription can.
>
> **Answer this separately for every item, exactly as you do the verdict.** A cluster shares a fix
> *by definition*, so the pull to answer once and apply it to the rest is strongest here - and
> "the same change at the other site" is precisely the assertion that keeps turning out to be
> false. Go to each site.
>
> Where dependencies were linked, prefer a targeted check over a broad one: the single test file,
> a type-check of the one project, the linter on the one file, a `node -e` probe. Do **not** judge a
> fix by a repo-wide typecheck or lint in this worktree - `node_modules` is a symlink to another
> checkout sitting on a different commit, so those runs produce duplicate-identifier and
> unresolved-import errors that belong to the setup, not to the fix.
>
> Report one of three fix verdicts:
>
> - `SOUND` - the fix works as written, and is complete under the obligation table.
> - `REWRITTEN` - it does not, or it omits an obligation. **Supply the replacement text**; flagging
>   the problem without repairing it leaves the orchestrator to guess, which is how the bad fix got
>   there. A replacement may be a constraint rather than a prescription.
> - `UNVERIFIABLE` - you could not establish even the constraints. Say what you could not check.
>   Rare: if you can name one thing a fix must not break, you have a constraint, and that is a
>   `REWRITTEN`.

Reply format, exactly and with nothing before it - **one block per finding, in the order they were
given, with a blank line between blocks**. A block for every id you were given, even where several
reach the same verdict:

```
ITEM: <id>
VERDICT: CONFIRMED|DOWNGRADED|REFUTED|INCONCLUSIVE
SEVERITY: blocker|high|medium|low
ON_CHANGED_LINE: true|false
REASON: <3-6 sentences with file:line evidence>
FIX_VERDICT: SOUND|REWRITTEN|UNVERIFIABLE
FIX_REASON: <what you checked about the fix, with file:line evidence>
FIX_REWRITE: <the replacement Fix: text - required on REWRITTEN, omitted otherwise>
```

The three `FIX_` lines may be omitted only on a `REFUTED` item, whose fix text reaches nobody.
On every other verdict they are required, and `validate` rejects the ledger without them.

Write `REASON` for the PR author, not for the pipeline. On a `CONFIRMED` verdict it publishes
verbatim under **How this was checked**, and on `INCONCLUSIVE` under **What could not be checked**,
so say what you checked and what you found, keep the `file:line` evidence, and leave out the review
vocabulary. A `DOWNGRADED` reason does not publish at all - see "Recording the result" below for
where it goes instead.

**Do not refute on grounds that are about posting rather than truth.** Whether the anchor sits on a
changed line is recorded in `on_changed_line` and enforced by `payload` on its own; it is never a
reason for a verdict. Neither is "a linter or CI would catch it" - that speaks to who reports the
problem, not whether it exists.

### Model

Refuters run on **Sonnet by default**, regardless of what tier the generator ran on. A refuter
overturns a finding the generator raised, and a weaker model's characteristic failure is exactly
the one that matters here: unable to trace a path, it reads that as absence of a path - so do not
drop below Sonnet as the default.

An even cheaper model is allowed only for a **single unclustered item that is single-file,
single-site, and settled by reading that file** - a localization string, a TSDoc omission, a comment
that misdescribes adjacent code, a catalog entry. It is never allowed for a cluster: a cluster spans
sites by definition, so "settled by reading one file" cannot be true of it. Such a refuter may not
return `REFUTED` on a reachability argument either: any verdict resting on "nothing anywhere in the
repo produces this" requires a whole-repo search, so escalate it to Sonnet instead. Say in the
report which items ran on a cheaper model.

### Recording the result

Record `verdict`, `verdict_reason`, `on_changed_line`, `fix_verdict` and `fix_reason` per item.
Record the generator's severity as `severity_in` and the refuter's as `severity` - **never overwrite
`severity_in`**.

**Apply every `FIX_REWRITE` into the item's `detail`**, replacing the text after `**Fix:**` and
leaving the other two slots alone. Do this as you record, not later: `detail` is what posts, so a
rewrite left sitting in a refuter reply is a rewrite that never reached the author. Where a refuter
returned `REWRITTEN` with no replacement text, re-dispatch it once for the replacement rather than
writing one yourself - it read the site and you did not. If it still will not supply one, record
`UNVERIFIABLE` with that as the `fix_reason` and surface the item in S6. A refuter that
downgrades and refutes in one step otherwise leaves a triage table where every dropped finding reads
as low-severity noise, hiding that a `high` was dropped.

**Every item must end S5 with one of the four verdicts, and - unless REFUTED - a fix verdict too.**
A block that answers the claim and drops the `FIX_` lines has answered half the question; treat it
exactly as you treat a missing block. Check each reply against the ids you
dispatched: a cluster refuter that returns four blocks for a six-item cluster has silently dropped
two findings, and an item nobody answered is indistinguishable from one nobody raised. A refuter
that returns prose without a parseable `VERDICT:` line, or that omits ids, has not answered -
re-dispatch it once with the format and the missing ids restated, and if it still will not answer,
record `INCONCLUSIVE` for each unanswered item with that as the reason. Never leave a placeholder:
`payload`'s exclusion is `REFUTED`-only, so a finding left in any other state posts to the PR as
though it had been confirmed. `validate` rejects the ledger for this, but do not rely on it to
catch what this step should not produce.

Any item left `INCONCLUSIVE` after one escalation must be **named to the user in the S6 report**,
with what could not be checked. Do not close out the round silently holding an unanswered finding.

**Fold each downgrade's reasoning into the finding itself.** Neither `severity_in -> severity` nor
"why this was lowered" publishes - both describe a revision the author never saw, and `payload`
emits neither. So for every `DOWNGRADED` item, read its `verdict_reason` and ask whether it changes
what the author needs to know. When it does - the path is gated, unreachable, pre-existing, or
intentional - rewrite it into that item's `Why it matters:` slot in the author's terms, so the
severity it posts at reads as sensible rather than arbitrary. When it is only bookkeeping ("less
severe than first rated"), drop it. Either way leave `verdict_reason` itself untouched: it is the
refuter's record, and the S6 terminal report still prints it.

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
         "on_changed_line": true,              // VERIFY against the diff - see step 2a. Not the
                                               // refuter's word: false = body-only, and a wrong
                                               // true fails the WHOLE review, not just its comment.
                                               // Missing/null is treated as "not excluded", so
                                               // fill it in for every item.
         "claim": "one sentence in observable terms - see How every finding is written",
         "detail": "**What happens:** / **Why it matters:** / **Fix:** - same section",
         "evidence": ["src/bar.ts:88"],
         "verdict": "CONFIRMED|DOWNGRADED|REFUTED|INCONCLUSIVE",  // no placeholders; see S5
         "verdict_reason": "the S5 refuter's reasoning, whichever verdict it reached",
         "fix_verdict": "SOUND|REWRITTEN|UNVERIFIABLE",  // the refuter's SEPARATE answer on the
                                               // `**Fix:**` slot. Required unless `verdict` is
                                               // REFUTED; `validate` rejects a missing or unknown
                                               // value. A REWRITE is applied into `detail` before
                                               // the ledger is written, never stored beside it.
         "fix_reason": "what the refuter checked about the fix, with file:line evidence",
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

2a. **Derive `on_changed_line` from the diff, overwriting what S5 recorded.** Refuters get this
   wrong in both directions, and one contradicting its own stated evidence is not unusual - it is a
   mechanical property of the diff, not a judgement, so the diff wins. A wrong `false` silently
   demotes a good finding to the body; a wrong `true` makes GitHub reject the **entire** review.

   Judge against the **posting base** (GitHub's merge-base), not the review base - `gh pr diff <n>`
   is exactly that diff. Parse the `+` hunk ranges per file and set the field for every item:

   ```bash
   GH_REPO="$REPO" gh pr diff <n> > "$SCRATCH/pr-review/pr<n>/head.diff"
   ```

   Report any item whose recorded value you had to flip, and treat it as a signal about that
   refuter's other answers. (`/pr-review-post`'s `reanchor` does this too, but the ledger should
   not be wrong in the meantime.)

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
     This arrow is **terminal-only**: it is how you judge whether the pressure-test is calibrated.
     `payload` publishes the final severity alone, so never carry the arrow into `claim`, `detail`,
     or the review body.
   - a `Verdict summary` line: counts of CONFIRMED / DOWNGRADED / REFUTED / INCONCLUSIVE, and the
     refutation rate as `REFUTED / total`. State it plainly; it is the number that tells you
     whether the pressure-test is calibrated.
   - a `Fix audit` line and section: counts of SOUND / REWRITTEN / UNVERIFIABLE, then every
     REWRITTEN item - id, what was wrong with the generator's fix, and the replacement in one line -
     and every UNVERIFIABLE item with what could not be established. The rate is the calibration
     number for this stage, exactly as the refutation rate is for the verdicts: it was 5 in 20 on
     the review that prompted the stage, and a run reporting 0 in 30 is a claim about the
     generator that deserves the same suspicion as a 0% refutation rate. Say plainly that an
     UNVERIFIABLE fix needs a human before it posts.
   - a `Downgraded` section: every DOWNGRADED item - id, `<severity_in> -> <severity>`, claim, and
     the refuter's reason. These post inline with `all` like any other anchorable finding; they are
     not dropped and need no `--include-refuted`. Say for each whether you folded its reason into
     the item's `Why it matters:` text or judged it bookkeeping and dropped it, since neither the
     arrow nor the reason reaches the author.
   - an `Unresolved` section: every INCONCLUSIVE item, with what the refuter could not check and
     what would settle it. **Never omit this section when it is non-empty**, and say plainly that
     these need a human judgement.
   - a `Refuted (not posted unless you name them)` section: each id, its claim, and the refuter's
     reason, so they can be overruled
   - a `Previously reported` section: each id with a link to the existing comment
   - the count of items not on a changed line (body-only, cannot be posted inline)
   - which items, if any, ran their refuter on a cheaper model (S5)
   - a `Pressure-test dispatch` line: how many refuter agents ran for how many items, and the
     largest cluster any one of them owned. This is the stage's cost driver, and the cluster size
     is where a shared context starts thinning - record it so the grouping stays checkable against
     what it is buying.
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
