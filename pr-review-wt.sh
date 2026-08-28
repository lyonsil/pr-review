#!/usr/bin/env bash
# Worktree lifecycle for the PR review pipeline: reap | create | remove
set -uo pipefail

MAX_AGE_HOURS="${REVIEW_WT_MAX_AGE_HOURS:-24}"

is_review_path() {
  case "$1" in
    */.claude/worktrees/review-*|*/.claude/worktrees/verify-*|*/scratchpad/wt-*) return 0 ;;
    *) return 1 ;;
  esac
}

# Extract "(pid N)" from a lock reason; empty if absent.
lock_pid() { printf '%s' "$1" | sed -n 's/.*(pid \([0-9]\+\).*/\1/p'; }

pid_alive() { [ -n "$1" ] && kill -0 "$1" 2>/dev/null; }

age_hours() {
  [ -d "$1" ] || { echo 999999; return; }
  echo $(( ( $(date +%s) - $(stat -c %Y "$1") ) / 3600 ))
}

consider() {
  local wt="$1" locked="$2" branch="$3"
  [ -n "$wt" ] || return 0
  is_review_path "$wt" || { echo "kept $wt not-a-review-worktree"; return 0; }

  if [ -n "${CLAUDE_SCRATCHPAD:-}" ]; then
    case "$wt" in "$CLAUDE_SCRATCHPAD"/*|"$CLAUDE_SCRATCHPAD") echo "kept $wt current-session"; return 0 ;; esac
  fi

  if [ -n "$locked" ]; then
    local p; p=$(lock_pid "$locked")
    if pid_alive "$p"; then echo "kept $wt locked-by-live-pid-$p"; return 0; fi
  fi

  local a; a=$(age_hours "$wt")
  if [ "$a" -lt "$MAX_AGE_HOURS" ]; then echo "kept $wt age-${a}h"; return 0; fi

  [ -n "$locked" ] && git worktree unlock "$wt" >/dev/null 2>&1
  if git worktree remove --force "$wt" >/dev/null 2>&1; then
    echo "reaped $wt"
  else
    echo "reaped $wt (registration only)"
  fi
  if [ -n "$branch" ]; then
    echo "stranded-branch ${branch#refs/heads/} $wt"
  fi
}

cmd_reap() {
  local wt="" locked="" branch="" line
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      worktree\ *) consider "$wt" "$locked" "$branch"; wt="${line#worktree }"; locked=""; branch="" ;;
      branch\ *)   branch="${line#branch }" ;;
      locked*)     locked="${line#locked}"; locked="${locked# }"; [ -n "$locked" ] || locked="(no reason)" ;;
    esac
  done < <(git worktree list --porcelain)
  consider "$wt" "$locked" "$branch"
  git worktree prune >/dev/null 2>&1
  return 0
}

case "${1:-}" in
  reap) shift; cmd_reap "$@" ;;
  *) echo "usage: pr-review-wt.sh reap" >&2; exit 2 ;;
esac
