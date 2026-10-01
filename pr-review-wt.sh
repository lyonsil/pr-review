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
# resolves to anything other than an absolute path with at least one component
# (on Windows, one below a drive root).
# Callers must treat a failure as "unknown", never as a prefix: an empty string
# would prefix-match every path (reap keeps everything) and would expand a
# caller's "$SCRATCH/wt-prN" to "/wt-prN" (git worktree add at the filesystem
# root). A bare "/" strips to empty and is rejected for that reason.
#
# pwd -P, not pwd: the derived path must key on the real directory, since a
# symlinked cwd would derive a prefix that never matches the worktree's own
# registered path. Same canonicalization registered_worktree does.
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
  # On Windows /c is the root of drive C:, rejected for the same reason as
  # "/". Elsewhere it is an ordinary one-letter directory.
  if on_windows; then
    case "$sp" in /[A-Za-z]) return 1 ;; esac
  fi
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

# Whether <path> is registered as a linked worktree of the current repo, under
# either its given or its physical spelling: pwd -P, since a symlinked path
# names the worktree but never string-matches its registration. Both are
# compared in git's own spelling. The main worktree, always listed first, is
# never a match.
registered_worktree() {
  local path="$1" given real
  given=$(native_path "$path")
  real=$(cd "$path" 2>/dev/null && pwd -P) && real=$(native_path "$real") || real=$given
  git worktree list --porcelain | sed -n 's/^worktree //p' | sed 1d \
    | grep -qxF -e "$given" -e "$real"
}

# Whether directory <path> is the root of a linked worktree: its .git is a
# file naming an admin directory under some repo's .git/worktrees/ (a
# submodule's names .git/modules/ instead), or the current repo has it
# registered as one. Nothing else may reach remove_worktree's deletes, so a
# mistaken argument can never be `/`, the primary checkout or a subdirectory.
is_linked_worktree() {
  local path="$1" gitdir
  if [ -f "$path/.git" ]; then
    gitdir=$(sed -n 's/^gitdir: //p' "$path/.git" 2>/dev/null)
    case "$gitdir" in */worktrees/?*) return 0 ;; esac
  fi
  registered_worktree "$path"
}

# Unlink every symlink and junction inside worktree <wt>, following none of
# them, and fail if any is still there afterwards. On Windows
# `git worktree remove --force` recurses through a directory junction and
# deletes the contents of whatever it points to, outside the worktree
# included, and package managers there (pnpm, npm workspaces) install with
# junctions. MSYS reports a junction as a symlink, so `find -type l` lists it
# without descending into it, and `rm` unlinks it without following it. git
# follows no symlink off Windows, and no native one on Windows, so a junction
# is the only hazard and this is Windows-only.
#
# Sweeps a whole tree, so only remove_worktree calls it, on a path
# is_linked_worktree has accepted.
unlink_links_in_worktree() {
  local wt="${1:?}"
  on_windows || return 0
  find "${wt:?}/" -type l -exec rm -f -- {} + 2>/dev/null
  [ -z "$(find "${wt:?}/" -type l -print -quit 2>/dev/null)" ]
}

# Put back the tracked files missing from worktree <wt>, which after a failed
# removal are the tracked symlinks the sweep unlinked. Left missing, they
# count as changes, and reap's dirty guard would keep the worktree for good.
restore_missing_tracked() {
  local wt="$1"
  [ -n "$(git -C "$wt" ls-files --deleted 2>/dev/null)" ] || return 0
  git -C "$wt" ls-files -z --deleted \
    | git -C "$wt" checkout -q --pathspec-from-file=- --pathspec-file-nul 2>/dev/null
}

