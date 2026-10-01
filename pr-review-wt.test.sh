#!/usr/bin/env bash
# Test harness for pr-review-wt.sh. Creates throwaway git repos under $TMPDIR.
# Expected: passed=143 failed=0 (passed=148 under Git for Windows, which runs
# the checks guarded by on_windows in place of the one guarded by its negation)
set -uo pipefail
# An MSYS or CYGWIN exported by the developer's shell would reach the
# script's own `ln -s` calls and make them link even where the script forgot
# to opt in.
unset MSYS CYGWIN
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

# cygpath ships only with MSYS/Cygwin, so its presence marks a Windows run.
# Checks guarded by it test what only Windows can get wrong, which is why the
# pass count differs between platforms.
on_windows() { command -v cygpath >/dev/null 2>&1; }

# The directory name Claude Code derives from a cwd: every character outside
# [A-Za-z0-9] becomes '-'. The literal-name checks under "scratchpad:" pin this
# independently, so a regression here cannot hide behind a matching helper.
harness_name() { printf '%s' "$1" | LC_ALL=C tr -c 'A-Za-z0-9' '-'; }

# Paths the script reports are spelled in git's own path form, so a POSIX
# input has to be converted the same way the script converts it. Identity
# off Windows.
native() {
  if on_windows; then cygpath -m "$1"; else printf '%s' "$1"; fi
}

# The spelling Windows itself hands out, backslashes and all.
winpath() {
  if on_windows; then cygpath -w "$1"; else printf '%s' "$1"; fi
}

# Windows only. A directory junction, as pnpm and npm workspaces create them.
junction() { MSYS_NO_PATHCONV=1 cmd /c mklink /J "$(cygpath -w "$1")" "$(cygpath -w "$2")" >/dev/null; }

# Windows only. Read <path> under directory <dir> the way a Windows program
# does. MSYS tools follow links that native programs such as node.exe cannot
# open, so `cat` alone cannot tell a usable link from an unusable one. Only
# <dir> goes through cygpath, which resolves every MSYS link it converts and
# would hand cmd the target instead of the link; <dir> must contain none.
wintype() {
  local bs='\' rest="$2"
  MSYS_NO_PATHCONV=1 cmd /c type "$(cygpath -w "$1")$bs${rest//\//$bs}" 2>/dev/null | tr -d '\r'
}

# Parent directory of worktree <name> as git registered it. An expectation
# built by converting the test's own path would only show that the
# conversion agrees with itself.
registered_parent() {
  git worktree list --porcelain | sed -n "s#^worktree \\(.*\\)/$1\$#\\1#p"
}

# Git Bash's `ln -s` copies the target instead of linking unless MSYS opts in,
# which would leave a fixture that asserts on a symlink with a directory copy
# and make the test vacuous. Scoped to each call, never exported: the script
# under test has to opt in for itself or its own bare `ln -s` goes undetected.
ln_s() { MSYS=winsymlinks:nativestrict ln -s "$@"; }

# A PATH under which the script takes its Windows code paths. On Windows that
# is the PATH as it is; elsewhere a stand-in cygpath goes first, converting
# drive letters the way the real one does and leaving other paths alone, so
# checks of Windows-only logic run on every platform.
WINSIM=$PATH
if ! on_windows; then
  CYGSTUB=$(mktemp -d)
  cat > "$CYGSTUB/cygpath" <<'EOF'
