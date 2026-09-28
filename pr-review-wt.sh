#!/usr/bin/env bash
# Worktree lifecycle for the PR review pipeline: reap | create | remove
set -uo pipefail

MAX_AGE_HOURS="${REVIEW_WT_MAX_AGE_HOURS:-24}"
# `kept <path> not-a-review-worktree` is emitted once per worktree the reaper is
# not responsible for, which in a real repo buries the lines that need action.
# It is suppressed unless asked for; every other kept/reaped/pruned/orphaned/
# error/stranded-branch line always prints.
VERBOSE="${REVIEW_WT_VERBOSE:-0}"

# cygpath ships only with MSYS/Cygwin, so its presence is what marks this
# script as running on Windows. Everything gated on it is a no-op on Linux
# and macOS.
on_windows() { command -v cygpath >/dev/null 2>&1; }

# Paths reach this script in two spellings. bash computes POSIX form
# (/tmp/..., /c/Users/...); git always reports its own native form, which on
# Windows is C:/Users/.... Any comparison between the two must pass through
# here first, or it silently never matches. Off Windows this returns its
# argument unchanged.
native_path() {
  if on_windows; then
    cygpath -m "$1" 2>/dev/null || printf '%s' "$1"
  else
    printf '%s' "$1"
  fi
}

# This session's scratchpad directory, or nothing.
#
# $CLAUDE_SCRATCHPAD wins if the harness ever sets it, but today it does not
# exist; what exists is $CLAUDE_CODE_SESSION_ID, from which Claude Code's
# scratchpad path is derivable:
#   /tmp/claude-<uid>/<cwd with every / replaced by ->/<session id>/scratchpad
# Returns 1 and prints nothing when neither is available, or when the value
# resolves to anything other than an absolute path with at least one component.
# Callers must treat a failure as "unknown", never as a prefix: an empty string
# would prefix-match every path (reap keeps everything) and would expand a
# caller's "$SCRATCH/wt-prN" to "/wt-prN" (git worktree add at the filesystem
# root). A bare "/" strips to empty and is rejected for that reason.
#
# pwd -P, not pwd: the derived path must key on the real directory, since a
# symlinked cwd would derive a prefix that never matches the worktree's own
# registered path. Same canonicalization cmd_remove does.
scratchpad_path() {
  local sp
  if [ -n "${CLAUDE_SCRATCHPAD:-}" ]; then
    sp="$CLAUDE_SCRATCHPAD"
    # A drive-letter value has to become POSIX before the absolute-path check
    # below, which only recognizes a leading slash and would reject it.
    case "$sp" in
      [A-Za-z]:[\\/]*) on_windows && sp=$(cygpath -u "$sp") ;;
    esac
  elif [ -n "${CLAUDE_CODE_SESSION_ID:-}" ]; then
    sp="/tmp/claude-$(id -u)/$(pwd -P | tr / -)/$CLAUDE_CODE_SESSION_ID/scratchpad"
  else
    return 1
  fi
  while [ "${sp%/}" != "$sp" ]; do sp="${sp%/}"; done
  case "$sp" in /?*) ;; *) return 1 ;; esac
  # Emit the form git reports, since consider() prefix-matches this against
  # paths taken straight from `git worktree list`, and callers grep it against
  # that same output.
  sp=$(native_path "$sp")
  printf '%s\n' "$sp"
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

# Unlink every symlink and junction inside worktree <wt>, following none of
# them. Must run before any `git worktree remove --force`: on Windows that
# recurses through a directory junction and deletes the contents of whatever
# it points to, outside the worktree included, and package managers there
# (pnpm, npm workspaces) install with junctions. MSYS reports a junction as a
# symlink, so `find -type l` lists it without descending into it, and `rm`
# unlinks it without following it. git follows no symlink off Windows, and no
# native one on Windows, so a junction is the only hazard and this is
# Windows-only.
#
# A no-op for anything but the root of a linked worktree, whose .git is a
# file: this sweeps a whole tree, so an empty or mistaken argument must
# never reach `find` as `/` or as the primary checkout.
unlink_links_in_worktree() {
  local wt="${1:-}"
  on_windows || return 0
  [ -n "$wt" ] && [ -f "$wt/.git" ] || return 0
  find "${wt:?}/" -type l -exec rm -f -- {} + 2>/dev/null
  return 0
}