# Remove linked worktree <path>: its directory and its registration, unlocking
# it first. Every removal of a worktree directory goes through here, so none
# can skip the link sweep. `git worktree remove --force` deletes a dirty
# worktree outright; a caller that must spare one checks before calling.
#
# With `fallback`, a failed `git worktree remove` is followed by `rm -rf`,
# which unlinks the links inside without following them; without it the
# worktree stays, whole again, for the caller to report.
#
# Returns 0 once it is removed, 1 if it could not be, 2 if a link survived
# the sweep (nothing is removed, since removing would reach through it), 3 if
# <path> is not a linked worktree (nothing is touched).
remove_worktree() {
  local path="${1:?}" fallback="${2:-}"
  is_linked_worktree "$path" || return 3
  unlink_links_in_worktree "$path" || return 2
  git worktree unlock "$path" >/dev/null 2>&1
  if git worktree remove --force "$path" >/dev/null 2>&1; then
    [ -n "$fallback" ] || return 0
  elif [ -z "$fallback" ]; then
    if [ -d "$path" ] && on_windows; then restore_missing_tracked "$path"; fi
    return 1
  fi
  # Also clears a symlink <path> named the worktree by, which git leaves.
  rm -rf "${path:?}"
  [ ! -e "$path" ] && [ ! -L "$path" ]
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

# Decide the fate of one worktree from `git worktree list`. <sp> is this
# session's scratchpad, or empty when it cannot be resolved.
consider() {
  local wt="$1" locked="$2" branch="$3" sp="$4"
  [ -n "$wt" ] || return 0
  if ! is_review_path "$wt"; then
    [ "$VERBOSE" = 0 ] || echo "kept $wt not-a-review-worktree"
    return 0
  fi

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

  if [ ! -d "$wt" ]; then
    # Directory already gone; this is registration cleanup only, not a
    # removal decision, so its outcome doesn't gate the message below.
    [ -n "$locked" ] && git worktree unlock "$wt" >/dev/null 2>&1
    git worktree remove --force "$wt" >/dev/null 2>&1
    echo "pruned $wt registration-only"
  else
    local rc=0
    remove_worktree "$wt" || rc=$?
    case "$rc" in
      0) echo "reaped $wt" ;;
      2) echo "error $wt links-remain"; return 0 ;;
      *)
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
        return 0 ;;
    esac
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
  # Resolved once: it depends only on the environment and the cwd, and on
  # Windows each resolution costs a cygpath process.
  local sp; sp=$(scratchpad_path) || sp=""
  local wt="" locked="" branch="" line
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      worktree\ *) consider "$wt" "$locked" "$branch" "$sp"; wt="${line#worktree }"; locked=""; branch="" ;;
      branch\ *)   branch="${line#branch }" ;;
      locked*)     locked="${line#locked}"; locked="${locked# }"; [ -n "$locked" ] || locked="(no reason)" ;;
    esac
  done < <(git worktree list --porcelain)
  consider "$wt" "$locked" "$branch" "$sp"
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
  # In git's own spelling, the same one `scratchpad` prints, so the paths a
  # caller builds from each agree and a native program can open either.
  local abs; abs=$(cd "$path" && pwd) || return 1
  printf '%s\n' "$(native_path "$abs")"
}

# Tear down worktree <path> unconditionally: unlike reap, no dirty guard.
# A path that does not exist is not an error, so a teardown can run whether or
# not the worktree was ever made; any registration it left is cleared.
# Anything else that is not a linked worktree is refused with `error:`, so a
# mistaken argument cannot delete the primary checkout or an unrelated tree.
cmd_remove() {
  local path="${1:-}"
  if [ ! -e "$path" ] && [ ! -L "$path" ]; then
    if [ -n "$path" ] && registered_worktree "$path"; then
      git worktree unlock "$path" >/dev/null 2>&1
      git worktree remove --force "$path" >/dev/null 2>&1
    fi
    git worktree prune >/dev/null 2>&1
    return 0
  fi
  local rc=0
  remove_worktree "$path" fallback || rc=$?
  git worktree prune >/dev/null 2>&1
  case "$rc" in
    0) return 0 ;;
    2) echo "error $path links-remain" >&2 ;;
    3) echo "error: not a linked worktree, so not removed: $path" >&2 ;;
    *) echo "error: could not remove $path" >&2 ;;
  esac
  return 1
}

