#!/usr/bin/env bash
# Test harness for pr-review-wt.sh. Creates throwaway git repos under $TMPDIR.
# Expected: passed=27 failed=0
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
ln -s "$R/scratchpad/wt-real" "$R/scratchpad/wt-link"
"$SCRIPT" remove "$R/scratchpad/wt-link"
check "worktree deregistered" "$(git worktree list | grep -c wt-real)" "0"
check "real dir gone" "$([ -d "$R/scratchpad/wt-real" ] && echo yes || echo no)" "no"

echo; echo "passed=$PASS failed=$FAIL"; [ "$FAIL" -eq 0 ]
