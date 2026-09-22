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


# --- Refuter brief -----------------------------------------------------

TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "refuter-brief.tmpl.md")
VERDICT_FIELDS = ("ITEM", "VERDICT", "SEVERITY", "ON_CHANGED_LINE", "REASON", "CLAIM_VERDICT",
                  "CLAIM_REASON", "CLAIM_REWRITE", "FIX_VERDICT", "FIX_REASON", "FIX_REWRITE")
HEADER_FIELDS = ("BRIEF", "KEY", "OWNS")
_LABEL = re.compile(r"^(%s):[ \t]?(.*)$" % "|".join(HEADER_FIELDS + VERDICT_FIELDS))
# Fields a refuter sees. Verdict fields are deliberately absent: a refuter must
# not be primed by an earlier answer.
BRIEF_ITEM_FIELDS = ("id", "severity_in", "cluster", "file", "line", "line_end", "claim",
                     "detail", "evidence", "refuter_note")
CONTEXT_KEYS = ("link_deps", "commands", "obligations", "claude_md_paths", "scratch")
_REPO_FILE = re.compile(r"[\w./-]+\.(?:md|mdc|txt|json|ya?ml|toml)\b|CLAUDE\.md|AGENTS\.md")


def template_hash(path=TEMPLATE_PATH):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()[:12]


def refuter_units(ledger):
    units = {c["id"]: sorted(c.get("item_ids", [])) for c in ledger.get("clusters", [])}
    for it in ledger.get("items", []):
        if not it.get("cluster"):
            units[f"i{it['id']}"] = [it["id"]]
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
        "BRIEF_HASH": h, "KEY": key, "OWNS": ", ".join(str(i) for i in owns),
        "WT": ledger.get("worktree", ""), "HEAD_SHA": ledger.get("head_sha", ""),
        "MERGE_BASE": ledger.get("merge_base", ""),
        "LINK_DEPS": context["link_deps"], "COMMANDS": context["commands"],
        "OBLIGATIONS": context["obligations"],
        "CLAUDE_MD_PATHS": ", ".join(f"`{p}`" for p in context["claude_md_paths"]),
        "ITEMS": "```json\n" + json.dumps(payload, indent=2) + "\n```",
        "VERDICT_PATH": os.path.join(ledger_dir, "verdicts", f"{key}.txt"),
        "PROBE_DIR": f"{scratch}/probes/{key}",
        "REFUTER_WT": f"{scratch}/wt-pr{n}-r{key.lower()}",
    }
    with open(template_path, encoding="utf-8") as fh:
        text = fh.read()
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", str(v))
    left = sorted(set(re.findall(r"\{\{[A-Z_]+\}\}", text)))
    if left:
        raise LedgerError(f"template placeholders left unfilled: {', '.join(left)}")
    return text, {"key": key, "owns": owns, "hash": h}


def parse_verdict_file(text):
    header, blocks, cur, field = {}, [], None, None
    for raw in text.splitlines():
        m = _LABEL.match(raw)
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


def ingest(ledger, briefs_dir, verdicts_dir, expected_hash, inconclusive=(), reason=""):
    """Merge refuter verdict files into `ledger` in place and apply the
    mechanical claim/fix rewrites. Raises `LedgerError` listing every problem
    found across all briefs/verdicts at once, and only mutates `ledger` once
    no problems remain, so a failed call leaves it untouched."""
    by_id = {it["id"]: it for it in ledger.get("items", [])}
    inconclusive = {int(i) for i in inconclusive}
    if inconclusive and not reason.strip():
        raise LedgerError("--inconclusive needs --reason")
    metas = []
    if os.path.isdir(briefs_dir):
        for f in sorted(os.listdir(briefs_dir)):
            if f.endswith(".json"):
                metas.append(load_json(os.path.join(briefs_dir, f), what="brief meta"))
    if not metas:
        raise LedgerError(f"no briefs in {briefs_dir} - render them with `brief` before dispatch")
    errors, answers = [], {}
    dispatched = {i for m in metas for i in m["owns"]}
    for iid in sorted(set(by_id) - dispatched):
        errors.append(f"item {iid} was never dispatched")
    for m in metas:
        key, owns = m["key"], set(m["owns"])
        path = os.path.join(verdicts_dir, f"{key}.txt")
        if not os.path.isfile(path):
            if not owns <= inconclusive:
                errors.append(f"{key}: no verdict file at {path} (owns {sorted(owns)})")
            continue
        with open(path, encoding="utf-8") as fh:
            try:
                header, blocks = parse_verdict_file(fh.read())
            except LedgerError as exc:
                errors.append(f"{key}: {exc}"); continue
        if header.get("BRIEF") != expected_hash:
            errors.append(f"{key}: BRIEF {header.get('BRIEF')!r} does not match the current "
                          f"template ({expected_hash}) - render the brief with `brief`")
        if header.get("KEY") != key:
            errors.append(f"{key}: KEY header is {header.get('KEY')!r}")
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
            errs = _block_errors(b, by_id[iid])
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