cmd_remove_refuters() {
  local scratch="${1:-}" n="${2:-}"
  if [ -z "$scratch" ] || [ -z "$n" ]; then
    echo "usage: pr-review-wt.sh remove-refuters <scratchpad> <pr-number>" >&2; return 2
  fi
  local d
  for d in "$scratch"/wt-pr"$n"-r[ci][0-9]*; do
    [ -e "$d" ] || continue
    cmd_remove "$d" && echo "removed $d"
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
  if on_windows && ! symlinks_available "$wt"; then
    echo "declined symlinks-unavailable"; return 0
  fi

  # The physical path, since mirror_modules_entry compares it against `readlink -f` output.
  local main_real wt_real
  main_real=$(cd "$main_repo" && pwd -P) && wt_real=$(cd "$wt" && pwd -P) || {
    echo "error: cannot resolve the physical paths of $main_repo and $wt" >&2; return 1; }

  local relinked=0
  mirror_modules_dir "$main_repo/node_modules" "$wt/node_modules" || {
    echo "error: could not link node_modules into $wt" >&2; return 1; }

  # All or nothing: a package left without its node_modules fails with
  # module-not-found, which reads as a fault in the change under review. What
  # this run already linked goes too, since a root node_modules left behind
  # would answer the next run `declined already-present`.
  local extra=0 pkg made=("$wt/node_modules") failed=() d
  while IFS= read -r pkg; do
    pkg=$(dirname "$pkg"); [ "$pkg" = "." ] && continue
    if [ -d "$main_repo/$pkg/node_modules" ] && [ ! -e "$wt/$pkg/node_modules" ]; then
      if mirror_modules_dir "$main_repo/$pkg/node_modules" "$wt/$pkg/node_modules"; then
        made+=("$wt/$pkg/node_modules"); extra=$((extra+1))
      else
        failed+=("$wt/$pkg/node_modules")
      fi
    fi
  done < <(git -C "$wt" ls-files -- '*package.json')
  if [ "${#failed[@]}" -gt 0 ]; then
    for d in "${made[@]}"; do rm -rf "${d:?}"; done
    echo "error: could not link node_modules into ${failed[*]}" >&2; return 1
  fi
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
# `mkdir` that made it would have failed on anything already there. The same
# holds for cmd_link_deps removing what earlier calls made.
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

# `ln -s`, making a native symlink or failing. Git Bash (MSYS2) and Cygwin
# each copy the target or make an emulated link that Windows programs cannot
# follow unless opted in, and each reads only its own variable. nativestrict
# fails loudly rather than falling back. Scoped to the one call, never
# exported; both variables are meaningless elsewhere.
native_ln_s() {
  MSYS=winsymlinks:nativestrict CYGWIN=winsymlinks:nativestrict ln -s "$@"
}

# Whether this account can create a native symlink beside worktree <wt>. The
# probe sits on the worktree's own volume, since a FAT or exFAT drive or a
# network share can refuse links that the system temp directory allows; it
# falls back to the temp directory when the worktree's parent is not
# writable. It links exactly as the real links do, since the default copy
# instead of a link would always pass. A probe that cannot even run answers
# yes, and leaves the real links to report whatever is wrong.
symlinks_available() {
  local wt="$1" probe rc=0
  probe=$(mktemp -d "$(dirname "$wt")/.symlink-probe.XXXXXX" 2>/dev/null) \
    || probe=$(mktemp -d 2>/dev/null) || return 0
  [ -n "$probe" ] && [ -d "$probe" ] || return 0
  if mkdir "$probe/t" 2>/dev/null; then
    native_ln_s "$probe/t" "$probe/l" 2>/dev/null || rc=1
  fi
  rm -rf "${probe:?}"
  return "$rc"
}

# Link one node_modules entry. An entry that resolves to a tracked path of the
# primary checkout outside any node_modules is a workspace package (or a
# `file:` dependency): point it at the same path in the worktree, when the
# head still has it. Everything else - installed third-party packages, `.bin`,
# the `.pnpm` store - is shared with the primary checkout as before.
#
# Every link here must be a real symlink, made by native_ln_s. A copy would
# duplicate the whole of the primary checkout's node_modules into the
# worktree and sever the sharing this exists for.
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
              native_ln_s "$wt_real/$rel" "$dst" || return 1
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
  native_ln_s "$src" "$dst"
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