# Resolve the primary checkout backing worktree <wt> - the directory holding
# the real .git this worktree shares - and echo its path on stdout. The path
# always ends in a literal "/..", since it is built directly from
# `--git-common-dir` (which names the .git dir itself, not its parent) and is
# not otherwise canonicalized; a caller that needs an absolute, existing path
# resolves it itself (`cd "$path" && pwd`, as cmd_link_deps does), since only
# that caller needs the extra failure mode of "the path doesn't resolve".
# Fails with "error: not a git worktree: <wt>" on stderr, exit 1, when <wt>
# is not a git worktree at all.
primary_repo_of() {
  local wt="$1" common
  common=$(git -C "$wt" rev-parse --git-common-dir 2>/dev/null) || {
    echo "error: not a git worktree: $wt" >&2; return 1; }
  # A drive letter is absolute too; read as relative it would be joined onto
  # $wt, yielding a path that cannot exist.
  case "$common" in /*|[A-Za-z]:[\\/]*) ;; *) common="$wt/$common" ;; esac
  printf '%s\n' "$common/.."
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
  #
  # A refuter's private worktree exists only to hold throwaway probes, so its
  # dirt is disposable by construction. Every other review path keeps the guard.
  case "$wt" in
    */scratchpad/wt-pr*-r[ci][0-9]*) ;;
    *)
      local d; d=$(dirty_count "$wt")
      if [ "$d" -gt 0 ]; then echo "kept $wt dirty-${d}-changes"; return 0; fi
      ;;
  esac

  local a; a=$(age_hours "$wt")
  if [ "$a" -lt "$MAX_AGE_HOURS" ]; then echo "kept $wt age-${a}h"; return 0; fi

  [ -n "$locked" ] && git worktree unlock "$wt" >/dev/null 2>&1
  unlink_links_in_worktree "$wt"
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
  # Reject anything that isn't the verbose flag rather than ignoring it: a
  # mistyped `--verbos` must not look like it was honoured.
  if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != "-v" ] && [ "$1" != "--verbose" ]; }; then
    echo "usage: pr-review-wt.sh reap [-v|--verbose]" >&2
    return 2
  fi
  [ "$#" -eq 0 ] || VERBOSE=1
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
  # worktree's real path unless symlink components are resolved. Both
  # candidates are converted to git's own path form, which is what the
  # registration they are matched against is spelled in.
  local registered real
  registered=$(native_path "$path")
  real=$(cd "$path" 2>/dev/null && pwd -P) && real=$(native_path "$real")
  if git worktree list --porcelain | grep -qxF "worktree $registered" \
  || { [ -n "$real" ] && git worktree list --porcelain | grep -qxF "worktree $real"; }; then
    git worktree unlock "$path" >/dev/null 2>&1
    unlink_links_in_worktree "$path"
    git worktree remove --force "$path" >/dev/null 2>&1
  fi
  rm -rf "$path"
  git worktree prune >/dev/null 2>&1
  return 0
}

cmd_remove_refuters() {
  local scratch="${1:-}" n="${2:-}"
  if [ -z "$scratch" ] || [ -z "$n" ]; then
    echo "usage: pr-review-wt.sh remove-refuters <scratchpad> <pr-number>" >&2; return 2
  fi
  local d
  for d in "$scratch"/wt-pr"$n"-r[ci][0-9]*; do
    [ -e "$d" ] || continue
    cmd_remove "$d"; echo "removed $d"
  done
  git worktree prune >/dev/null 2>&1
  return 0
}

cmd_primary_status() {
  local wt="${1:-}"
  [ -n "$wt" ] || { echo "usage: pr-review-wt.sh primary-status <worktree>" >&2; return 2; }
  local primary
  primary=$(primary_repo_of "$wt") || return 1
  git -C "$primary" status --porcelain
}