#!/usr/bin/env bash
mode=$1 p=$2
case "$mode" in
  -u) p=${p//\\//}
      case "$p" in [A-Za-z]:*) d=${p%%:*}; p="/${d,,}${p:2}" ;; esac ;;
  -m) case "$p" in
        /[A-Za-z]|/[A-Za-z]/*) d=${p:1:1}; p="${d^^}:${p:2}"; [ "${#p}" -gt 2 ] || p="$p/" ;;
      esac ;;
esac
printf '%s\n' "$p"
EOF
  chmod +x "$CYGSTUB/cygpath"
  WINSIM="$CYGSTUB:$PATH"
fi

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
# A pid that has just exited. Git Bash has no /proc/sys, so anything derived
# from pid_max there is not a pid at all and never reaches the liveness check.
sleep 0 & DEADPID=$!; wait "$DEADPID"
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
# In the spelling git registers it under, which is also the one `scratchpad`
# prints, so paths a caller builds from the two agree.
check "path echoed is the registered one" "$(git worktree list --porcelain | grep -cxF "worktree $P")" "1"
check "exists"         "$([ -d "$R/scratchpad/wt-pr7" ] && echo yes || echo no)" "yes"
check "is detached"    "$(git -C "$R/scratchpad/wt-pr7" symbolic-ref -q HEAD >/dev/null && echo branch || echo detached)" "detached"
check "is locked"      "$(git worktree list --porcelain | grep -c '^locked')" "1"
check "no new branch"  "$(git branch --list | wc -l)" "1"

echo "== create: its own lock protects it from reap =="
age_dir "$R/scratchpad/wt-pr7" 999
OUT=$("$SCRIPT" reap 2>&1)
check "survives reap" "$(git worktree list | grep -c wt-pr7)" "1"

echo "== remove: tears down cleanly =="
if on_windows; then
  # On Windows `git worktree remove --force` deletes what a junction inside
  # the worktree points to, even outside it.
  OUTSIDE=$(mktemp -d); echo keep > "$OUTSIDE/file"
  junction "$R/scratchpad/wt-pr7/j" "$OUTSIDE"
fi
"$SCRIPT" remove "$P"
# `git worktree prune` skips a locked worktree, so a registration still here
# means the path never matched it and only the directory was removed -
# leaving a registration no later prune can ever clear.
check "worktree gone" "$(git worktree list | grep -c wt-pr7)" "0"
check "dir gone"      "$([ -d "$R/scratchpad/wt-pr7" ] && echo yes || echo no)" "no"
if on_windows; then
  check "a junction's target outside the worktree survives" "$(cat "$OUTSIDE/file" 2>/dev/null)" "keep"
fi

echo "== remove: is idempotent =="
"$SCRIPT" remove "$R/scratchpad/wt-pr7"
check "second remove exits 0" "$?" "0"

echo "== remove: refuses anything that is not a linked worktree =="
setup_repo
mkdir -p scratchpad
"$SCRIPT" create "$R/scratchpad/wt-pr8" "$SHA" >/dev/null
mkdir "$R/scratchpad/wt-pr8/sub"; echo keep > "$R/scratchpad/wt-pr8/sub/file"
PLAIN=$(mktemp -d); echo keep > "$PLAIN/file"
# A submodule's .git is a file too, but names .git/modules/, not .git/worktrees/.
SUBMOD=$(mktemp -d); echo keep > "$SUBMOD/file"
printf 'gitdir: %s\n' "$R/.git/modules/sub" > "$SUBMOD/.git"
for T in "$R" "$PLAIN" "$SUBMOD" "$R/scratchpad/wt-pr8/sub"; do
  case "$T" in
    "$R")      NAME="the primary checkout";       KEEP="$R/f.txt";      WANT=hi ;;
    "$PLAIN")  NAME="a plain directory";          KEEP="$PLAIN/file";   WANT=keep ;;
    "$SUBMOD") NAME="a submodule-style checkout"; KEEP="$SUBMOD/file";  WANT=keep ;;
    *)         NAME="a worktree's subdirectory";  KEEP="$T/file";       WANT=keep ;;
  esac
  ERR=$("$SCRIPT" remove "$T" 2>&1 >/dev/null); RC=$?
  check "$NAME: exits nonzero" "$([ "$RC" -ne 0 ] && echo yes || echo no)" "yes"
  check "$NAME: curated error" "$(printf '%s' "$ERR" | grep -c '^error: ')" "1"
  check "$NAME: survives"      "$(cat "$KEEP" 2>/dev/null)" "$WANT"
done
check "the primary checkout keeps its .git" "$([ -d "$R/.git" ] && echo yes || echo no)" "yes"

echo "== remove: a missing path is not an error, and clears its registration =="
OUT=$("$SCRIPT" remove "$R/scratchpad/wt-never" 2>&1); RC=$?
check "never-created path: silent, exits 0" "$OUT rc=$RC" " rc=0"
rm -rf "${R:?}/scratchpad/wt-pr8"
OUT=$("$SCRIPT" remove "$R/scratchpad/wt-pr8" 2>&1); RC=$?
check "deleted worktree: silent, exits 0" "$OUT rc=$RC" " rc=0"
# The registration is locked, so no prune would ever clear it.
check "its locked registration is gone" "$(git worktree list | grep -c wt-pr8)" "0"

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
WANT=$(native "/tmp/claude-$(id -u)/$(harness_name "$CWD")/sess-abc/scratchpad")
OUT=$(env -u CLAUDE_SCRATCHPAD CLAUDE_CODE_SESSION_ID=sess-abc "$SCRIPT" scratchpad)
check "derived path"    "$OUT" "$WANT"
OUT=$(env -u CLAUDE_SCRATCHPAD -u CLAUDE_CODE_SESSION_ID "$SCRIPT" scratchpad 2>/dev/null)
check "unresolvable prints nothing" "$OUT" ""
env -u CLAUDE_SCRATCHPAD -u CLAUDE_CODE_SESSION_ID "$SCRIPT" scratchpad >/dev/null 2>&1
check "unresolvable exits nonzero"  "$([ $? -ne 0 ] && echo yes || echo no)" "yes"

echo "== scratchpad: replaces every non-alphanumeric, not only slashes =="
setup_repo
mkdir -p "$R/my.repo_x y"
OUT=$(cd "$R/my.repo_x y" && env -u CLAUDE_SCRATCHPAD CLAUDE_CODE_SESSION_ID=sess-lit "$SCRIPT" scratchpad)
check "dot, underscore and space become '-'" \
  "$(case "$OUT" in *-my-repo-x-y/sess-lit/scratchpad) echo yes ;; *) echo no ;; esac)" "yes"
check "no dot survives in the cwd component" \
  "$(printf '%s' "${OUT%/sess-lit/scratchpad}" | sed 's#^.*/##' | grep -c '[.]')" "0"

