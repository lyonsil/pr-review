# Decisions made while building this

Rulings taken during implementation, with what each costs if wrong. Recorded because this tooling
is still being iterated on and the reasoning is not recoverable from the diffs.

## Structural

- **Built as a git repo at `~/.claude/pr-review`, commands symlinked into `~/.claude/commands/`.**
  The plan put everything in untracked `~/.claude/`, leaving no commits or diffs to review.
  *If wrong:* a directory move and a sed.
- **Session-scoped ledger, in the scratchpad.** Chosen for zero cleanup burden. The triage gate
  must therefore close same-day; a `/clear` between the two commands loses the review.
  *If wrong:* re-run `/pr-review`; moving the ledger to `~/.claude/` touches two paths.
- **v1 covers fresh reviews only.** The "author responded, check their updates against my prior
  feedback" round stays manual.

## Safety of the destructive path

- **The reaper never removes a worktree holding uncommitted or untracked changes** (`kept ...
  dirty-N-changes`). A single `git worktree remove --force` destroys dirty and untracked content
  outright on git 2.52.0 - verified, not assumed. *If wrong:* a dirty abandoned worktree needs one
  manual `-f -f` and is named every run until then.
- **`cmd_remove` deliberately does NOT inherit that guard.** It is the explicit teardown path;
  refusing there would strand exactly the worktrees this tool exists to collect.
- **Dropped the `*/scratch/wt-*` glob** that existed only to suit a test fixture. Reaper breadth is
  the one place this tool destroys data, so it matches the spec exactly.
- **The live-pid lock guard is provenance only, not protection.** Each Bash call is a fresh shell,
  so the recorded pid is dead by the next call whatever the invocation shape. Real protection is
  the scratchpad path exclusion plus the 24h age floor. An earlier ruling claimed otherwise and was
  wrong; the spec and prompts were corrected.
- **Scratchpad path is derived inside the script, not passed in by the prompts.** Pinning it from
  the prompt would create split resolution - `create` pinned, a later `reap` derived, two different
  prefixes, exclusion silently misses the live worktree. *If wrong:* a harness path change degrades
  protection to the 24h age floor, which is the documented fallback, not unprotected deletion.
- **Reaping is name-based.** A native `EnterWorktree` tree named `review-*` is in scope; while in
  use it is protected by a live Claude pid, and once abandoned collecting it is desirable.

## Safety of the outward-facing path

- **Anchor per distinct code site**, never one-per-cluster. A cluster shares a *fix*, but the fix
  must land at every site; on PR #2565 "fixed one of the two sites" was the dominant failure, seven
  times over.
- **`--include-refuted` is per-id and requires the id to also be selected.** A blanket flag would
  defeat the point of refuting anything. Overruling by hand-editing `verdict` is forbidden - it
  destroys the refuter's recorded reasoning.
- **A dead anchor is excluded unconditionally** and cannot be lifted by `--include-refuted`.
  GitHub rejects the whole review if one anchor is off-diff.
- **An empty payload is an error, not a silent success.** A review carrying only framing and the
  attribution footer is never intended.
- **A pure re-indent classifies as `dead` rather than relocating.** The failure modes are
  asymmetric: a false `ok` puts a comment on the wrong line in front of the PR author; a false
  `dead` only demotes the finding to the body.
- **Re-anchoring runs unconditionally**, not only when the head moved - otherwise `anchor_status`
  is never set on the unchanged-head path and the dead-anchor filter cannot fire.

## Process

- Every guard is mutation-proven: a test that still passes with its guard removed is treated as a
  defect. This caught three guards that had shipped under fully green runs.
- Task 5's review and the whole-branch review ran on the most capable model; the rest on a mid
  tier. Task 5 is the only one whose output publishes under a real account.
- Tasks 6 and 7 were implemented as one batch - two prompt files sharing one ledger contract, where
  consistency matters more than separate review surfaces.
- One hardening round was run past the single-fix-wave limit, because the wave's own new subcommand
  re-opened the plan's worst defect (`CLAUDE_SCRATCHPAD=/` yielding an empty path).