# Make the primary checkout's installed dependencies visible inside a review
# worktree, so a refuter can actually run typecheck/lint/tests instead of
# reasoning from source and calling the result unverifiable.
#
# Only safe when the PR does not itself change what is installed, so this
# refuses whenever any tracked lockfile differs between the merge base and the
# worktree's head - a stale node_modules would make a refuter's "I ran it and
# it passed" worse than no answer at all. Prints exactly one machine-readable
# line: `linked <path>` or `declined <reason>`.
cmd_link_deps() {
  local wt="${1:-}" base="${2:-}"
  if [ -z "$wt" ] || [ -z "$base" ]; then
    echo "usage: pr-review-wt.sh link-deps <worktree-path> <merge-base>" >&2; return 2
  fi
  if [ ! -d "$wt" ]; then echo "error: no such worktree: $wt" >&2; return 1; fi

  local primary
  primary=$(primary_repo_of "$wt") || return 1
  local main_repo
  main_repo=$(cd "$primary" 2>/dev/null && pwd) || {
    echo "error: cannot locate the primary checkout for $wt" >&2; return 1; }

  if [ -e "$wt/node_modules" ]; then echo "declined already-present"; return 0; fi
  if [ ! -d "$main_repo/node_modules" ]; then
    echo "declined no-node_modules-in-$main_repo"; return 0
  fi

  # Every tracked lockfile, not just the root one: a monorepo can carry more
  # than one (this repo has release/app's alongside the root's), and a change
  # to any of them makes the primary checkout's tree the wrong tree.
  local locks
  locks=$(git -C "$wt" ls-files -- '*package-lock.json' '*pnpm-lock.yaml' '*yarn.lock' '*bun.lockb' 2>/dev/null | sort -u)
  if [ -n "$locks" ]; then
    local changed
    changed=$(git -C "$wt" diff --name-only "$base" HEAD -- $locks 2>/dev/null)
    if [ -n "$changed" ]; then
      echo "declined lockfile-changed:$(echo "$changed" | tr '\n' ',' | sed 's/,$//')"; return 0
    fi
  fi

  # A Windows account without the symlink privilege (Developer Mode off, not
  # elevated - the default) fails every link below. That is a property of the
  # account rather than a fault, so it is a decline, not an error.
  if on_windows && ! symlinks_available; then
    echo "declined symlinks-unavailable"; return 0
  fi

  # The physical path, since mirror_modules_entry compares it against `readlink -f` output.
  local main_real wt_real
  main_real=$(cd "$main_repo" && pwd -P) && wt_real=$(cd "$wt" && pwd -P) || {
    echo "error: cannot resolve the physical paths of $main_repo and $wt" >&2; return 1; }

  local relinked=0
  mirror_modules_dir "$main_repo/node_modules" "$wt/node_modules" || {
    echo "error: could not link node_modules into $wt" >&2; return 1; }

  local extra=0 pkg
  while IFS= read -r pkg; do
    pkg=$(dirname "$pkg"); [ "$pkg" = "." ] && continue
    if [ -d "$main_repo/$pkg/node_modules" ] && [ ! -e "$wt/$pkg/node_modules" ]; then
      mirror_modules_dir "$main_repo/$pkg/node_modules" "$wt/$pkg/node_modules" && extra=$((extra+1))
    fi
  done < <(git -C "$wt" ls-files -- '*package.json')
  echo "linked $wt/node_modules (+$extra package node_modules, $relinked workspace links into the worktree)"
}

# Rebuild one node_modules directory in the worktree as a real directory whose
# entries link back to the primary checkout's, one by one. Linking the whole
# directory instead would resolve every workspace-package link inside it
# (pnpm's `node_modules/shared -> ../libs/shared`) against the PRIMARY
# checkout, so a test run in the worktree would import the primary's source
# for every workspace package rather than the head under review - and caches a
# tool writes into node_modules (vite's `.vite`) would land in the primary.
# A scope directory (`@scope/`) is mirrored the same way, one level down.
# Reads `main_real`, `wt_real` and increments `relinked` from cmd_link_deps.
#
# On a failed link the directory this created is removed again: left
# half-built, it reads to every later run as `declined already-present`.
# `rm -rf` unlinks the links inside it without following them into the
# primary checkout, and <dst> is known to be this call's own, since the
# `mkdir` that made it would have failed on anything already there.
mirror_modules_dir() {
  local src="$1" dst="$2" entry name
  mkdir "$dst" || return 1
  for entry in "$src"/* "$src"/.[!.]* "$src"/..?*; do
    [ -e "$entry" ] || [ -L "$entry" ] || continue
    name=${entry##*/}
    if [[ "$name" == @* ]] && [ -d "$entry" ] && [ ! -L "$entry" ]; then
      mirror_modules_dir "$entry" "$dst/$name"
    else
      mirror_modules_entry "$entry" "$dst/$name"
    fi || { rm -rf "${dst:?}"; return 1; }
  done
}

