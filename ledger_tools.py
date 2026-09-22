"""Deterministic ledger steps for the PR review pipeline. Standard library only.

pr-review-anchor.py is the only CLI the command prompts call; it imports this
module and re-exports the shared primitives so both files agree on them."""
import hashlib, json, os, re, shutil, tempfile

SCHEMA = 2
SEVERITIES = ("blocker", "high", "medium", "low")
SEV_RANK = {s: n for n, s in enumerate(SEVERITIES)}


class LedgerError(ValueError):
    """A deterministic ledger step cannot proceed. The message is user-facing
    and is printed as one `error: ...` line per line of the message."""


def fix_slot(detail):
    """The text of a finding's `**Fix:**` slot, or "" when `detail` has none.

    The slot is always last by contract, so everything after the label is the
    fix. Returns "" rather than raising on a `detail` shaped some other way:
    this is a lint over the fix text, not an enforcement of the slot's
    presence, and a ledger written before the slots existed still validates.
    """
    if not detail:
        return ""
    marker = re.search(r"\*\*Fix:\*\*", detail)
    if not marker:
        return ""
    return detail[marker.end():].strip()


def parse_changed_lines(diff_text):
    """Map file path -> set of new-side line numbers added or modified."""
    changed, path, newline = {}, None, None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            raw = line[4:].strip()
            path = None if raw == "/dev/null" else (raw[2:] if raw.startswith("b/") else raw)
            if path:
                changed.setdefault(path, set())
            newline = None
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            newline = int(m.group(1)) if m else None
        elif path and newline is not None:
            if line.startswith("+"):
                changed[path].add(newline); newline += 1
            elif line.startswith(" "):
                newline += 1
    return changed


def load_json(path, what="ledger"):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise LedgerError(f"{what} file not found: {path}")
    except OSError as exc:
        raise LedgerError(f"could not read {what} file {path}: {exc}")
    except json.JSONDecodeError as exc:
        raise LedgerError(f"{what} file {path} is not valid JSON: {exc}")
    if not isinstance(data, dict):
        raise LedgerError(f"{what} root must be a JSON object, got {type(data).__name__}")
    return data


def write_ledger(path, ledger, step):
    """Write atomically (temp file in the same directory, then rename), keeping
    the previous content as `<path>.bak-<step>` so a bad step can be inspected
    and a failed one never leaves a half-written ledger."""
    d = os.path.dirname(os.path.abspath(path))
    if os.path.exists(path):
        shutil.copyfile(path, f"{path}.bak-{step}")
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".ledger-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(ledger, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def read_lines(worktree, file, start, end):
    """Lines start..end (1-based, inclusive) of `file` in `worktree`, without
    line terminators."""
    if end < start:
        raise LedgerError(f"{file}: line_end {end} is before line {start}")
    path = os.path.join(worktree, file)
    if not os.path.isfile(path):
        raise LedgerError(f"{file}: not found in {worktree}")
    with open(path, encoding="utf-8", errors="replace") as fh:
        content = fh.read().splitlines()
    if end > len(content):
        raise LedgerError(f"{file}: line {end} is beyond end of file ({len(content)} lines)")
    return content[start - 1:end]