echo "== scratchpad: a cwd it cannot name exactly is unresolvable, not guessed =="
setup_repo
mkdir -p "$R/caf$(printf '\303\251')"
OUT=$(cd "$R/caf$(printf '\303\251')" && env -u CLAUDE_SCRATCHPAD CLAUDE_CODE_SESSION_ID=sess-u "$SCRIPT" scratchpad 2>/dev/null)
RC=$?
check "non-ASCII cwd prints nothing" "$OUT" ""
check "non-ASCII cwd exits nonzero"  "$([ "$RC" -ne 0 ] && echo yes || echo no)" "yes"
LONG="$R/$(printf 'a%.0s' $(seq 1 210))"
mkdir -p "$LONG"
OUT=$(cd "$LONG" && env -u CLAUDE_SCRATCHPAD CLAUDE_CODE_SESSION_ID=sess-l "$SCRIPT" scratchpad 2>/dev/null)
RC=$?
check "over-long cwd prints nothing" "$OUT" ""
check "over-long cwd exits nonzero"  "$([ "$RC" -ne 0 ] && echo yes || echo no)" "yes"
# An explicit value still wins where derivation cannot.
OUT=$(cd "$LONG" && CLAUDE_SCRATCHPAD=/some/where/scratchpad CLAUDE_CODE_SESSION_ID=sess-l "$SCRIPT" scratchpad)
check "explicit value still honoured there" "$OUT" "$(native /some/where/scratchpad)"

echo "== reap: keeps an aged worktree inside the DERIVED scratchpad =="
setup_repo
SP="/tmp/claude-$(id -u)/$(harness_name "$(pwd -P)")/sess-derive/scratchpad"
mkdir -p "$SP"
git worktree add -q --detach "$SP/wt-pr88" "$SHA"
age_dir "$SP/wt-pr88" 999
OUT=$(env -u CLAUDE_SCRATCHPAD CLAUDE_CODE_SESSION_ID=sess-derive "$SCRIPT" reap 2>&1)
check "reports current-session"  "$(echo "$OUT" | grep -cxF "kept $(registered_parent wt-pr88)/wt-pr88 current-session")" "1"
check "still registered"         "$(git worktree list | grep -c wt-pr88)" "1"
check "directory survives"       "$([ -d "$SP/wt-pr88" ] && echo yes || echo no)" "yes"
# Same worktree, no session id to derive from: the exclusion cannot resolve and
# must NOT degrade into "keep everything" - the age floor takes over and reaps.
OUT=$(env -u CLAUDE_SCRATCHPAD -u CLAUDE_CODE_SESSION_ID "$SCRIPT" reap 2>&1)
check "unresolvable does not keep" "$(git worktree list | grep -c wt-pr88)" "0"
rm -rf "/tmp/claude-$(id -u)/$(harness_name "$(pwd -P)")"

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
WANT=$(native "/tmp/claude-$(id -u)/$(harness_name "$(cd "$R/real" && pwd -P)")/sess-sym/scratchpad")
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
if on_windows; then
  # A refuter that ran an install leaves junctions behind, and reap's own
  # `git worktree remove --force` would delete what they point to.
  OUTSIDE=$(mktemp -d); echo keep > "$OUTSIDE/file"
  junction "$SP/wt-pr5-rc3/j" "$OUTSIDE"
