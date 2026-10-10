"""Deterministic ledger steps for the PR review pipeline. Standard library only.

pr-review-anchor.py is the only CLI the command prompts call; it imports this
module and re-exports the shared primitives so both files agree on them."""
import hashlib, json, os, re, shutil, tempfile, textwrap

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


def why_it_matters_errors(detail):
    """Read-only. Every finding that can reach an author carries a non-empty
    `**Why it matters:**` slot. Unlike the Fix lint this enforces presence:
    a finding with no stated consequence reads as one too minor to raise, so
    a minor one says what it costs instead ("test-only, no behavior effect").
    """
    m = re.search(r"\*\*Why it matters:\*\*(.*?)(?=\*\*Fix:\*\*|\Z)", detail or "", re.DOTALL)
    if not m:
        return ["detail has no **Why it matters:** slot - state the consequence, however "
                "small; a missing slot reads as a finding not worth raising"]
    if not m.group(1).strip():
        return ["detail's **Why it matters:** slot is empty"]
    return []


def demotion_reason(it):
    """Read-only. Why `it` cannot be posted as an inline comment, judged
    purely by anchoring: "dead anchor" when `anchor_status` is "dead", "not
    on a changed line" when `anchor_status` is "off-diff" or
    `on_changed_line` is explicitly `False`, or `None` when it can be posted
    inline.

    Verdict and the REFUTED exclusion are the caller's job, not this
    function's - `plan_review` checks REFUTED before ever reaching this,
    and `render_report`'s inline/body count is scoped to non-REFUTED items
    by its caller. A missing/null `on_changed_line` is NOT a demotion, only
    an explicit `False` is: a ledger that has never been through S5 or
    reanchor has neither field set, and must stay postable rather than
    silently demoting every item.

    This is the one routing rule `plan_review`'s inline/demoted split and
    `render_report`'s inline/body count both have to agree on, so it lives
    here once instead of twice. It is GitHub's split: a backend that accepts
    comments off the diff posts "not on a changed line" findings inline
    instead (see `plan_review`'s `off_diff_inline`)."""
    if it.get("anchor_status") == "dead":
        return "dead anchor"
    if it.get("anchor_status") == "off-diff" or it.get("on_changed_line") is False:
        return "not on a changed line"
    return None


# Git C-quotes a path holding a byte it will not print raw - any non-ASCII
# byte under the default core.quotePath, a control character, a quote or a
# backslash - as "..." with octal escapes for the raw bytes and C escapes for
# the rest. The escapes are always ASCII, so the decode runs on bytes and only
# the result is read as UTF-8.
_GIT_QUOTED = r'"(?:[^"\\]|\\.)*"'
_C_ESCAPE = re.compile(rb'\\([0-3][0-7]{2}|[abtnvfr"\\])')
_C_CHARS = {b"a": b"\a", b"b": b"\b", b"t": b"\t", b"n": b"\n", b"v": b"\v", b"f": b"\f",
            b"r": b"\r", b'"': b'"', b"\\": b"\\"}


def git_path(raw):
    """A path as a git diff header prints it, unquoted: `"b/caf\\303\\251.ts"`
    reads as `b/café.ts`. Anything not wrapped in quotes is returned as is -
    git quotes every path that starts with a quote, so an unquoted one is
    already literal."""
    if len(raw) < 2 or raw[0] != '"' or raw[-1] != '"':
        return raw
    def unescape(m):
        esc = m.group(1)
        return bytes([int(esc, 8)]) if len(esc) == 3 else _C_CHARS[esc]
    return _C_ESCAPE.sub(unescape, raw[1:-1].encode("utf-8")).decode("utf-8", errors="replace")


def _strip_b(path):
    return path[2:] if path.startswith("b/") else path


def _diff_git_new_path(rest):
    """The new-side path named by a `diff --git <old> <new>` header, given the
    text after `diff --git `, or None when it cannot be read unambiguously.

    A quoted half is delimited by its quotes. Two unquoted halves are split
    only where they name the same path, which is every header but a rename's
    or a copy's - and those carry a `rename to`/`copy to` line, which wins."""
    m = re.fullmatch(rf"(?:{_GIT_QUOTED}|\S.*?) ({_GIT_QUOTED})", rest)
    if m:
        return _strip_b(git_path(m.group(1)))
    m = re.fullmatch(rf"{_GIT_QUOTED} (.+)", rest)
    if m:
        return _strip_b(m.group(1))
    half = (len(rest) - 5) // 2
    if half > 0 and rest == f"a/{rest[2:2 + half]} b/{rest[2:2 + half]}":
        return rest[2:2 + half]
    return None


