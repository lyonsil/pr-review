# pr-review

Personal tooling for the multi-stage PR review pattern. Two slash commands plus the
deterministic scripts they call.

- `pr-review-wt.sh`      worktree lifecycle: reap | create | remove | scratchpad | link-deps
- `pr-review-anchor.py`  ledger validate | reanchor | payload
- `commands/pr-review.md`, `commands/pr-review-post.md`  symlinked into ~/.claude/commands/

Spec and plan: paranext-core `docs/superpowers/specs|plans/2026-08-28-pr-review-pipeline*.md`

## Install

The commands hardcode `~/.claude/pr-review/...` paths to the scripts, so this directory must
live at exactly `~/.claude/pr-review/` - not a repo-relative path.

1. Extract/clone this directory to `~/.claude/pr-review/`.
2. `chmod +x ~/.claude/pr-review/pr-review-wt.sh ~/.claude/pr-review/pr-review-anchor.py`
3. Symlink the two commands into your commands directory:
   ```bash
   mkdir -p ~/.claude/commands
   ln -s ~/.claude/pr-review/commands/pr-review.md ~/.claude/commands/pr-review.md
   ln -s ~/.claude/pr-review/commands/pr-review-post.md ~/.claude/commands/pr-review-post.md
   ```
4. Requires: `bash`, `python3` (standard library only, no pip installs), `git`, and an
   authenticated `gh` CLI (`gh auth status`).
5. Sanity check: `~/.claude/pr-review/pr-review-wt.sh reap` should print reaper output with no
   errors, and `python3 ~/.claude/pr-review/pr-review-anchor.py --help` should print usage.
