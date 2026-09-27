#!/usr/bin/env bash
# Test harness for pr-review-wt.sh. Creates throwaway git repos under $TMPDIR.
# Expected: passed=99 failed=0
set -uo pipefail
# Resolve to an absolute path before any test cd's into a throwaway repo dir.
SCRIPT="$(cd "$(dirname "$0")" && pwd)/pr-review-wt.sh"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  ok   - $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL - $1"; }
check(){ if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (want '$3', got '$2')"; fi; }

setup_repo() {
  R=$(mktemp -d); cd "$R" || exit 1
  git init -q .; git config user.email t@t; git config user.name t
  echo hi > f.txt; git add f.txt; git commit -qm init
  SHA=$(git rev-parse HEAD)
}

age_dir() { touch -d "$2 hours ago" "$1"; }

# Paths the script reports are spelled in git's own path form, so a POSIX
# expectation has to be converted the same way the script converts it.
# cygpath ships only with MSYS/Cygwin, so this is identity off Windows and
# every such expectation stays byte-for-byte what it was there.
native() {
  if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi
}

# Git Bash's `ln -s` copies the target instead of linking unless MSYS opts in,
# which would leave a fixture that asserts on a symlink with a directory copy
# and make the test vacuous. Scoped to each call, never exported: the script
# under test has to opt in for itself or its own bare `ln -s` goes undetected.
ln_s() { MSYS=winsymlinks:nativestrict ln -s "$@"; }

echo "== reap: removes an old, unlocked review worktree =="
setup_repo
git worktree add -q --detach .claude/worktrees/review-999 "$SHA"
age_dir .claude/worktrees/review-999 48
OUT=$("$SCRIPT" reap 2>&1)
check "reports it reaped" "$(echo "$OUT" | grep -c '^reaped .*review-999$')" "1"
check "worktree is gone"  "$(git worktree list | grep -c review-999)" "0"
check "directory is gone" "$([ -d .claude/worktrees/review-999 ] && echo yes || echo no)" "no"

echo "== reap: keeps a recent worktree =="
setup_repo
git worktree add -q --detach .claude/worktrees/review-111 "$SHA"
OUT=$("$SCRIPT" reap 2>&1)
check "kept recent" "$(git worktree list | grep -c review-111)" "1"

echo "== reap: keeps a worktree locked by a LIVE pid =="
setup_repo
git worktree add -q --detach .claude/worktrees/review-222 "$SHA"
age_dir .claude/worktrees/review-222 48
git worktree lock --reason "pr-review session x (pid $$)" .claude/worktrees/review-222
OUT=$("$SCRIPT" reap 2>&1)
check "kept live-locked" "$(git worktree list | grep -c review-222)" "1"

echo "== reap: removes a worktree locked by a DEAD pid =="
setup_repo
git worktree add -q --detach .claude/worktrees/review-333 "$SHA"
age_dir .claude/worktrees/review-333 48
DEADPID=$(( $(cat /proc/sys/kernel/pid_max) - 1 ))
git worktree lock --reason "pr-review session y (pid $DEADPID)" .claude/worktrees/review-333
OUT=$("$SCRIPT" reap 2>&1)
check "reaped stale-locked" "$(git worktree list | grep -c review-333)" "0"

echo "== reap: ignores non-review worktrees =="
setup_repo
git worktree add -q --detach feature-work "$SHA"
age_dir feature-work 999
OUT=$("$SCRIPT" reap 2>&1)
check "left feature worktree" "$(git worktree list | grep -c feature-work)" "1"

echo "== reap: never touches the current session scratchpad =="
setup_repo
mkdir -p scratchpad
git worktree add -q --detach scratchpad/wt-pr42 "$SHA"
age_dir scratchpad/wt-pr42 999
OUT=$(CLAUDE_SCRATCHPAD="$R/scratchpad" "$SCRIPT" reap 2>&1)
check "kept own session wt" "$(git worktree list | grep -c wt-pr42)" "1"

echo "== reap: reports a stranded branch without deleting it =="
setup_repo
git worktree add -q -b strandme .claude/worktrees/review-444 "$SHA"
age_dir .claude/worktrees/review-444 48
OUT=$("$SCRIPT" reap 2>&1)
check "reported stranded" "$(echo "$OUT" | grep -c '^stranded-branch strandme')" "1"
check "branch still exists" "$(git branch --list strandme | wc -l)" "1"

echo "== reap: keeps a DIRTY worktree (modified tracked + untracked file) =="
setup_repo
git worktree add -q --detach .claude/worktrees/review-555 "$SHA"
echo modified > .claude/worktrees/review-555/f.txt
echo untracked > .claude/worktrees/review-555/new.txt
age_dir .claude/worktrees/review-555 48
OUT=$("$SCRIPT" reap 2>&1)
check "reports dirty" "$(echo "$OUT" | grep -c '^kept .*review-555 dirty-')" "1"
check "worktree still registered" "$(git worktree list | grep -c review-555)" "1"
check "modified file survives" "$(cat .claude/worktrees/review-555/f.txt)" "modified"
check "untracked file survives" "$(cat .claude/worktrees/review-555/new.txt)" "untracked"

echo "== reap: reaps an aged, unlocked verify- worktree =="
setup_repo
git worktree add -q --detach .claude/worktrees/verify-777 "$SHA"
age_dir .claude/worktrees/verify-777 48
OUT=$("$SCRIPT" reap 2>&1)
check "reports it reaped verify-" "$(echo "$OUT" | grep -c '^reaped .*verify-777$')" "1"
check "verify worktree is gone" "$(git worktree list | grep -c verify-777)" "0"

echo "== create: makes a detached, locked worktree =="
setup_repo
mkdir -p scratchpad
P=$("$SCRIPT" create "$R/scratchpad/wt-pr7" "$SHA")
check "path echoed"    "$(basename "$P")" "wt-pr7"
check "exists"         "$([ -d "$R/scratchpad/wt-pr7" ] && echo yes || echo no)" "yes"
check "is detached"    "$(git -C "$R/scratchpad/wt-pr7" symbolic-ref -q HEAD >/dev/null && echo branch || echo detached)" "detached"
check "is locked"      "$(git worktree list --porcelain | grep -c '^locked')" "1"
check "no new branch"  "$(git branch --list | wc -l)" "1"

echo "== create: its own lock protects it from reap =="
age_dir "$R/scratchpad/wt-pr7" 999
OUT=$("$SCRIPT" reap 2>&1)
check "survives reap" "$(git worktree list | grep -c wt-pr7)" "1"

echo "== remove: tears down cleanly =="
"$SCRIPT" remove "$R/scratchpad/wt-pr7"
check "worktree gone" "$(git worktree list | grep -c wt-pr7)" "0"
check "dir gone"      "$([ -d "$R/scratchpad/wt-pr7" ] && echo yes || echo no)" "no"

echo "== remove: is idempotent =="
"$SCRIPT" remove "$R/scratchpad/wt-pr7"
check "second remove exits 0" "$?" "0"

echo "== remove: resolves a symlinked path to the registered worktree =="
setup_repo
mkdir -p scratchpad
"$SCRIPT" create "$R/scratchpad/wt-real" "$SHA" >/dev/null
ln_s "$R/scratchpad/wt-real" "$R/scratchpad/wt-link"
"$SCRIPT" remove "$R/scratchpad/wt-link"
check "worktree deregistered" "$(git worktree list | grep -c wt-real)" "0"
check "real dir gone" "$([ -d "$R/scratchpad/wt-real" ] && echo yes || echo no)" "no"

echo "== scratchpad: honours an explicit \$CLAUDE_SCRATCHPAD =="
setup_repo
OUT=$(CLAUDE_SCRATCHPAD=/some/where/scratchpad "$SCRIPT" scratchpad)
check "echoes it in git path form" "$OUT" "$(native /some/where/scratchpad)"
OUT=$(CLAUDE_SCRATCHPAD=/some/where/scratchpad/ "$SCRIPT" scratchpad)
check "strips a trailing slash" "$OUT" "$(native /some/where/scratchpad)"

echo "== scratchpad: derives the path from \$CLAUDE_CODE_SESSION_ID =="
setup_repo
CWD=$(pwd -P)
WANT=$(native "/tmp/claude-$(id -u)/$(printf '%s' "$CWD" | tr / -)/sess-abc/scratchpad")
OUT=$(env -u CLAUDE_SCRATCHPAD CLAUDE_CODE_SESSION_ID=sess-abc "$SCRIPT" scratchpad)
check "derived path"    "$OUT" "$WANT"
OUT=$(env -u CLAUDE_SCRATCHPAD -u CLAUDE_CODE_SESSION_ID "$SCRIPT" scratchpad 2>/dev/null)
check "unresolvable prints nothing" "$OUT" ""
env -u CLAUDE_SCRATCHPAD -u CLAUDE_CODE_SESSION_ID "$SCRIPT" scratchpad >/dev/null 2>&1
check "unresolvable exits nonzero"  "$([ $? -ne 0 ] && echo yes || echo no)" "yes"

echo "== reap: keeps an aged worktree inside the DERIVED scratchpad =="
setup_repo
SP="/tmp/claude-$(id -u)/$(pwd -P | tr / -)/sess-derive/scratchpad"
mkdir -p "$SP"
git worktree add -q --detach "$SP/wt-pr88" "$SHA"
age_dir "$SP/wt-pr88" 999
OUT=$(env -u CLAUDE_SCRATCHPAD CLAUDE_CODE_SESSION_ID=sess-derive "$SCRIPT" reap 2>&1)
check "reports current-session"  "$(echo "$OUT" | grep -c "^kept $(native "$SP")/wt-pr88 current-session$")" "1"
check "still registered"         "$(git worktree list | grep -c wt-pr88)" "1"
check "directory survives"       "$([ -d "$SP/wt-pr88" ] && echo yes || echo no)" "yes"
# Same worktree, no session id to derive from: the exclusion cannot resolve and
# must NOT degrade into "keep everything" - the age floor takes over and reaps.
OUT=$(env -u CLAUDE_SCRATCHPAD -u CLAUDE_CODE_SESSION_ID "$SCRIPT" reap 2>&1)
check "unresolvable does not keep" "$(git worktree list | grep -c wt-pr88)" "0"
rm -rf "/tmp/claude-$(id -u)/$(pwd -P | tr / -)"

echo "== reap: suppresses not-a-review-worktree noise unless -v =="
setup_repo
git worktree add -q --detach feature-quiet "$SHA"
OUT=$("$SCRIPT" reap 2>&1)
check "quiet by default" "$(echo "$OUT" | grep -c 'not-a-review-worktree')" "0"
OUT=$("$SCRIPT" reap -v 2>&1)
# Two lines under -v: the feature worktree AND the repo's own main worktree,
# which `git worktree list` always reports and which is never a review path.
check "shown with -v"    "$(echo "$OUT" | grep -c 'feature-quiet not-a-review-worktree')" "1"
check "-v also names the main worktree" "$(echo "$OUT" | grep -c 'not-a-review-worktree')" "2"

echo "== scratchpad: rejects a value that is not an absolute path =="
setup_repo
# Each of these would otherwise hand a caller "" and expand "$SCRATCH/wt-prN"
# to "/wt-prN" - the very failure the subcommand exists to prevent.
for BAD in "/" "//" "relative/path" ""; do
  OUT=$(env -u CLAUDE_CODE_SESSION_ID CLAUDE_SCRATCHPAD="$BAD" "$SCRIPT" scratchpad 2>/dev/null)
  RC=$?
  check "rejects '$BAD' (stdout empty)" "$OUT" ""
  check "rejects '$BAD' (nonzero exit)" "$([ "$RC" -ne 0 ] && echo yes || echo no)" "yes"
done
ERR=$(env -u CLAUDE_CODE_SESSION_ID CLAUDE_SCRATCHPAD=/ "$SCRIPT" scratchpad 2>&1 >/dev/null)
check "bare / gets a curated error" "$(printf '%s' "$ERR" | grep -c '^error: ')" "1"
# An explicit but invalid value must not silently fall through to derivation.
OUT=$(env CLAUDE_CODE_SESSION_ID=sess-x CLAUDE_SCRATCHPAD=/ "$SCRIPT" scratchpad 2>/dev/null)
check "bad explicit value does not fall back to derivation" "$OUT" ""

echo "== scratchpad: derives from the REAL cwd through a symlink =="
setup_repo
mkdir -p "$R/real"; ln_s "$R/real" "$R/link"
OUT=$(cd "$R/link" && env -u CLAUDE_SCRATCHPAD CLAUDE_CODE_SESSION_ID=sess-sym "$SCRIPT" scratchpad)
WANT=$(native "/tmp/claude-$(id -u)/$(cd "$R/real" && pwd -P | tr / -)/sess-sym/scratchpad")
check "uses pwd -P not pwd" "$OUT" "$WANT"

echo "== reap: rejects an unrecognized argument =="
setup_repo
git worktree add -q --detach .claude/worktrees/review-666 "$SHA"
age_dir .claude/worktrees/review-666 48
ERR=$("$SCRIPT" reap --verbos 2>&1 >/dev/null); RC=$?
check "mistyped flag exits nonzero" "$([ "$RC" -ne 0 ] && echo yes || echo no)" "yes"
check "prints the usage line"       "$(printf '%s' "$ERR" | grep -c '^usage: ')" "1"
check "reaped nothing"              "$(git worktree list | grep -c review-666)" "1"
ERR=$("$SCRIPT" reap -v extra 2>&1 >/dev/null); RC=$?
check "extra argument exits nonzero" "$([ "$RC" -ne 0 ] && echo yes || echo no)" "yes"


echo "== link-deps: links when no lockfile changed =="
setup_repo
mkdir -p node_modules && echo x > node_modules/marker
echo '{}' > package-lock.json; git add package-lock.json; git commit -qm lock
BASE=$(git rev-parse HEAD)
echo more >> f.txt; git add f.txt; git commit -qm work
HEAD_SHA=$(git rev-parse HEAD)
WT=$(mktemp -d)/wt
"$SCRIPT" create "$WT" "$HEAD_SHA" >/dev/null
OUT=$("$SCRIPT" link-deps "$WT" "$BASE" 2>&1)
check "reports linked"         "$(echo "$OUT" | grep -c '^linked ')" "1"
check "marker is reachable"    "$([ -f "$WT/node_modules/marker" ] && echo yes || echo no)" "yes"

echo "== link-deps: declines when a lockfile changed =="
setup_repo
mkdir -p node_modules && echo x > node_modules/marker
echo '{}' > package-lock.json; git add package-lock.json; git commit -qm lock
BASE=$(git rev-parse HEAD)
echo '{"changed":1}' > package-lock.json; git add package-lock.json; git commit -qm bump
HEAD_SHA=$(git rev-parse HEAD)
WT=$(mktemp -d)/wt
"$SCRIPT" create "$WT" "$HEAD_SHA" >/dev/null
OUT=$("$SCRIPT" link-deps "$WT" "$BASE" 2>&1)
check "reports declined"       "$(echo "$OUT" | grep -c '^declined lockfile-changed')" "1"
check "nothing was linked"     "$([ -e "$WT/node_modules" ] && echo yes || echo no)" "no"

echo "== link-deps: declines when the primary checkout has no node_modules =="
setup_repo
BASE=$(git rev-parse HEAD)
WT=$(mktemp -d)/wt
"$SCRIPT" create "$WT" "$BASE" >/dev/null
OUT=$("$SCRIPT" link-deps "$WT" "$BASE" 2>&1)
check "reports declined"       "$(echo "$OUT" | grep -c '^declined no-node_modules')" "1"

echo "== link-deps: refuses without both arguments =="
setup_repo
"$SCRIPT" link-deps >/dev/null 2>&1; RC=$?
check "usage exit code"        "$RC" "2"

echo "== reap: a stale dirty refuter worktree is disposable =="
setup_repo
R=$(mktemp -d); mkdir -p "$R/other-session/scratchpad"
SP="$R/other-session/scratchpad"
"$SCRIPT" create "$SP/wt-pr5-rc3" HEAD >/dev/null
"$SCRIPT" create "$SP/wt-pr5" HEAD >/dev/null
# create records this test shell's pid, which is alive - unlock so the dirty
# rule, not the live-lock rule, is what decides.
git worktree unlock "$SP/wt-pr5-rc3"; git worktree unlock "$SP/wt-pr5"
echo dirt > "$SP/wt-pr5-rc3/probe.txt"; echo dirt > "$SP/wt-pr5/probe.txt"
age_dir "$SP/wt-pr5-rc3" 30; age_dir "$SP/wt-pr5" 30
OUT=$(CLAUDE_SCRATCHPAD=/elsewhere/scratchpad "$SCRIPT" reap 2>&1)
check "dirty refuter wt reaped"   "$(git worktree list | grep -c wt-pr5-rc3)" "0"
check "dirty main wt kept"        "$(echo "$OUT" | grep -c "kept $(native "$SP")/wt-pr5 dirty-")" "1"

echo "== reap: current-session refuter worktree is kept even when dirty =="
setup_repo
SP=$(mktemp -d)/scratchpad; mkdir -p "$SP"
"$SCRIPT" create "$SP/wt-pr5-ri12" HEAD >/dev/null
git worktree unlock "$SP/wt-pr5-ri12"
echo dirt > "$SP/wt-pr5-ri12/probe.txt"; age_dir "$SP/wt-pr5-ri12" 30
OUT=$(CLAUDE_SCRATCHPAD="$SP" "$SCRIPT" reap 2>&1)
check "current-session kept"      "$(git worktree list | grep -c wt-pr5-ri12)" "1"

echo "== remove-refuters removes every refuter worktree for the PR only =="
setup_repo
SP=$(mktemp -d)/scratchpad; mkdir -p "$SP"
for w in wt-pr5-rc1 wt-pr5-ri2 wt-pr5 wt-pr6-rc1; do "$SCRIPT" create "$SP/$w" HEAD >/dev/null; done
echo dirt > "$SP/wt-pr5-rc1/x"
"$SCRIPT" remove-refuters "$SP" 5 >/dev/null
check "rc1 gone"   "$([ -d "$SP/wt-pr5-rc1" ] && echo yes || echo no)" "no"
check "ri2 gone"   "$([ -d "$SP/wt-pr5-ri2" ] && echo yes || echo no)" "no"
check "main kept"  "$([ -d "$SP/wt-pr5" ] && echo yes || echo no)" "yes"
check "other PR kept" "$([ -d "$SP/wt-pr6-rc1" ] && echo yes || echo no)" "yes"

echo "== remove-refuters: refuses without both arguments =="
setup_repo
ERR=$("$SCRIPT" remove-refuters 2>&1 >/dev/null); RC=$?
check "no args: exit code"        "$RC" "2"
check "no args: usage message"    "$(printf '%s' "$ERR" | grep -c '^usage: pr-review-wt.sh remove-refuters')" "1"
ERR=$("$SCRIPT" remove-refuters "$R/scratchpad" 2>&1 >/dev/null); RC=$?
check "missing pr-number: exit code" "$RC" "2"

echo "== primary-status reports the primary checkout, not the worktree =="
setup_repo
echo change >> f.txt
WT=$(mktemp -d)/wt; "$SCRIPT" create "$WT" HEAD >/dev/null
check "primary dirt shown" "$("$SCRIPT" primary-status "$WT" | grep -c 'f.txt')" "1"

echo "== primary-status: refuses without an argument =="
setup_repo
ERR=$("$SCRIPT" primary-status 2>&1 >/dev/null); RC=$?
check "no arg: exit code"      "$RC" "2"
check "no arg: usage message"  "$(printf '%s' "$ERR" | grep -c '^usage: pr-review-wt.sh primary-status')" "1"

echo "== primary-status: errors on a path that is not a git worktree =="
NOTWT=$(mktemp -d)
ERR=$("$SCRIPT" primary-status "$NOTWT" 2>&1 >/dev/null); RC=$?
check "not-a-worktree: exit code"     "$RC" "1"
check "not-a-worktree: error message" "$(printf '%s' "$ERR" | grep -Fc "error: not a git worktree: $NOTWT")" "1"

echo "== link-deps: declines when pnpm-lock.yaml changed =="
setup_repo
mkdir -p node_modules && echo x > node_modules/marker
echo 'a: 1' > pnpm-lock.yaml; git add pnpm-lock.yaml; git commit -qm lock
BASE=$(git rev-parse HEAD)
echo 'a: 2' > pnpm-lock.yaml; git add pnpm-lock.yaml; git commit -qm bump
WT=$(mktemp -d)/wt; "$SCRIPT" create "$WT" HEAD >/dev/null
check "pnpm lock declines" "$("$SCRIPT" link-deps "$WT" "$BASE" | grep -c '^declined lockfile-changed:pnpm-lock.yaml')" "1"

echo "== link-deps: links per-package node_modules too =="
setup_repo
mkdir -p node_modules libs/a/node_modules && echo x > libs/a/node_modules/m
echo node_modules > .gitignore
echo '{}' > libs/a/package.json; git add libs/a/package.json; git commit -qm pkg
BASE=$(git rev-parse HEAD)
WT=$(mktemp -d)/wt; "$SCRIPT" create "$WT" HEAD >/dev/null
"$SCRIPT" link-deps "$WT" "$BASE" >/dev/null
check "package node_modules linked" "$([ -f "$WT/libs/a/node_modules/m" ] && echo yes || echo no)" "yes"

echo "== link-deps: never overwrites an existing per-package node_modules =="
setup_repo
mkdir -p node_modules libs/a/node_modules && echo x > libs/a/node_modules/m
echo node_modules > .gitignore
echo '{}' > libs/a/package.json; git add libs/a/package.json; git commit -qm pkg
BASE=$(git rev-parse HEAD)
WT=$(mktemp -d)/wt; "$SCRIPT" create "$WT" HEAD >/dev/null
mkdir -p "$WT/libs/a/node_modules"; echo original > "$WT/libs/a/node_modules/existing-marker"
"$SCRIPT" link-deps "$WT" "$BASE" >/dev/null
check "existing marker survives untouched"     "$(cat "$WT/libs/a/node_modules/existing-marker" 2>/dev/null)" "original"
check "existing node_modules stays a real dir" "$([ -L "$WT/libs/a/node_modules" ] && echo symlink || echo dir)" "dir"
# `ln -s SRC DEST` treats an existing DEST *directory* as a container and
# links SRC's basename inside it instead of failing - so without the guard,
# link-deps would silently drop a stray node_modules/node_modules symlink
# into the pre-existing directory rather than leaving it alone.
check "no stray nested symlink is dropped in"  "$([ -e "$WT/libs/a/node_modules/node_modules" ] && echo yes || echo no)" "no"

echo "== link-deps: workspace packages resolve to the worktree, not the primary checkout =="
# A pnpm workspace links each workspace package into node_modules with a relative
# symlink (node_modules/a -> ../libs/a). Linking the whole node_modules directory
# resolves that link against the PRIMARY checkout, so a test in the worktree
# silently imports the primary checkout's source instead of the head under review.
setup_repo
mkdir -p libs/a libs/b node_modules/@s node_modules/dep libs/b/node_modules
echo node_modules > .gitignore
echo '{}' > libs/a/package.json; echo '{}' > libs/b/package.json
echo head > libs/a/index.js
git add .gitignore libs; git commit -qm pkgs
echo primary > libs/a/index.js   # the primary checkout's working copy differs from the head
ln_s ../libs/a node_modules/a
ln_s ../../libs/a node_modules/@s/a
ln_s ../../a libs/b/node_modules/a
echo x > node_modules/dep/index.js
BASE=$(git rev-parse HEAD)
WT=$(mktemp -d)/wt; "$SCRIPT" create "$WT" HEAD >/dev/null
OUT=$("$SCRIPT" link-deps "$WT" "$BASE" 2>&1)
check "reports linked"                        "$(echo "$OUT" | grep -c '^linked ')" "1"
check "root workspace link reads the head"    "$(cat "$WT/node_modules/a/index.js" 2>/dev/null)" "head"
check "scoped workspace link reads the head"  "$(cat "$WT/node_modules/@s/a/index.js" 2>/dev/null)" "head"
check "package workspace link reads the head" "$(cat "$WT/libs/b/node_modules/a/index.js" 2>/dev/null)" "head"
check "third-party dep still reachable"       "$(cat "$WT/node_modules/dep/index.js" 2>/dev/null)" "x"
check "third-party dep is the primary's"      "$(readlink -f "$WT/node_modules/dep")" "$(readlink -f "$R/node_modules/dep")"
mkdir -p "$WT/node_modules/.vite"; echo cache > "$WT/node_modules/.vite/c"
check "a cache written in the worktree stays out of the primary" "$([ -e "$R/node_modules/.vite/c" ] && echo leaked || echo kept)" "kept"

echo "== scratchpad: prints the same spelling git registers a worktree under =="
setup_repo
SP="$R/scratchpad"; mkdir -p "$SP"
git worktree add -q --detach "$SP/wt-pr91" "$SHA"
# Callers filter `git worktree list` by this value, and consider() prefix-
# matches it against the same output, so the two spellings of one directory
# have to collapse to a single spelling before either comparison happens.
OUT=$(env -u CLAUDE_CODE_SESSION_ID CLAUDE_SCRATCHPAD="$SP" "$SCRIPT" scratchpad)
check "prefixes the registered worktree" \
  "$(git worktree list --porcelain | grep -cxF "worktree $OUT/wt-pr91")" "1"

echo "== scratchpad: honours a \$CLAUDE_SCRATCHPAD in git's own path form =="
setup_repo
SP="$R/scratchpad"; mkdir -p "$SP"
git worktree add -q --detach "$SP/wt-pr92" "$SHA"
age_dir "$SP/wt-pr92" 999
# Either spelling names the same directory, and a value the platform hands
# over in the other one must not read as "not an absolute path" and be
# discarded - that would drop the exclusion protecting the live worktree.
OUT=$(env -u CLAUDE_CODE_SESSION_ID CLAUDE_SCRATCHPAD="$(native "$SP")" "$SCRIPT" scratchpad); RC=$?
check "accepted, not rejected"          "$RC" "0"
check "resolves to the same directory"  "$OUT" "$(native "$SP")"
OUT=$(env -u CLAUDE_CODE_SESSION_ID CLAUDE_SCRATCHPAD="$(native "$SP")" "$SCRIPT" reap 2>&1)
check "protects the session's worktree" "$(git worktree list | grep -c wt-pr92)" "1"

echo "== primary-status resolves the primary checkout without a git error =="
setup_repo
echo change >> f.txt
WT=$(mktemp -d)/wt; "$SCRIPT" create "$WT" HEAD >/dev/null
ERR=$("$SCRIPT" primary-status "$WT" 2>&1 >/dev/null); RC=$?
# An absolute --git-common-dir mistaken for a relative one is joined onto the
# worktree path, and git then fails on a directory that cannot exist.
check "exits 0"                 "$RC" "0"
check "nothing on stderr"       "$ERR" ""

echo "== remove: unlocks and deregisters, not just deletes the directory =="
setup_repo
mkdir -p scratchpad
"$SCRIPT" create "$R/scratchpad/wt-pr93" "$SHA" >/dev/null
"$SCRIPT" remove "$R/scratchpad/wt-pr93"
# `git worktree prune` skips a locked worktree, so a lock still standing here
# means the path never matched its registration and nothing but the directory
# was removed - leaving a registration no later prune can ever clear.
check "no lock left behind" "$(git worktree list --porcelain | grep -c '^locked')" "0"
check "deregistered"        "$(git worktree list | grep -c wt-pr93)" "0"

echo "== link-deps shares by link, never by copy =="
# Copying instead of linking duplicates the whole of the primary checkout's
# node_modules into the worktree and stops it tracking either tree, which
# reads as a successful link and is not one.
setup_repo
mkdir -p libs/a node_modules/dep
echo node_modules > .gitignore
echo '{}' > libs/a/package.json; echo head > libs/a/index.js
git add .gitignore libs; git commit -qm pkgs
ln_s ../libs/a node_modules/a
echo x > node_modules/dep/index.js
BASE=$(git rev-parse HEAD)
WT=$(mktemp -d)/wt; "$SCRIPT" create "$WT" HEAD >/dev/null
"$SCRIPT" link-deps "$WT" "$BASE" >/dev/null
check "third-party entry is a symlink" "$([ -L "$WT/node_modules/dep" ] && echo yes || echo no)" "yes"
check "workspace entry is a symlink"   "$([ -L "$WT/node_modules/a" ] && echo yes || echo no)" "yes"
# Written after linking, so only a live link can reach it.
echo late > "$R/node_modules/dep/late.js"
check "third-party entry tracks the primary" "$(cat "$WT/node_modules/dep/late.js" 2>/dev/null)" "late"
echo late > "$WT/libs/a/late.js"
check "workspace entry tracks the worktree"  "$(cat "$WT/node_modules/a/late.js" 2>/dev/null)" "late"

echo; echo "passed=$PASS failed=$FAIL"; [ "$FAIL" -eq 0 ]
