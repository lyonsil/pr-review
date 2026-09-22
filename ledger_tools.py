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


_CODE_SPAN = re.compile(r"`[^`\n]*`")
PROCESS_PATTERNS = (
    r"\brefuters?\b",
    r"\bpressure-test",
    r"\bNote for the\b",
    r"\bthis batch\b",
    r"\bitems?\s+#\d+",
    r"(?<![\w{])#\d+\b(?!\}\})",
)
_ABBREV = re.compile(r"\b(?:e\.g|i\.e|etc|vs|cf)\.", re.IGNORECASE)
_ELLIPSIS_LINE = re.compile(r"^\s*(?://\s*|#\s*|/\*\s*)?(?:\.\.\.|…)(?:\s*\*/)?\s*$")


def strip_code_spans(text):
    return _CODE_SPAN.sub("``", text or "")


def process_vocabulary_in(text):
    """Review-process wording that must never reach a PR author. `{{#n}}` tokens
    and code spans are exempt."""
    plain = strip_code_spans(text)
    found = []
    for pat in PROCESS_PATTERNS:
        m = re.search(pat, plain, re.IGNORECASE)
        if m:
            found.append(m.group(0))
    return found


def claim_shape_errors(claim):
    plain = _ABBREV.sub("abbr", strip_code_spans(claim))
    errs = []
    if len(plain.split()) > 40:
        errs.append(f"claim is {len(plain.split())} words; the limit is 40")
    if len(re.findall(r"[.!?]\s+(?=[A-Z])", plain)) > 0:
        errs.append("claim is more than one sentence")
    return errs


def is_ellipsis_line(line):
    return bool(_ELLIPSIS_LINE.match(line))


def excerpt_errors(ledger, worktree):
    """Each item's anchor_excerpt must equal lines line..line_end of its file,
    compared with trailing whitespace ignored (reanchor's own rule)."""
    errs = []
    for it in ledger.get("items", []):
        iid, line = it.get("id"), it.get("line")
        end = it.get("line_end") or line
        try:
            want = read_lines(worktree, it.get("file") or "", line, end)
        except (LedgerError, TypeError) as exc:
            errs.append(f"item {iid!r}: cannot read anchor lines - {exc}")
            continue
        got = (it.get("anchor_excerpt") or "").splitlines()
        if [l.rstrip() for l in got] != [l.rstrip() for l in want]:
            errs.append(f"item {iid!r}: anchor_excerpt is not verbatim at "
                        f"{it.get('file')}:{line}-{end} - run `excerpt`, never retype it")
    return errs