def _parse_diff(diff_text):
    """(changed, files): `changed` maps each file with a new-side `+++`
    header to the new-side line numbers its hunks add, and `files` is every
    file the diff gives a new side.

    The two differ because a rename with no content change, a mode-only change
    and a binary change print no `+++` line at all: such a file is in the pull
    request but has no hunk to anchor to, so it is in `files` and not in
    `changed`. Each file's `diff --git` header names it - or its
    `rename to`/`copy to` line, for a path that moved. A deleted file has no
    new side and is in neither. Paths git quotes are decoded, so both match
    the plain path a ledger records.

    Each hunk header's counts say how many old-side and new-side lines its
    body holds, and a line inside a body is never a header: an added line
    whose text starts `++ ` reads `+++ ...`, and a removed one starting `-- `
    reads `--- ...`. So `+++ ` is a file header only once the current hunk's
    counts are spent. `diff --git` and `@@` need no such check, since no body
    line can start with either."""
    changed, files, path, newline = {}, set(), None, None
    # Old-side and new-side lines the current hunk's body has yet to show.
    old_left = new_left = 0
    # Between a `diff --git` line and its first hunk: the file that header
    # names, recorded when the header ends.
    in_header, header = False, None

    def flush():
        if in_header and header:
            files.add(header)

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            flush()
            old_left = new_left = 0
            in_header, header = True, _diff_git_new_path(line[len("diff --git "):])
        elif in_header and line.startswith(("rename to ", "copy to ")):
            header = git_path(line.split(" ", 2)[2])
        elif in_header and line.startswith("deleted file mode"):
            header = None
        elif line.startswith("+++ ") and not (old_left or new_left):
            raw = git_path(line[4:].strip())
            path = None if raw == "/dev/null" else _strip_b(raw)
            if path:
                changed.setdefault(path, set())
                files.add(path)
            newline = None
        elif line.startswith("@@"):
            flush()
            in_header = False
            m = re.search(r"\+(\d+)", line)
            newline = int(m.group(1)) if m else None
            counts = re.match(r"@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@", line)
            # An omitted count means one line.
            old_left, new_left = ((int(c) if c is not None else 1 for c in counts.groups())
                                  if counts else (0, 0))
        else:
            if line.startswith(("+", " ")):
                new_left = max(new_left - 1, 0)
            if line.startswith(("-", " ")):
                old_left = max(old_left - 1, 0)
            if path and newline is not None:
                if line.startswith("+"):
                    changed[path].add(newline); newline += 1
                elif line.startswith(" "):
                    newline += 1
    flush()
    return changed, files


def parse_changed_lines(diff_text):
    """Map file path -> set of new-side line numbers added or modified.

    Every file with a new-side `+++` header is a key, even one whose hunks
    add no line. A file the diff names without one - a pure rename, a mode
    change, a binary change - is not, since it has no hunk to anchor to;
    `diff_files` includes those."""
    return _parse_diff(diff_text)[0]


