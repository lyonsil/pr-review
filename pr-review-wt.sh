#!/usr/bin/env bash
# Worktree lifecycle for the PR review pipeline: reap | create | remove
set -uo pipefail

MAX_AGE_HOURS="${REVIEW_WT_MAX_AGE_HOURS:-24}"
# `kept <path> not-a-review-worktree` is emitted once per worktree the reaper is
# not responsible for, which in a real repo buries the lines that need action.
# It is suppressed unless asked for; every other kept/reaped/pruned/orphaned/
# error/stranded-branch line always prints.
VERBOSE="${REVIEW_WT_VERBOSE:-0}"

# This session's scratchpad directory, or nothing.
#
# $CLAUDE_SCRATCHPAD wins if the harness ever sets it, but today it does not
# exist; what exists is $CLAUDE_CODE_SESSION_ID, from which Claude Code's
# scratchpad path is derivable:
#   /tmp/claude-<uid>/<cwd with every / replaced by ->/<session id>/scratchpad
# Returns 1 and prints nothing when neither is available. Callers must treat
# that as "unknown", never as a prefix - an empty string would prefix-match
# every path and make the reaper keep everything.
scratchpad_path() {
  if [ -n "${CLAUDE_SCRATCHPAD:-}" ]; then printf '%s\n' "${CLAUDE_SCRATCHPAD%/}"; return 0; fi
  [ -n "${CLAUDE_CODE_SESSION_ID:-}" ] || return 1
  printf '/tmp/claude-%s/%s/%s/scratchpad\n' "$(id -u)" "$(pwd | tr / -)" "$CLAUDE_CODE_SESSION_ID"
}

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

# Count of dirty lines (modified tracked + untracked) in a worktree. 0 when
# the directory is missing (git -C on a missing dir fails silently), which is
# intentional: a registration with no directory behind it has nothing to lose
# and must still fall through to the registration-only cleanup path below.
dirty_count() {
  git -C "$1" status --porcelain 2>/dev/null | wc -l | tr -d ' '
}

consider() {
  local wt="$1" locked="$2" branch="$3"
  [ -n "$wt" ] || return 0
  if ! is_review_path "$wt"; then
    [ "$VERBOSE" = 0 ] || echo "kept $wt not-a-review-worktree"
    return 0
  fi

  local sp; sp=$(scratchpad_path) || sp=""
  if [ -n "$sp" ]; then
    case "$wt" in "$sp"/*|"$sp") echo "kept $wt current-session"; return 0 ;; esac
  fi

  if [ -n "$locked" ]; then
    local p; p=$(lock_pid "$locked")
    if pid_alive "$p"; then echo "kept $wt locked-by-live-pid-$p"; return 0; fi
  fi

  # A single `git worktree remove --force` deletes a dirty worktree outright
  # (confirmed on git 2.52.0) - it does not refuse the way an unclean removal
  # without --force does. This guard must run before the age check, and
  # before any removal call, or a dirty-but-aged worktree is destroyed.
  local d; d=$(dirty_count "$wt")
  if [ "$d" -gt 0 ]; then echo "kept $wt dirty-${d}-changes"; return 0; fi

  local a; a=$(age_hours "$wt")
  if [ "$a" -lt "$MAX_AGE_HOURS" ]; then echo "kept $wt age-${a}h"; return 0; fi

  [ -n "$locked" ] && git worktree unlock "$wt" >/dev/null 2>&1
  if [ ! -d "$wt" ]; then
    # Directory already gone; this is registration cleanup only, not a
    # removal decision, so its outcome doesn't gate the message below.
    git worktree remove --force "$wt" >/dev/null 2>&1
    echo "pruned $wt registration-only"
  elif git worktree remove --force "$wt" >/dev/null 2>&1; then
    echo "reaped $wt"
  else
    # A failed `git worktree remove --force` can still have torn down the
    # admin registration and deleted file contents before failing only on
    # the final rmdir (observed on git 2.52.0 under a permission-denied
    # parent dir) - leaving an orphaned, deregistered, empty directory that
    # no future `git worktree list` will surface. Distinguish that from a
    # genuine no-op failure so the message never implies "nothing happened"
    # when something did.
    if [ -d "$wt" ] && ! git worktree list --porcelain | grep -qxF "worktree $wt"; then
      echo "orphaned $wt manual-cleanup-needed"
    else
      echo "error $wt remove-failed"
    fi
    return 0
  fi
  if [ -n "$branch" ]; then
    echo "stranded-branch ${branch#refs/heads/} $wt"
  fi
}

cmd_reap() {
  case "${1:-}" in -v|--verbose) VERBOSE=1 ;; esac
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

cmd_create() {
  local path="$1" committish="$2"
  mkdir -p "$(dirname "$path")"
  git worktree add -q --detach "$path" "$committish"
  # $PPID, not $$: this subcommand's own process exits the instant create()
  # returns, so a pid recorded from $$ is already dead by the time any later
  # reap checks it. $PPID is the invoking shell's pid - still not a live
  # guard in practice (that shell is itself short-lived across separate tool
  # invocations), so treat this lock as provenance only. What actually
  # protects a live worktree is reap's scratchpad path exclusion (see
  # scratchpad_path/consider() above), plus the 24h age floor as a fallback.
  git worktree lock --reason "pr-review session ${CLAUDE_CODE_SESSION_ID:-local} (pid $PPID)" "$path"
  (cd "$path" && pwd)
}

cmd_remove() {
  local path="$1"
  # pwd -P (not pwd): a symlinked path won't string-match the registered
  # worktree's real path unless symlink components are resolved.
  if git worktree list --porcelain | grep -qxF "worktree $path" \
  || git worktree list --porcelain | grep -qxF "worktree $(cd "$path" 2>/dev/null && pwd -P)"; then
    git worktree unlock "$path" >/dev/null 2>&1
    git worktree remove --force "$path" >/dev/null 2>&1
  fi
  rm -rf "$path"
  git worktree prune >/dev/null 2>&1
  return 0
}

cmd_scratchpad() {
  local sp
  if sp=$(scratchpad_path); then printf '%s\n' "$sp"; return 0; fi
  echo "error: cannot resolve the scratchpad directory - neither \$CLAUDE_SCRATCHPAD nor \$CLAUDE_CODE_SESSION_ID is set" >&2
  return 1
}

case "${1:-}" in
  reap)       shift; cmd_reap "$@" ;;
  create)     shift; cmd_create "$@" ;;
  remove)     shift; cmd_remove "$@" ;;
  scratchpad) shift; cmd_scratchpad "$@" ;;
  *) echo "usage: pr-review-wt.sh {reap [-v]|create <path> <committish>|remove <path>|scratchpad}" >&2; exit 2 ;;
esac