fi
age_dir "$SP/wt-pr5-rc3" 30; age_dir "$SP/wt-pr5" 30
OUT=$(CLAUDE_SCRATCHPAD=/elsewhere/scratchpad "$SCRIPT" reap 2>&1)
check "dirty refuter wt reaped"   "$(git worktree list | grep -c wt-pr5-rc3)" "0"
check "dirty main wt kept"        "$(echo "$OUT" | grep -c "^kept $(registered_parent wt-pr5)/wt-pr5 dirty-")" "1"
if on_windows; then
  check "a junction's target outside the reaped worktree survives" "$(cat "$OUTSIDE/file" 2>/dev/null)" "keep"
fi

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

echo "== remove-refuters: reports only what it removed =="
setup_repo
SP=$(mktemp -d)/scratchpad; mkdir -p "$SP"
"$SCRIPT" create "$SP/wt-pr5-rc1" HEAD >/dev/null
mkdir "$SP/wt-pr5-rc2"; echo keep > "$SP/wt-pr5-rc2/file"
OUT=$("$SCRIPT" remove-refuters "$SP" 5 2>/dev/null)
check "reports the worktree it removed" "$(echo "$OUT" | grep -cxF "removed $SP/wt-pr5-rc1")" "1"
check "does not report what it refused" "$(echo "$OUT" | grep -c 'wt-pr5-rc2')" "0"
check "the refused directory survives"  "$(cat "$SP/wt-pr5-rc2/file" 2>/dev/null)" "keep"

echo "== remove and reap: a link the sweep cannot unlink stops the removal =="
setup_repo
SP=$(mktemp -d)/scratchpad; mkdir -p "$SP"
# A refuter's worktree, whose dirt reap disregards, so only the link decides.
"$SCRIPT" create "$SP/wt-pr9-rc1" HEAD >/dev/null
OUTSIDE=$(mktemp -d); echo keep > "$OUTSIDE/file"
if on_windows; then junction "$SP/wt-pr9-rc1/j" "$OUTSIDE"; else ln_s "$OUTSIDE" "$SP/wt-pr9-rc1/j"; fi
# An `rm` that refuses any path matching $RM_FAIL, as a locked reparse point
# or an antivirus handle can.
RMSHIM=$(mktemp -d); REAL_RM=$(command -v rm)
cat > "$RMSHIM/rm" <<EOF
#!/usr/bin/env bash
for a in "\$@"; do
  case "\$a" in \$RM_FAIL) echo "rm: simulated failure" >&2; exit 1 ;; esac
done
exec "$REAL_RM" "\$@"
EOF
chmod +x "$RMSHIM/rm"
ERR=$(RM_FAIL='*/wt-pr9-rc1/j' PATH="$RMSHIM:$WINSIM" "$SCRIPT" remove "$SP/wt-pr9-rc1" 2>&1 >/dev/null); RC=$?
check "remove exits nonzero"            "$([ "$RC" -ne 0 ] && echo yes || echo no)" "yes"
check "remove reports links-remain"     "$(printf '%s\n' "$ERR" | grep -c '^error .*wt-pr9-rc1 links-remain$')" "1"
check "remove keeps the worktree"       "$(git worktree list | grep -c wt-pr9-rc1)" "1"
check "remove leaves the link's target" "$(cat "$OUTSIDE/file" 2>/dev/null)" "keep"
git worktree unlock "$SP/wt-pr9-rc1" 2>/dev/null; age_dir "$SP/wt-pr9-rc1" 48
OUT=$(RM_FAIL='*/wt-pr9-rc1/j' PATH="$RMSHIM:$WINSIM" CLAUDE_SCRATCHPAD=/elsewhere/scratchpad "$SCRIPT" reap 2>&1)
check "reap reports links-remain"       "$(echo "$OUT" | grep -c '^error .*wt-pr9-rc1 links-remain$')" "1"
check "reap keeps the worktree"         "$(git worktree list | grep -c wt-pr9-rc1)" "1"
check "reap leaves the link's target"   "$(cat "$OUTSIDE/file" 2>/dev/null)" "keep"