def diff_files(diff_text):
    """Every file the diff gives a new side, including one with no `+++`
    header (see `_parse_diff`): the files the pull request touches, which is
    what `file_in_diff` records."""
    return _parse_diff(diff_text)[1]


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
    and a failed one never leaves a half-written ledger. A failed write
    raises `LedgerError`."""
    d = os.path.dirname(os.path.abspath(path))
    try:
        if os.path.exists(path):
            shutil.copyfile(path, f"{path}.bak-{step}")
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".ledger-", suffix=".tmp")
    except OSError as exc:
        raise LedgerError(f"could not write ledger file {path}: {exc}")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(ledger, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException as exc:
        if os.path.exists(tmp):
            os.remove(tmp)
        if isinstance(exc, OSError):
            raise LedgerError(f"could not write ledger file {path}: {exc}")
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
)
# A bare `#123` outside a `{{#n}}` token. In `claim`/`detail` it can only be
# a stale finding number; in a refuter's reason it is usually a PR or issue.
BARE_REF_PATTERN = r"(?<![\w{])#\d+\b(?!\}\})"
_ABBREV = re.compile(r"\b(?:e\.g|i\.e|etc|vs|cf)\.", re.IGNORECASE)
_ELLIPSIS_LINE = re.compile(r"^\s*(?://\s*|#\s*|/\*\s*)?(?:\.\.\.|…)(?:\s*\*/)?\s*$")


def strip_code_spans(text):
    return _CODE_SPAN.sub("``", text or "")


def process_vocabulary_in(text, bare_refs=True):
    """Read-only. Review-process wording that must never reach a PR author.
    `{{#n}}` tokens and code spans are exempt; `bare_refs=False` also exempts
    a bare `#123`, for text that legitimately cites a PR or issue."""
    plain = strip_code_spans(text)
    found = []
    for pat in PROCESS_PATTERNS + ((BARE_REF_PATTERN,) if bare_refs else ()):
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


def _excerpt_for(worktree, it, start=None):
    start = start or it["line"]
    end = max(it.get("line_end") or it["line"], start)
    return "\n".join(read_lines(worktree, it["file"], start, end))


def fill_excerpts(ledger, worktree):
    """Mutating: call inside the `excerpt` subcommand, before `write_ledger`.

    Sets every item's `anchor_excerpt` to lines `line..line_end` read verbatim
    from `worktree`. All-or-nothing: every item is read first, and `ledger` is
    only mutated once none of them error, so a failed call leaves it
    untouched."""
    new, errors = {}, []
    for it in ledger.get("items", []):
        try:
            new[it["id"]] = _excerpt_for(worktree, it)
        except (LedgerError, KeyError, TypeError) as exc:
            errors.append(f"item {it.get('id')}: {exc}")
    if errors:
        raise LedgerError("\n".join(errors))
    for it in ledger["items"]:
        it["anchor_excerpt"] = new[it["id"]]


def first_changed_line(changed, start, end):
    """Read-only. The first line of `changed` inside [start, end], or None.

    The one rule for anchoring a range: it posts at its first changed line,
    and its end stays where it is - the moved start never drags the end
    along. `mark_changed_lines` and `reanchor` both call this, so the two
    commands always agree on where a range lands."""
    return min((n for n in changed if start <= n <= end), default=None)


def mark_changed_lines(ledger, worktree, changed, files=None):
    """Mutating: call inside the `changed-lines` subcommand, before
    `write_ledger`.

    Same rule reanchor applies: a finding is on the diff when its anchor line
    is a `+` line; a range whose first line is unchanged moves onto the first
    changed line inside [line, line_end]. `file_in_diff` records whether the
    item's file is in the diff at all - in `files` (see `diff_files`), or a key
    of `changed` when no `files` is given - since a backend that posts
    off-diff findings inline still has nowhere to put one in a file the pull
    request does not touch. Returns the notes to print - one per move and one per
    on_changed_line flip. An item missing `file` or `line`,
    or whose recut excerpt cannot be read, is named in a `LedgerError` instead
    of raising a bare `KeyError`/`TypeError` past this function."""
    notes, errors = [], []
    for it in ledger.get("items", []):
        try:
            lines = changed.get(it["file"], set())
            it["file_in_diff"] = it["file"] in (changed if files is None else files)
            start, end = it["line"], it.get("line_end") or it["line"]
            was = it.get("on_changed_line")
            hit = first_changed_line(lines, start, end)
            now = hit is not None
            if now and hit != start:
                it["line"] = hit
                it["anchor_excerpt"] = _excerpt_for(worktree, it)
                notes.append(f"moved #{it['id']} {start} -> {hit} "
                             "(first changed line in its range)")
            if was is not None and bool(was) != now:
                notes.append(f"flipped #{it['id']} on_changed_line {bool(was)} -> {now}")
            it["on_changed_line"] = now
        except (LedgerError, KeyError, TypeError) as exc:
            errors.append(f"item {it.get('id')}: {exc}")
    if errors:
        raise LedgerError("\n".join(errors))
    return notes


TOKEN = re.compile(r"\{\{#(\d+)\}\}")
# The fields that publish and may carry `{{#n}}` cross-references.
TOKEN_FIELDS = ("claim", "detail", "verdict_reason")
EDITABLE_FIELDS = ("claim", "detail", "fold_note", "verdict_reason", "fix_reason", "fix_verdict")
# fix_verdict may only be set to this value: a fix that no longer holds at a
# new head can be demoted to UNVERIFIABLE, but nothing short of a refuter may
# mark a fix SOUND or REWRITTEN.
FIX_VERDICT_DEMOTION = "UNVERIFIABLE"


def render_tokens(text):
    """Read-only. Render `{{#n}}` cross-reference tokens in `text` as plain
    `#n` references for publishing, and report every id referenced.

    Unlike `renumber`'s `sub`, this never checks the referenced ids against a
    ledger - the caller (`plan_review`) is the one that knows which ids are
    actually posted, and warns about any that are not."""
    refs = {int(m) for m in TOKEN.findall(text or "")}
    return TOKEN.sub(lambda m: "#" + m.group(1), text or ""), refs


def renumber(ledger):
    """Mutating: call inside the `renumber` subcommand, before `write_ledger`.

    Re-sorts `items` by (severity, file, id) - the same order `validate`
    requires - and remaps every id to its new 1..N position, rewriting
    `{{#n}}` cross-references in `TOKEN_FIELDS` and cluster `item_ids` to
    match. Each item's `old_id` records its id from before the *first* time
    it was renumbered, so re-running `renumber` after a further edit still
    traces back to the original id. All-or-nothing: every `{{#n}}` token is
    checked against the current ids first, and `ledger` is only mutated once
    none of them are unknown, so a failed call leaves it untouched."""
    items = ledger.get("items", [])
    noid = [f"item[{n}] has no integer id" for n, it in enumerate(items)
            if not isinstance(it.get("id"), int) or isinstance(it.get("id"), bool)]
    if noid:
        raise LedgerError("\n".join(noid))
    ids = {it["id"] for it in items}
    bad = [f"item {it['id']} references {{{{#{m}}}}}, which is not in this ledger"
           for it in items for f in TOKEN_FIELDS
           for m in TOKEN.findall(it.get(f) or "") if int(m) not in ids]
    if bad:
        raise LedgerError("\n".join(bad))
    order = sorted(items, key=lambda it: (SEV_RANK.get(it.get("severity"), 99),
                                          it.get("file") or "", it["id"]))
    mapping = {it["id"]: n for n, it in enumerate(order, start=1)}
    sub = lambda text: TOKEN.sub(lambda m: "{{#%d}}" % mapping[int(m.group(1))], text or "")
    for it in order:
        it.setdefault("old_id", it["id"])
        it["id"] = mapping[it["id"]]
        for f in TOKEN_FIELDS:
            if it.get(f):
                it[f] = sub(it[f])
    ledger["items"] = order
    for c in ledger.get("clusters", []):
        c["item_ids"] = sorted(mapping[i] for i in c.get("item_ids", []) if i in mapping)
    return mapping


def edit_field(ledger, iid, field, text):
    """Mutating: call inside the `edit` subcommand, before `write_ledger`.

    Replaces one editable text field of item `iid` verbatim (a single
    trailing newline is trimmed; nothing else is touched, so apostrophes,
    quotes and backticks survive byte-for-byte) - the safe alternative to an
    inline `python -c` edit, which has corrupted this text before.
    `fix_verdict` is the one non-text field here, and only as a demotion to
    `FIX_VERDICT_DEMOTION` - used when a fix no longer holds at a new head."""
    if field not in EDITABLE_FIELDS:
        raise LedgerError(f"field {field!r} is not editable; only {EDITABLE_FIELDS}")
    it = next((x for x in ledger.get("items", []) if x.get("id") == iid), None)
    if it is None:
        raise LedgerError(f"no item {iid} in this ledger")
    value = text.rstrip("\n")
    if field == "fix_verdict" and value != FIX_VERDICT_DEMOTION:
        raise LedgerError(f"fix_verdict can only be set to {FIX_VERDICT_DEMOTION}, got {value!r} - "
                          "a stale fix can be demoted, never promoted")
    it[field] = value


# --- Refuter brief -----------------------------------------------------

TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "refuter-brief.tmpl.md")
VERDICT_FIELDS = ("ITEM", "VERDICT", "SEVERITY", "ON_CHANGED_LINE", "REASON", "CLAIM_VERDICT",
                  "CLAIM_REASON", "CLAIM_REWRITE", "FIX_VERDICT", "FIX_REASON", "FIX_REWRITE")
HEADER_FIELDS = ("BRIEF", "RUN", "KEY", "OWNS")
_LABEL = re.compile(r"^(%s):[ \t]?(.*)$" % "|".join(HEADER_FIELDS + VERDICT_FIELDS))
# Fields a refuter sees. Verdict fields are deliberately absent: a refuter must
# not be primed by an earlier answer.
BRIEF_ITEM_FIELDS = ("id", "severity_in", "cluster", "file", "line", "line_end", "claim",
                     "detail", "evidence", "refuter_note")
CONTEXT_KEYS = ("link_deps", "commands", "obligations", "claude_md_paths", "scratch")
_PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")
_REPO_FILE = re.compile(r"[\w./-]+\.(?:md|mdc|txt|json|ya?ml|toml)\b|CLAUDE\.md|AGENTS\.md")


def template_hash(path=TEMPLATE_PATH):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()[:12]


def run_id(ledger):
    """Read-only. Identifies one draft: the head it was reviewed at and every
    item's id and claim, in ledger order. `brief` writes it into each brief
    and its meta, the refuter copies it into its reply, and `ingest` refuses
    a brief or reply whose run id is not this ledger's - so a brief or verdict
    left over from an earlier draft in the same session can never be merged
    into this one. Taken before `ingest`, the only step between `brief` and
    `ingest` that rewrites a claim."""
    basis = [ledger.get("head_sha") or "",
             [[it.get("id"), it.get("claim")] for it in ledger.get("items", [])]]
    return hashlib.sha256(json.dumps(basis, default=str).encode("utf-8")).hexdigest()[:12]


def _id_order(iid):
    """Sort key for item ids: integers numerically, anything malformed after
    them, so a bad id is reported rather than breaking the sort."""
    ok = isinstance(iid, int) and not isinstance(iid, bool)
    return (0, iid, "") if ok else (1, 0, str(iid))


def refuter_units(ledger):
    units = {c.get("id"): sorted(c.get("item_ids") or [], key=_id_order)
             for c in ledger.get("clusters", [])}
    for it in ledger.get("items", []):
        if not it.get("cluster"):
            units[f"i{it.get('id')}"] = [it.get("id")]
    return units


def _table_cells(row):
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _is_separator_row(row):
    """A `| --- | --- |` divider row: every cell is made up only of `-`, `:`
    and spaces, and the row is not blank (an empty joined string is
    vacuously a subset of that character set, so it must be excluded
    explicitly or a blank line would be misread as a divider)."""
    if not row.strip().startswith("|"):
        return False
    joined = "".join(_table_cells(row))
    return bool(joined) and set(joined) <= set("-: ")


def _check_obligations(table):
    """Every data row of the obligation table must cite a repo file in its
    last cell. The header row - identified as the row immediately BEFORE a
    `| --- |` separator row, per this table's actual shape, rather than by
    guessing at its wording - is exempt, and so is the separator itself."""
    rows = table.splitlines()
    for i, row in enumerate(rows):
        if not row.strip().startswith("|") or _is_separator_row(row):
            continue
        nxt = rows[i + 1] if i + 1 < len(rows) else ""
        if _is_separator_row(nxt):
            continue  # header row: the separator right after it says so
        cells = _table_cells(row)
        if not _REPO_FILE.search(cells[-1] if cells else ""):
            raise LedgerError(f"obligation row does not cite a repo file: {row.strip()} - "
                              "a personal preference is not a repo obligation")


def render_brief(ledger, key, context, ledger_dir, template_path=TEMPLATE_PATH):
    units = refuter_units(ledger)
    if key not in units:
        raise LedgerError(f"unknown refuter key {key!r}: expected one of {sorted(units)} "
                          "(one cluster, or one unclustered item as i<id>)")
    missing = [k for k in CONTEXT_KEYS if k not in context]
    if missing:
        raise LedgerError(f"context file is missing {', '.join(missing)}")
    _check_obligations(context["obligations"])
    paths = context["claude_md_paths"]
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise LedgerError(f"claude_md_paths must be a list of path strings, got {paths!r}")
    owns = units[key]
    by_id = {it["id"]: it for it in ledger.get("items", [])}
    payload = [{k: by_id[i][k] for k in BRIEF_ITEM_FIELDS if k in by_id[i]} for i in owns]
    cluster = next((c for c in ledger.get("clusters", []) if c["id"] == key), None)
    if cluster:
        payload = {"cluster": {k: cluster.get(k) for k in ("id", "title", "shared_fix")},
                   "items": payload}
    target = ledger.get("target", {})
    n = target.get("number", "local")
    scratch = context["scratch"].rstrip("/")
    h = template_hash(template_path)
    values = {
        "BRIEF_HASH": h, "RUN_ID": run_id(ledger), "KEY": key,
        "OWNS": ", ".join(str(i) for i in owns),
        "WT": ledger.get("worktree", ""), "HEAD_SHA": ledger.get("head_sha", ""),
        "MERGE_BASE": ledger.get("merge_base", ""),
        "LINK_DEPS": context["link_deps"], "COMMANDS": context["commands"],
        "OBLIGATIONS": context["obligations"],
        "CLAUDE_MD_PATHS": ", ".join(f"`{p}`" for p in paths),
        "ITEMS": "```json\n" + json.dumps(payload, indent=2) + "\n```",
        "VERDICT_PATH": os.path.join(ledger_dir, "verdicts", f"{key}.txt"),
        "PROBE_DIR": f"{scratch}/probes/{key}",
        "REFUTER_WT": f"{scratch}/wt-pr{n}-r{key.lower()}",
    }
    with open(template_path, encoding="utf-8") as fh:
        text = fh.read()
    # One pass over the template: a value is never re-scanned, so item text
    # that happens to contain `{{KEY}}` or `{{TITLE}}` is copied literally,
    # and "unfilled" means a placeholder in the template itself.
    left = sorted({m for m in _PLACEHOLDER.findall(text) if m not in values})
    if left:
        raise LedgerError("template placeholders left unfilled: "
                          + ", ".join("{{%s}}" % m for m in left))
    text = _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), text)
    return text, {"key": key, "owns": owns, "hash": h, "run": values["RUN_ID"]}


_FENCE = re.compile(r"^\s*```[\w-]*\s*$")


def _unfence(lines):
    """`lines` without one code fence wrapping the whole reply, which a
    refuter copying the fenced reply format from its brief may include. Only
    a fence that is both the first and the last non-blank line is removed, so
    a fence inside a multi-line value is kept."""
    body = [n for n, l in enumerate(lines) if l.strip()]
    if len(body) >= 2 and _FENCE.match(lines[body[0]]) and _FENCE.match(lines[body[-1]]):
        return lines[body[0] + 1:body[-1]]
    return lines


def parse_verdict_file(text):
    """Read-only. Split a refuter's reply into its header fields and one dict
    per `ITEM` block. Labels may be indented (the brief's reply format may be
    copied with its indentation): an indent common to every line is removed,
    and a label is recognized after any remaining leading whitespace. A
    continuation line otherwise keeps its own indentation."""
    header, blocks, cur, field = {}, [], None, None
    for raw in _unfence(textwrap.dedent(text).splitlines()):
        m = _LABEL.match(raw.lstrip())
        if m:
            name, val = m.group(1), m.group(2)
            if name in HEADER_FIELDS:
                if cur is not None:
                    raise LedgerError(f"{name}: header line after the first ITEM block")
                header[name] = val.strip(); field = None
                continue
            if name == "ITEM":
                cur = {"ITEM": val.strip()}; blocks.append(cur); field = None
                continue
            if cur is None:
                raise LedgerError(f"{name}: field before any ITEM line")
            if name in cur:
                raise LedgerError(f"item {cur['ITEM']}: {name} given twice")
            cur[name] = val; field = name
            continue
        if field is not None:
            cur[field] += "\n" + raw
        elif raw.strip():
            where = "header" if cur is None else f"item {cur['ITEM']}"
            raise LedgerError(f"{where}: unrecognized line {raw.strip()[:60]!r}")
    for b in blocks:
        for k in b:
            b[k] = b[k].strip()
    return header, blocks


# --- Ingest --------------------------------------------------------------

VERDICTS = ("CONFIRMED", "DOWNGRADED", "REFUTED", "INCONCLUSIVE")


def replace_fix_slot(detail, text):
    m = re.search(r"\*\*Fix:\*\*", detail or "")
    if not m:
        raise LedgerError("detail has no **Fix:** slot to rewrite")
    text = re.sub(r"^\s*\*\*Fix:\*\*\s*", "", text)
    return detail[:m.end()] + " " + text.strip()


def _block_errors(b, it):
    iid, v = b["ITEM"], b.get("VERDICT")
    e = []
    if v not in VERDICTS:
        e.append(f"item {iid}: VERDICT {v!r} not one of {VERDICTS}")
    if b.get("SEVERITY") not in SEVERITIES:
        e.append(f"item {iid}: SEVERITY {b.get('SEVERITY')!r} not one of {SEVERITIES}")
    if b.get("ON_CHANGED_LINE", "").lower() not in ("true", "false"):
        e.append(f"item {iid}: ON_CHANGED_LINE must be true or false")
    if not b.get("REASON"):
        e.append(f"item {iid}: REASON is empty")
    if v == "REFUTED":
        extra = [k for k in b if k.startswith(("CLAIM_", "FIX_"))]
        if extra:
            e.append(f"item {iid}: REFUTED must omit {', '.join(extra)}")
        return e
    for slot, allowed in (("CLAIM", ("SOUND", "REWRITTEN")),
                          ("FIX", ("SOUND", "REWRITTEN", "UNVERIFIABLE"))):
        sv = b.get(f"{slot}_VERDICT")
        if sv not in allowed:
            e.append(f"item {iid}: {slot}_VERDICT {sv!r} not one of {allowed}")
        if not b.get(f"{slot}_REASON"):
            e.append(f"item {iid}: {slot}_REASON is empty")
        if sv == "REWRITTEN" and not b.get(f"{slot}_REWRITE"):
            e.append(f"item {iid}: {slot}_VERDICT REWRITTEN without {slot}_REWRITE")
        if sv != "REWRITTEN" and b.get(f"{slot}_REWRITE"):
            e.append(f"item {iid}: {slot}_REWRITE given with {slot}_VERDICT {sv}")
    if b.get("FIX_VERDICT") == "REWRITTEN" and not re.search(r"\*\*Fix:\*\*", it.get("detail") or ""):
        e.append(f"item {iid}: detail has no **Fix:** slot to rewrite")
    if v == "DOWNGRADED" and b.get("SEVERITY") in SEV_RANK and \
            SEV_RANK[b["SEVERITY"]] <= SEV_RANK.get(it.get("severity_in"), 99):
        e.append(f"item {iid}: DOWNGRADED but SEVERITY {b['SEVERITY']} is not lower than "
                 f"severity_in {it.get('severity_in')}")
    return e


def _meta_errors(m, units, run):
    """Why brief meta `m` does not belong to this draft, or [] when it does:
    its key must be one of this ledger's refuter units, it must own exactly
    that unit's ids, and it must carry this draft's run id. A meta that fails
    here is from an earlier run (or hand-written) and is not read further."""
    key, owns = m.get("key"), m.get("owns")
    if key not in units:
        return [f"{key}: not a refuter unit of this ledger - a brief left over from an "
                "earlier run; clear briefs/ and render them again with `brief`"]
    if not isinstance(owns, list) or sorted(owns, key=_id_order) != units[key]:
        return [f"{key}: brief owns {owns!r} but this ledger's {key} is {units[key]} - "
                "render the brief again with `brief`"]
    if m.get("run") != run:
        return [f"{key}: brief was rendered for a different draft (run {m.get('run')!r}, "
                f"this ledger is {run}) - render the brief again with `brief`"]
    return []


def _read_verdict_file(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except UnicodeDecodeError as exc:
        raise LedgerError(f"verdict file {path} is not valid UTF-8: {exc}")
    except OSError as exc:
        raise LedgerError(f"could not read verdict file {path}: {exc}")


def ingest(ledger, briefs_dir, verdicts_dir, expected_hash, inconclusive=(), reason=""):
    """Mutating: call inside the `ingest` subcommand, before `write_ledger`.

    Merge refuter verdict files into `ledger` in place and apply the
    mechanical claim/fix rewrites. Raises `LedgerError` listing every problem
    found across all briefs/verdicts at once, and only mutates `ledger` once
    no problems remain, so a failed call leaves it untouched.

    Every brief meta must belong to this draft (see `_meta_errors`), and every
    verdict file's `RUN`/`OWNS` headers must match its meta, so nothing from
    an earlier run in the same session is merged. An id may be answered by
    one file only. Ids in `inconclusive` are closed INCONCLUSIVE unless a
    valid answer for them exists: a unit whose ids are all in `inconclusive`
    is not read at all, and a block error for an `inconclusive` id is dropped
    rather than failing the run - that is what closing a refuter that failed
    twice means."""
    by_id = {it.get("id"): it for it in ledger.get("items", [])}
    inconclusive = {int(i) for i in inconclusive}
    if inconclusive and not reason.strip():
        raise LedgerError("--inconclusive needs --reason")
    unknown = sorted(inconclusive - set(by_id))
    if unknown:
        raise LedgerError("\n".join(
            f"--inconclusive names item {iid}, which is not in this ledger" for iid in unknown))
    metas = []
    if os.path.isdir(briefs_dir):
        for f in sorted(os.listdir(briefs_dir)):
            if f.endswith(".json"):
                metas.append(load_json(os.path.join(briefs_dir, f), what="brief meta"))
    if not metas:
        raise LedgerError(f"no briefs in {briefs_dir} - render them with `brief` before dispatch")
    units, run = refuter_units(ledger), run_id(ledger)
    errors, answers, answered_by = [], {}, {}
    valid = []
    for m in metas:
        errs = _meta_errors(m, units, run)
        errors.extend(errs)
        if not errs:
            valid.append(m)
    dispatched = {i for m in metas if isinstance(m.get("owns"), list) for i in m["owns"]}
    for iid in sorted(set(by_id) - dispatched, key=_id_order):
        errors.append(f"item {iid} was never dispatched")
    for m in valid:
        key, owns = m["key"], set(m["owns"])
        if owns <= inconclusive:
            continue
        path = os.path.join(verdicts_dir, f"{key}.txt")
        if not os.path.isfile(path):
            errors.append(f"{key}: no verdict file at {path} (owns {sorted(owns)})")
            continue
        try:
            header, blocks = parse_verdict_file(_read_verdict_file(path))
        except LedgerError as exc:
            errors.append(f"{key}: {exc}"); continue
        if header.get("BRIEF") != expected_hash:
            errors.append(f"{key}: BRIEF {header.get('BRIEF')!r} does not match the current "
                          f"template ({expected_hash}) - render the brief with `brief`")
        if header.get("RUN") != run:
            errors.append(f"{key}: RUN header is {header.get('RUN')!r}, expected {run!r} - "
                          "a reply to a brief from another draft")
        if header.get("KEY") != key:
            errors.append(f"{key}: KEY header is {header.get('KEY')!r}")
        want_owns = ", ".join(str(i) for i in m["owns"])
        if header.get("OWNS") != want_owns:
            errors.append(f"{key}: OWNS header is {header.get('OWNS')!r}, expected {want_owns!r}")
        seen = set()
        for b in blocks:
            try:
                iid = int(b["ITEM"])
            except ValueError:
                errors.append(f"{key}: ITEM {b['ITEM']!r} is not an integer id"); continue
            if iid in seen:
                errors.append(f"{key}: item {iid} answered twice"); continue
            seen.add(iid)
            if iid not in owns:
                errors.append(f"{key}: does not own item {iid}"); continue
            if iid in answered_by:
                errors.append(f"item {iid} answered in both {answered_by[iid]} and {key}")
                continue
            answered_by[iid] = key
            errs = _block_errors(b, by_id[iid])
            if errs and iid in inconclusive:
                continue
            errors.extend(f"{key}: {x}" for x in errs)
            if not errs:
                answers[iid] = (key, b)
        for iid in sorted(owns - seen - inconclusive):
            errors.append(f"{key}: no answer for item {iid}")
    if errors:
        raise LedgerError("\n".join(errors))

    for iid, (key, b) in answers.items():
        it = by_id[iid]
        it.update(verdict=b["VERDICT"], severity=b["SEVERITY"], verdict_reason=b["REASON"],
                  on_changed_line=b["ON_CHANGED_LINE"].lower() == "true", refuter_key=key)
        if b["VERDICT"] != "REFUTED":
            it.update(claim_verdict=b["CLAIM_VERDICT"], claim_reason=b["CLAIM_REASON"],
                      fix_verdict=b["FIX_VERDICT"], fix_reason=b["FIX_REASON"])
            if b["CLAIM_VERDICT"] == "REWRITTEN":
                it["claim_original"], it["claim"] = it["claim"], b["CLAIM_REWRITE"]
            if b["FIX_VERDICT"] == "REWRITTEN":
                it["fix_original"] = fix_slot(it["detail"])
                it["detail"] = replace_fix_slot(it["detail"], b["FIX_REWRITE"])
    for iid in inconclusive - set(answers):
        it = by_id[iid]
        it.update(verdict="INCONCLUSIVE", verdict_reason=reason, unanswered=True,
                  fix_verdict="UNVERIFIABLE", fix_reason=reason, claim_reason=reason,
                  refuter_key="none")
    for it in by_id.values():
        it.pop("refuter_note", None)
    return [f"ingested {len(answers)} answer(s) from {len(metas)} refuter(s)"
            + (f"; {len(inconclusive - set(answers))} closed INCONCLUSIVE" if inconclusive else "")]


# --- Report ----------------------------------------------------------------


def _esc_cell(text):
    """A table cell's text with `|` escaped so a claim or reason containing one
    cannot be misread as a new column by a Markdown table renderer."""
    return str(text).replace("|", "\\|")


def render_report(ledger, ledger_path):
    """Read-only. Render the S6 terminal report and `report.md` content from
    `ledger` - plain text and GitHub-flavored markdown only, no raw HTML,
    since this is printed straight to a terminal as well as written to disk."""
    items = ledger.get("items", [])
    ldir = os.path.dirname(os.path.abspath(ledger_path))
    by_v = {v: [it for it in items if it.get("verdict") == v] for v in VERDICTS}
    sev = lambda it: (it.get("severity") if it.get("severity") == it.get("severity_in")
                      else f"{it.get('severity_in')} -> {it.get('severity')}")
    lowered = sum(SEV_RANK.get(it.get("severity"), 0) > SEV_RANK.get(it.get("severity_in"), 0)
                  for it in items)
    raised = sum(SEV_RANK.get(it.get("severity"), 0) < SEV_RANK.get(it.get("severity_in"), 0)
                 for it in items)
    out = ["## Triage", "", "| id | severity | verdict | cluster | location | claim |",
           "| --- | --- | --- | --- | --- | --- |"]
    for it in items:
        out.append(f"| {it.get('id')} | {sev(it)} | {it.get('verdict')} | {it.get('cluster') or ''} | "
                   f"`{it.get('file')}:{it.get('line')}` | {_esc_cell(it.get('claim'))} |")
    n = len(items)
    out += ["", "## Verdict summary", "",
            " / ".join(f"{v} {len(by_v[v])}" for v in VERDICTS)
            + f"; refuted {len(by_v['REFUTED'])}/{n}"]
    if n and not by_v["REFUTED"]:
        out.append("warning: nothing was refuted - treat the pressure-test as uncalibrated "
                   "until you have looked at why")
    if lowered and not raised:
        out.append(f"warning: {lowered} lowered / 0 raised - a one-sided revision is itself a "
                   "claim about the generator")
    rc = [it for it in items if it.get("claim_verdict") == "REWRITTEN"]
    out += ["", "## Claim audit", "",
            f"SOUND {sum(it.get('claim_verdict') == 'SOUND' for it in items)} / REWRITTEN {len(rc)}; "
            f"severity {lowered} lowered / {raised} raised"]
    out += [f"- #{it.get('id')}: {it.get('claim_original')} -> {it.get('claim')}" for it in rc]
    rf = [it for it in items if it.get("fix_verdict") == "REWRITTEN"]
    uf = [it for it in items if it.get("fix_verdict") == "UNVERIFIABLE"]
    out += ["", "## Fix audit", "",
            f"SOUND {sum(it.get('fix_verdict') == 'SOUND' for it in items)} / REWRITTEN {len(rf)} / "
            f"UNVERIFIABLE {len(uf)}"]
    out += [f"- #{it.get('id')}: {it.get('fix_original')} -> {fix_slot(it.get('detail'))}" for it in rf]
    out += [f"- #{it.get('id')} UNVERIFIABLE (needs a human before it posts): {it.get('fix_reason')}"
            for it in uf]
    out += ["", "## Downgraded", ""]
    out += [f"- #{it.get('id')} {sev(it)} - {it.get('claim')} - {it.get('verdict_reason')} "
            f"[{it.get('fold_note')}]" for it in by_v["DOWNGRADED"]] or ["none"]
    out += ["", "## Unresolved", ""]
    out += [f"- #{it.get('id')} - {it.get('claim')} - {it.get('verdict_reason')} (needs a human call)"
            for it in by_v["INCONCLUSIVE"]] or ["none"]
    out += ["", "## Refuted (not posted unless you name them)", ""]
    out += [f"- #{it.get('id')} - {it.get('claim')} - {it.get('verdict_reason')}"
            for it in by_v["REFUTED"]] or ["none"]
    dd = [it for it in items if it.get("dedupe")]
    out += ["", "## Previously reported", ""]
    out += [f"- #{it.get('id')} - " + (str(it["dedupe"].get("prior_url"))
                                       if isinstance(it["dedupe"], dict) else str(it["dedupe"]))
            for it in dd] or ["none"]
    live = [it for it in items if it.get("verdict") != "REFUTED"]
    inline = sum(demotion_reason(it) is None for it in live)
    body = len(live) - inline
    owners = {}
    for it in items:
        owners.setdefault(it.get("refuter_key"), []).append(it.get("id"))
    largest = max((len(v) for k, v in owners.items() if k not in (None, "none")), default=0)
    real = [k for k in owners if k not in (None, "none")]
    out += ["", "## Pressure-test dispatch", "",
            f"{len(real)} refuters for {n} items, largest owned {largest}",
            "", "## Files", "",
            f"    Ledger:   {os.path.abspath(ledger_path)}",
            f"    Report:   {os.path.join(ldir, 'report.md')}",
            f"    Worktree: {ledger.get('worktree')}",
            "", "## To post", "",
            f"    /pr-review-post all          # on GitHub: {inline} inline + {body} in the body; "
            f"{len(by_v['REFUTED'])} refuted excluded",
            "    /pr-review-post 1 3-7        # a chosen subset (ranges allowed)",
            "    /pr-review-post all --include-refuted <id>   # also post a refuted finding",
            "", "Placement follows anchoring, not verdict: every selected finding on a changed "
            "line posts inline whatever its verdict; the rest go in the review body."]
    return "\n".join(out) + "\n"