# Whether this account can create a native symlink. It opts in exactly as the
# real links do, since default MSYS copies instead of failing and would always
# pass. A probe that cannot even run answers yes, and leaves the real links
# to report whatever is wrong.
symlinks_available() {
  local probe ok=0
  probe=$(mktemp -d 2>/dev/null) && [ -n "$probe" ] && [ -d "$probe" ] || return 0
  if mkdir "$probe/t" 2>/dev/null; then
    MSYS=winsymlinks:nativestrict ln -s "$probe/t" "$probe/l" 2>/dev/null || ok=1
  fi
  rm -rf "${probe:?}"
  return "$ok"
}

# Link one node_modules entry. An entry that resolves to a tracked path of the
# primary checkout outside any node_modules is a workspace package (or a
# `file:` dependency): point it at the same path in the worktree, when the
# head still has it. Everything else - installed third-party packages, `.bin`,
# the `.pnpm` store - is shared with the primary checkout as before.
#
# Every link here must be a real symlink. Git Bash's `ln -s` copies the target
# instead unless MSYS opts in, which would duplicate the whole of the primary
# checkout's node_modules into the worktree and sever the sharing this exists
# for; nativestrict also fails loudly rather than falling back to that copy.
# The variable is meaningless off MSYS.
mirror_modules_entry() {
  local src="$1" dst="$2" target rel
  if [ -L "$src" ]; then
    target=$(readlink -f "$src")
    case "$target" in
      "$main_real"/*)
        rel=${target#"$main_real"/}
        case "/$rel/" in
          */node_modules/*) ;;
          *)
            if [ -e "$wt_real/$rel" ]; then
              MSYS=winsymlinks:nativestrict ln -s "$wt_real/$rel" "$dst" || return 1
              relinked=$((relinked+1)); return 0
            fi ;;
        esac ;;
    esac
  fi
  # On Windows the link names the directory <src> resolves to, not <src>
  # itself: linking a junction - which is how pnpm installs every package
  # there - yields a file-type symlink, which MSYS tools read through but
  # node.exe cannot (stat fails with EPERM, so `require` does too). A junction
  # is never used in its place, because `git worktree remove --force`
  # deletes a junction's contents.
  on_windows && [ -n "${target:-}" ] && src=$target
  MSYS=winsymlinks:nativestrict ln -s "$src" "$dst"
}

cmd_scratchpad() {
  local sp
  if sp=$(scratchpad_path); then printf '%s\n' "$sp"; return 0; fi
  echo "error: cannot resolve the scratchpad directory - \$CLAUDE_SCRATCHPAD is unset or not an absolute path, and \$CLAUDE_CODE_SESSION_ID is unset" >&2
  return 1
}

case "${1:-}" in
  reap)             shift; cmd_reap "$@" ;;
  create)           shift; cmd_create "$@" ;;
  remove)           shift; cmd_remove "$@" ;;
  remove-refuters)  shift; cmd_remove_refuters "$@" ;;
  primary-status)   shift; cmd_primary_status "$@" ;;
  scratchpad)       shift; cmd_scratchpad "$@" ;;
  link-deps)        shift; cmd_link_deps "$@" ;;
  *) echo "usage: pr-review-wt.sh {reap [-v]|create <path> <committish>|remove <path>|remove-refuters <scratchpad> <pr-number>|primary-status <worktree>|scratchpad|link-deps <path> <merge-base>}" >&2; exit 2 ;;
esac