echo "== reap: a failed removal puts back the tracked links the sweep unlinked =="
setup_repo
git config core.symlinks true
ln_s f.txt tracked-link; git add tracked-link; git commit -qm link
SP=$(mktemp -d)/scratchpad; mkdir -p "$SP"
"$SCRIPT" create "$SP/wt-pr10" HEAD >/dev/null
git worktree unlock "$SP/wt-pr10"; age_dir "$SP/wt-pr10" 48
# A `git` whose `worktree remove` fails, as it does on a file another process
# holds open.
GITSHIM=$(mktemp -d); REAL_GIT=$(command -v git)
cat > "$GITSHIM/git" <<EOF
#!/usr/bin/env bash
[ "\${1:-} \${2:-}" = "worktree remove" ] && exit 1
exec "$REAL_GIT" "\$@"
EOF
chmod +x "$GITSHIM/git"
OUT=$(PATH="$GITSHIM:$WINSIM" CLAUDE_SCRATCHPAD=/elsewhere/scratchpad "$SCRIPT" reap 2>&1)
check "reports remove-failed"       "$(echo "$OUT" | grep -c '^error .*wt-pr10 remove-failed$')" "1"
check "the tracked link is back"    "$([ -L "$SP/wt-pr10/tracked-link" ] && echo yes || echo no)" "yes"
check "the worktree is clean again" "$(git -C "$SP/wt-pr10" status --porcelain | wc -l | tr -d ' ')" "0"

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
if on_windows; then
  # pnpm on Windows installs every package as a junction into its store.
  mkdir -p node_modules/.pnpm/pj@1.0.0/node_modules/pj
  echo store > node_modules/.pnpm/pj@1.0.0/node_modules/pj/index.js
  junction "$R/node_modules/pj" "$R/node_modules/.pnpm/pj@1.0.0/node_modules/pj"
fi
BASE=$(git rev-parse HEAD)
WT=$(mktemp -d)/wt; "$SCRIPT" create "$WT" HEAD >/dev/null
OUT=$("$SCRIPT" link-deps "$WT" "$BASE" 2>&1)
check "reports linked"                        "$(echo "$OUT" | grep -c '^linked ')" "1"
check "root workspace link reads the head"    "$(cat "$WT/node_modules/a/index.js" 2>/dev/null)" "head"
check "scoped workspace link reads the head"  "$(cat "$WT/node_modules/@s/a/index.js" 2>/dev/null)" "head"
check "package workspace link reads the head" "$(cat "$WT/libs/b/node_modules/a/index.js" 2>/dev/null)" "head"
# Written after linking, so only a live link can reach it: a copy of the
# worktree's package reads the head just as well at link time.
echo late > "$WT/libs/a/late.js"
check "workspace link tracks the worktree"    "$(cat "$WT/node_modules/a/late.js" 2>/dev/null)" "late"
if on_windows; then
  check "a Windows program reads through a workspace link" "$(wintype "$WT/node_modules" a/index.js)" "head"
  check "a Windows program reads a package pnpm installed as a junction" "$(wintype "$WT/node_modules" pj/index.js)" "store"
fi
check "third-party dep still reachable"       "$(cat "$WT/node_modules/dep/index.js" 2>/dev/null)" "x"
check "third-party dep is the primary's"      "$(readlink -f "$WT/node_modules/dep")" "$(readlink -f "$R/node_modules/dep")"
mkdir -p "$WT/node_modules/.vite"; echo cache > "$WT/node_modules/.vite/c"
check "a cache written in the worktree stays out of the primary" "$([ -e "$R/node_modules/.vite/c" ] && echo leaked || echo kept)" "kept"

echo "== scratchpad: rejects a bare drive on Windows only =="
setup_repo
for BAD in 'C:\' 'C:/' '/c'; do
  OUT=$(PATH="$WINSIM" env -u CLAUDE_CODE_SESSION_ID CLAUDE_SCRATCHPAD="$BAD" "$SCRIPT" scratchpad 2>/dev/null); RC=$?
  check "rejects drive root '$BAD'" "$OUT rc=$([ "$RC" -ne 0 ] && echo nonzero || echo 0)" " rc=nonzero"
done
OUT=$(PATH="$WINSIM" env -u CLAUDE_CODE_SESSION_ID CLAUDE_SCRATCHPAD='C:\x' "$SCRIPT" scratchpad 2>/dev/null)
check "accepts a directory below it" "$OUT" "C:/x"
if ! on_windows; then
  OUT=$(env -u CLAUDE_CODE_SESSION_ID CLAUDE_SCRATCHPAD=/c "$SCRIPT" scratchpad 2>/dev/null)
  check "off Windows /c is an ordinary directory" "$OUT" "/c"
fi

echo "== reap: resolves the scratchpad once per run =="
setup_repo
for n in 1 2 3; do git worktree add -q --detach ".claude/worktrees/review-90$n" "$SHA"; done
# A cygpath that logs each call, in front of whichever one WINSIM supplies.
CYGREC=$(mktemp -d); CYGLOG="$CYGREC/calls"; REAL_CYGPATH=$(PATH="$WINSIM" command -v cygpath)
cat > "$CYGREC/cygpath" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "$CYGLOG"
exec "$REAL_CYGPATH" "\$@"
EOF
chmod +x "$CYGREC/cygpath"
OUT=$(PATH="$CYGREC:$WINSIM" CLAUDE_SCRATCHPAD="$R/scratchpad" "$SCRIPT" reap 2>&1)
check "three review worktrees considered" "$(echo "$OUT" | grep -c '^kept .*review-90[123] ')" "3"
check "one cygpath call for all of them"  "$(wc -l < "$CYGLOG" | tr -d ' ')" "1"

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
check "resolves to the same directory"  "$OUT" "$(registered_parent wt-pr92)"
OUT=$(env -u CLAUDE_CODE_SESSION_ID CLAUDE_SCRATCHPAD="$(native "$SP")" "$SCRIPT" reap 2>&1)
check "protects the session's worktree" "$(git worktree list | grep -c wt-pr92)" "1"
if on_windows; then
  # Windows itself spells the same directory with backslashes.
  OUT=$(env -u CLAUDE_CODE_SESSION_ID CLAUDE_SCRATCHPAD="$(winpath "$SP")" "$SCRIPT" reap 2>&1)
  check "protects it given backslashes"  "$(git worktree list | grep -c wt-pr92)" "1"
fi

echo "== link-deps: a failed link leaves no half-built node_modules behind =="
setup_repo
mkdir -p node_modules/a-dep node_modules/fail-me
echo x > node_modules/a-dep/index.js; echo x > node_modules/fail-me/index.js
BASE=$(git rev-parse HEAD)
WT=$(mktemp -d)/wt; "$SCRIPT" create "$WT" HEAD >/dev/null
# An `ln` that, for any destination matching $LN_FAIL, fails whenever it is
# asked for a native symlink - as it does for a Windows account without the
# symlink privilege, where MSYS's default copy still succeeds.
SHIM=$(mktemp -d); REAL_LN=$(command -v ln)
cat > "$SHIM/ln" <<EOF
#!/usr/bin/env bash
case "\${MSYS:-}" in *nativestrict*)
  case "\${@: -1}" in \$LN_FAIL) echo "ln: simulated failure" >&2; exit 1 ;; esac ;;
esac
exec "$REAL_LN" "\$@"
EOF
chmod +x "$SHIM/ln"
LN_FAIL='*/fail-me' PATH="$SHIM:$PATH" "$SCRIPT" link-deps "$WT" "$BASE" >/dev/null 2>&1
OUT=$(LN_FAIL='*/fail-me' PATH="$SHIM:$PATH" "$SCRIPT" link-deps "$WT" "$BASE" 2>/dev/null)
check "a retry is not told it is already present" "$OUT" ""
check "the primary's entries survive the cleanup" "$(cat "$R/node_modules/a-dep/index.js" 2>/dev/null)" "x"

echo "== link-deps: a failed package-level link fails the whole run =="
setup_repo
mkdir -p node_modules/a-dep libs/b/node_modules/ok libs/b/node_modules/fail-me
echo x > node_modules/a-dep/index.js; echo x > libs/b/node_modules/ok/index.js
echo node_modules > .gitignore
echo '{}' > libs/b/package.json; git add .gitignore libs/b/package.json; git commit -qm pkg
BASE=$(git rev-parse HEAD)
WT=$(mktemp -d)/wt; "$SCRIPT" create "$WT" HEAD >/dev/null
ERRF=$(mktemp -d)/err
OUT=$(LN_FAIL='*/libs/b/node_modules/fail-me' PATH="$SHIM:$PATH" "$SCRIPT" link-deps "$WT" "$BASE" 2>"$ERRF"); RC=$?
check "exits nonzero"                 "$([ "$RC" -ne 0 ] && echo yes || echo no)" "yes"
check "prints no linked line"         "$(echo "$OUT" | grep -c '^linked ')" "0"
check "names the package in an error" "$(grep -c '^error: could not link node_modules into .*libs/b/node_modules$' "$ERRF")" "1"
check "leaves the package no half-built node_modules" "$([ -e "$WT/libs/b/node_modules" ] && echo yes || echo no)" "no"
check "leaves no root node_modules to read as already present" "$([ -e "$WT/node_modules" ] && echo yes || echo no)" "no"
check "the primary's entries survive" "$(cat "$R/node_modules/a-dep/index.js" "$R/libs/b/node_modules/ok/index.js" 2>/dev/null | tr -d '\n')" "xx"

echo "== link-deps: every link opts in to native symlinks under MSYS and Cygwin alike =="
setup_repo
mkdir -p node_modules/dep; echo x > node_modules/dep/index.js
BASE=$(git rev-parse HEAD)
WT=$(mktemp -d)/wt; "$SCRIPT" create "$WT" HEAD >/dev/null
# An `ln` that logs each call's two opt-in variables and its destination.
LNREC=$(mktemp -d); LNLOG="$LNREC/calls"
cat > "$LNREC/ln" <<EOF
#!/usr/bin/env bash
echo "MSYS=\${MSYS-unset} CYGWIN=\${CYGWIN-unset} \${@: -1}" >> "$LNLOG"
exec "$REAL_LN" "\$@"
EOF
chmod +x "$LNREC/ln"
OUT=$(PATH="$LNREC:$WINSIM" "$SCRIPT" link-deps "$WT" "$BASE" 2>&1)
check "reports linked"                  "$(echo "$OUT" | grep -c '^linked ')" "1"
check "the probe and the link both ran" "$(wc -l < "$LNLOG" | tr -d ' ')" "2"
check "every ln sets MSYS"              "$(grep -vc '^MSYS=winsymlinks:nativestrict ' "$LNLOG")" "0"
check "every ln sets CYGWIN"            "$(grep -vc ' CYGWIN=winsymlinks:nativestrict ' "$LNLOG")" "0"
# A worktree on a volume without symlink support must decline, which only a
# probe on that same volume can tell.
PROBE_DST=$(head -1 "$LNLOG"); PROBE_DST=${PROBE_DST##* }
check "the probe runs beside the worktree" \
  "$(case "$PROBE_DST" in "$(dirname "$WT")"/.symlink-probe.*/l) echo yes ;; *) echo "no: $PROBE_DST" ;; esac)" "yes"
check "the probe leaves nothing behind" "$(ls -A "$(dirname "$WT")")" "wt"

if on_windows; then
  echo "== link-deps: declines when the account cannot create symlinks =="
  WT=$(mktemp -d)/wt; "$SCRIPT" create "$WT" HEAD >/dev/null
  OUT=$(LN_FAIL='*' PATH="$SHIM:$PATH" "$SCRIPT" link-deps "$WT" "$BASE" 2>/dev/null); RC=$?
  check "declines, and exits 0" "$OUT rc=$RC" "declined symlinks-unavailable rc=0"
fi

echo; echo "passed=$PASS failed=$FAIL"; [ "$FAIL" -eq 0 ]
