#!/usr/bin/env python3
"""Ledger validation, head-drift re-anchoring, and GitHub review payload building
for the PR review pipeline. Standard library only."""
import argparse, json, os, re, sys

SEVERITIES = ("blocker", "high", "medium", "low")
ATTRIBUTION = "_(AI-assisted, with my guidance)_"


class SelectionError(ValueError):
    """A --select token names neither a real item id nor a real cluster id."""


class PayloadError(ValueError):
    """A selected item can't be safely anchored into a GitHub review comment."""


class IncludeRefutedError(ValueError):
    """An --include-refuted token doesn't name a REFUTED item in this ledger."""


def validate(ledger):
    if not isinstance(ledger, dict):
        return [f"ledger root must be a JSON object, got {type(ledger).__name__}"]
    errs = []
    items = ledger.get("items", [])
    clusters = ledger.get("clusters", [])

    for n, it in enumerate(items):
        iid = it.get("id")
        if not isinstance(iid, int) or isinstance(iid, bool):
            errs.append(f"item[{n}] id {iid!r} is not an integer "
                        "(letter suffixes like '2a' are forbidden)")
        if it.get("severity") not in SEVERITIES:
            errs.append(f"item {iid!r} severity {it.get('severity')!r} not one of {SEVERITIES}")
        for field in ("file", "claim", "anchor_excerpt"):
            if not it.get(field):
                errs.append(f"item {iid!r} is missing required field '{field}'")

    int_ids = [it["id"] for it in items if isinstance(it.get("id"), int) and not isinstance(it.get("id"), bool)]
    if len(set(int_ids)) != len(int_ids):
        errs.append("duplicate item ids")
    elif int_ids and int_ids != list(range(1, len(int_ids) + 1)):
        errs.append(f"ids are not contiguous 1..N in list order (got {int_ids[:6]})")

    rank = {s: n for n, s in enumerate(SEVERITIES)}
    keyed = [(rank.get(it.get("severity"), 99), it.get("file", "")) for it in items]
    if keyed != sorted(keyed):
        errs.append("items are not ordered by severity descending then file path")

    known_ids = set(int_ids)
    known_clusters = {c.get("id") for c in clusters}
    for c in clusters:
        for ref in c.get("item_ids", []):
            if ref not in known_ids:
                errs.append(f"cluster {c.get('id')} references unknown item {ref}")
    for it in items:
        cl = it.get("cluster")
        if cl and cl not in known_clusters:
            errs.append(f"item {it.get('id')} references unknown cluster {cl}")
    return errs


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def cmd_validate(args):
    try:
        led = load(args.ledger)
    except FileNotFoundError:
        print(f"error: ledger file not found: {args.ledger}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: could not read ledger file {args.ledger}: {exc}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"error: ledger file {args.ledger} is not valid JSON: {exc}", file=sys.stderr)
        return 1
    errs = validate(led)
    if errs:
        for e in errs:
            print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"ledger ok: {len(led.get('items', []))} items, {len(led.get('clusters', []))} clusters")
    return 0


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


def reanchor_item(item, worktree, changed):
    path = os.path.join(worktree, item["file"])
    if not os.path.isfile(path):
        return "dead", None, "file no longer present at head"
    with open(path, encoding="utf-8", errors="replace") as fh:
        content = fh.read().splitlines()
    excerpt = item.get("anchor_excerpt", "").splitlines()
    if not excerpt:
        return "dead", None, "empty anchor excerpt"
    # Trailing whitespace is normalized away (formatters trim it harmlessly).
    # Leading whitespace is left significant on purpose: ignoring it too would
    # make short/common lines (e.g. a lone "}") match far more places and raise
    # the odds of silently anchoring to the wrong duplicate, which is the one
    # failure mode this function must never produce. The conservative cost is
    # that a pure re-indent reports "dead" instead of "ok" - losing one inline
    # comment is cheap; a false "ok" is what sinks the whole review POST.
    norm = [l.rstrip() for l in excerpt]
    hits = [n + 1 for n in range(len(content) - len(excerpt) + 1)
            if [l.rstrip() for l in content[n:n + len(excerpt)]] == norm]
    if not hits:
        return "dead", None, "anchor excerpt no longer present"
    old = item.get("line") or hits[0]
    new_line = min(hits, key=lambda h: abs(h - old))
    note = ""
    if len(hits) > 1:
        others = [h for h in hits if h != new_line]
        note = f"ambiguous: excerpt also matches at line(s) {others}; nearest to {old} chosen"
    if new_line not in changed.get(item["file"], set()):
        dead_reason = "no longer on a changed line; cannot be posted inline"
        return "dead", new_line, f"{dead_reason} ({note})" if note else dead_reason
    if new_line == item.get("line"):
        return "ok", new_line, note
    moved_reason = f"moved {item.get('line')} -> {new_line}"
    return "moved", new_line, f"{moved_reason} ({note})" if note else moved_reason


def cmd_reanchor(args):
    try:
        led = load(args.ledger)
    except FileNotFoundError:
        print(f"error: ledger file not found: {args.ledger}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: could not read ledger file {args.ledger}: {exc}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"error: ledger file {args.ledger} is not valid JSON: {exc}", file=sys.stderr)
        return 1
    if not isinstance(led, dict):
        print(f"error: ledger root must be a JSON object, got {type(led).__name__}", file=sys.stderr)
        return 1

    try:
        with open(args.diff, encoding="utf-8", errors="replace") as fh:
            changed = parse_changed_lines(fh.read())
    except FileNotFoundError:
        print(f"error: diff file not found: {args.diff}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: could not read diff file {args.diff}: {exc}", file=sys.stderr)
        return 1

    counts = {"ok": 0, "moved": 0, "dead": 0}
    for it in led.get("items", []):
        status, new_line, why = reanchor_item(it, args.worktree, changed)
        it["anchor_status"], it["anchor_note"] = status, why
        if new_line and status != "dead":
            it["line"] = new_line
        counts[status] += 1
        print(f"{status:6} #{it.get('id')!s:<3} {it.get('file')}:{new_line or it.get('line')} {why}")

    try:
        with open(args.ledger, "w", encoding="utf-8") as fh:
            json.dump(led, fh, indent=2)
    except OSError as exc:
        print(f"error: could not write ledger file {args.ledger}: {exc}", file=sys.stderr)
        return 1

    print(f"\nok={counts['ok']} moved={counts['moved']} dead={counts['dead']}")
    return 0


def resolve_selection(ledger, tokens):
    """Expand --select tokens ('all', integer ids, cluster ids like 'C3') into a
    set of item ids. Every token must resolve to something real in THIS ledger:
    a typo'd id or an id that isn't in the ledger raises rather than silently
    posting a smaller set than the caller asked for."""
    items = ledger.get("items", [])
    known_ids = {it.get("id") for it in items
                 if isinstance(it.get("id"), int) and not isinstance(it.get("id"), bool)}
    if not tokens or any(str(t).strip().lower() == "all" for t in tokens):
        return set(known_ids)

    by_cluster = {c.get("id"): list(c.get("item_ids", [])) for c in ledger.get("clusters", [])}
    ids, unrecognized, unknown = set(), [], []
    for raw in tokens:
        for tok in str(raw).replace(",", " ").split():
            if tok.upper() in by_cluster:
                # Same known-id check as the digit branch below - a cluster
                # referencing an id absent from this ledger must fail the
                # selection, not silently expand to fewer items than the
                # cluster actually has.
                for cid in by_cluster[tok.upper()]:
                    if cid in known_ids:
                        ids.add(cid)
                    else:
                        unknown.append(str(cid))
            elif tok.isdigit():
                requested = int(tok)
                if requested in known_ids:
                    ids.add(requested)
                else:
                    unknown.append(tok)
            else:
                unrecognized.append(tok)

    if unrecognized or unknown:
        parts = []
        if unrecognized:
            parts.append(f"unrecognized selection token(s): {', '.join(unrecognized)}")
        if unknown:
            parts.append(f"item id(s) not present in this ledger: {', '.join(unknown)}")
        raise SelectionError("; ".join(parts))
    return ids


def resolve_include_refuted(ledger, tokens):
    """Expand --include-refuted tokens into a set of item ids to re-admit past
    the REFUTED exclusion, scoped strictly per id. Every token must name an
    item that both exists in this ledger AND is currently REFUTED - anything
    else is an error, never a silent no-op, since a typo here must not
    quietly post a different set than the operator asked to overrule. This
    only lifts the REFUTED exclusion for the named ids; it selects nothing by
    itself. It has no effect on whether a re-admitted item is dead or
    on_changed_line: false - those still demote it into the review body
    instead of posting it inline (see build_payload)."""
    if not tokens:
        return set()
    by_id = {it.get("id"): it for it in ledger.get("items", [])
             if isinstance(it.get("id"), int) and not isinstance(it.get("id"), bool)}

    ids, unknown, not_refuted = set(), [], []
    for raw in tokens:
        for tok in str(raw).replace(",", " ").split():
            it = by_id.get(int(tok)) if tok.isdigit() else None
            if it is None:
                unknown.append(tok)
            elif it.get("verdict") != "REFUTED":
                not_refuted.append(tok)
            else:
                ids.add(it["id"])

    if unknown or not_refuted:
        parts = []
        if unknown:
            parts.append(f"item id(s) not present in this ledger: {', '.join(unknown)}")
        if not_refuted:
            parts.append(f"item id(s) not REFUTED, nothing to include: {', '.join(not_refuted)}")
        raise IncludeRefutedError("; ".join(parts))
    return ids


def build_payload(ledger, tokens, body_intro, include_refuted=None):
    """Build the GitHub reviews API body for the selected items, anchored one
    comment per distinct (file, line) site. A cluster whose selected items span
    more than one site gets a 'Same fix as #a, #b.' cross-reference at each of
    its sites, listing every OTHER site's id(s) - never just one, however many
    sites the cluster spans.

    A selected item that cannot be anchored to a changed line (a dead
    anchor, or on_changed_line: false) is never dropped - it was still
    verified and selected, so it is demoted into the review body under its
    own heading instead of being posted inline. REFUTED is the one true
    exclusion: a finding judged not real belongs nowhere in the output
    unless --include-refuted names it, checked before the demotion checks so
    a REFUTED item is excluded rather than demoted even when it is also
    dead. A re-admitted REFUTED item then follows the normal rules above
    (inline if anchorable, demoted if dead or off-diff).

    include_refuted names specific REFUTED item ids to post anyway (per id,
    never blanket - see resolve_include_refuted). An id must still be in
    `tokens` (the --select set) to appear; the flag only lifts the REFUTED
    exclusion, it is not a second selection mechanism."""
    selected = resolve_selection(ledger, tokens)
    admitted = resolve_include_refuted(ledger, include_refuted)
    by_id = {it.get("id"): it for it in ledger.get("items", [])}

    items, demoted, dropped, readmitted = [], [], [], []
    for iid in selected:
        it = by_id.get(iid)
        if it is None:
            continue
        # REFUTED is the only unconditional exclusion, checked first: a
        # finding judged not real belongs nowhere in the output, dead anchor
        # or not, unless --include-refuted names it.
        if it.get("verdict") == "REFUTED" and iid not in admitted:
            dropped.append((iid, "REFUTED"))
            continue
        if it.get("verdict") == "REFUTED":
            readmitted.append(iid)

        if it.get("anchor_status") == "dead":
            demoted.append((it, "dead anchor", it.get("anchor_note")
                             or "anchor could not be relocated in the current diff"))
        # The S5 refuter records on_changed_line; false means the finding does
        # not sit on a line this PR touched, and GitHub rejects the ENTIRE
        # review when any one comment anchors off a changed line - demoted
        # for the same reason as a dead anchor. Only an explicit False
        # demotes: a missing or null value means the field was never filled
        # in (a ledger that has not been through S5 or reanchor), which must
        # stay postable rather than silently demoting every item.
        elif it.get("on_changed_line") is False:
            demoted.append((it, "not on a changed line", it.get("anchor_note")
                             or "not on a changed line in the current diff"))
        else:
            items.append(it)

    if demoted:
        # Distinct from the exclusion warning below: these items WERE kept -
        # just moved out of comments[] into the review body, because GitHub
        # rejects the entire review over one off-diff comment.
        detail = ", ".join(f"#{it['id']} ({reason})"
                            for it, reason, _ in sorted(demoted, key=lambda d: d[0]["id"]))
        print(f"warning: {len(demoted)} selected item(s) moved into the review body "
              f"(cannot be anchored to a changed line) - {detail}", file=sys.stderr)
    if dropped:
        # Selected-but-excluded items are dropped from the payload on purpose
        # (a refuted finding must never be posted by default), but the
        # caller asked for them - say so on stderr so nobody believes a
        # finding was posted when it silently wasn't. Stdout stays pure JSON.
        detail = ", ".join(f"#{iid} (excluded, {reason})" for iid, reason in sorted(dropped))
        print(f"warning: {len(dropped)} selected item(s) not posted - {detail}", file=sys.stderr)
    unselected = sorted(admitted - selected)
    if unselected:
        # --include-refuted only lifts the REFUTED exclusion; it never selects.
        # An id named here but absent from --select posts nothing, and an
        # explicit overrule vanishing without a word is the worst exclusion to
        # lose. Not an error: passing a superset of --select is reasonable.
        detail = ", ".join(f"#{iid}" for iid in unselected)
        print(f"warning: {len(unselected)} item(s) named by --include-refuted are not in "
              f"--select, so nothing was re-admitted for them - {detail}", file=sys.stderr)
    if readmitted:
        # Distinct from the exclusion warning above: these items WERE kept,
        # but only because --include-refuted named them - the operator should
        # see that an override, not the default, put a REFUTED finding back
        # into the payload (inline or demoted).
        detail = ", ".join(f"#{iid}" for iid in sorted(readmitted))
        print(f"warning: {len(readmitted)} item(s) re-admitted by --include-refuted "
              f"despite being REFUTED - {detail}", file=sys.stderr)

    if not items and not demoted:
        # A payload with nothing substantive - no inline comments and no
        # demoted findings - would POST bare framing plus the attribution
        # footer and nothing else. Fail here rather than exit 0, and name
        # why the selection came out empty. A payload with zero comments but
        # a non-empty demoted section is fine: the findings are still in the
        # body, just not anchored inline.
        if dropped:
            why = ("every selected item was excluded - "
                   + ", ".join(f"#{iid} ({reason})" for iid, reason in sorted(dropped)))
        elif not selected:
            why = "the selection resolved to no items"
        else:
            why = "the selected id(s) are not present in the ledger's items"
        raise PayloadError(f"selection produced no comments to post - {why}")

    # A missing or null file/line means the item was never anchored (or lost
    # its anchor and wasn't caught upstream - validate() doesn't check line,
    # and a finding that skipped reanchor never got one at all). Posting it
    # anyway would put `"line": null` in the payload, which GitHub rejects
    # for the whole review, not just that comment - fail loudly here instead.
    # Demoted items are exempt: they render as free text in the body, never
    # anchored to a line, so a missing line there is expected, not an error.
    unanchored = sorted(it.get("id") for it in items if not it.get("file") or it.get("line") is None)
    if unanchored:
        raise PayloadError("item id(s) missing file/line, cannot be anchored to a comment: "
                            + ", ".join(f"#{i}" for i in unanchored))

    sites = {}
    for it in items:
        sites.setdefault((it["file"], it["line"]), []).append(it)

    cluster_sites = {}
    for it in items:
        if it.get("cluster"):
            cluster_sites.setdefault(it["cluster"], set()).add((it["file"], it["line"]))

    comments = []
    for (path, line), group in sorted(sites.items()):
        blocks = []
        for it in sorted(group, key=lambda x: x["id"]):
            text = f"**#{it['id']}** ({it['severity']}) {it['claim']}"
            detail = (it.get("detail") or "").strip()
            if detail:
                text += "\n\n" + detail
            cl = it.get("cluster")
            if cl and len(cluster_sites.get(cl, ())) > 1:
                others = sorted(o["id"] for o in items
                                if o.get("cluster") == cl and (o["file"], o["line"]) != (path, line))
                if others:
                    text += "\n\nSame fix as " + ", ".join(f"#{o}" for o in others) + "."
            blocks.append(text)
        comments.append({"path": path, "line": line, "side": "RIGHT",
                         "body": "\n\n---\n\n".join(blocks)})

    # The attribution footer must appear exactly once and be the LAST thing
    # in the body, after the demoted-findings section below. A body file the
    # author already wrote to their own standing convention may well already
    # contain it - strip any existing occurrence(s) first so it is never
    # duplicated or left stranded ahead of the demoted section, then
    # re-append it once at the very end.
    body = body_intro.strip()
    if ATTRIBUTION in body:
        body = body.replace(ATTRIBUTION, "").rstrip()

    if demoted:
        lines = ["## Findings not anchored to a changed line", ""]
        for it, _reason, note in sorted(demoted, key=lambda d: d[0]["id"]):
            loc = f"{it.get('file')}:{it['line']}" if it.get("line") is not None else it.get("file", "?")
            lines.append(f"**#{it['id']}** ({it['severity']}) {loc} - {it['claim']}")
            detail = (it.get("detail") or "").strip()
            if detail:
                lines.append(""); lines.append(detail)
            lines.append(""); lines.append(f"_Not anchored: {note}_"); lines.append("")
        demoted_section = "\n".join(lines).rstrip()
        body = (body + "\n\n" + demoted_section).strip() if body else demoted_section

    body = (body + "\n\n" + ATTRIBUTION).strip()

    return {"body": body, "event": "COMMENT", "comments": comments}


def cmd_payload(args):
    try:
        led = load(args.ledger)
    except FileNotFoundError:
        print(f"error: ledger file not found: {args.ledger}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: could not read ledger file {args.ledger}: {exc}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"error: ledger file {args.ledger} is not valid JSON: {exc}", file=sys.stderr)
        return 1
    if not isinstance(led, dict):
        print(f"error: ledger root must be a JSON object, got {type(led).__name__}", file=sys.stderr)
        return 1

    try:
        with open(args.body_file, encoding="utf-8") as fh:
            intro = fh.read()
    except FileNotFoundError:
        print(f"error: body file not found: {args.body_file}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: could not read body file {args.body_file}: {exc}", file=sys.stderr)
        return 1

    try:
        payload = build_payload(led, args.select or ["all"], intro,
                                 include_refuted=getattr(args, "include_refuted", None))
    except (SelectionError, PayloadError, IncludeRefutedError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    json.dump(payload, sys.stdout, indent=2)
    return 0


def render_preview(payload):
    """Render a complete, non-truncated human-readable rendering of a GitHub
    review payload dict (the JSON `payload` writes) - a header with the event
    and comment count, the review body in full, then every comment in full
    with its path:line and entire body text. Every text field is emitted
    verbatim (no strip/elide/cap) so the rendering is byte-for-byte what
    `preview`'s caller is about to POST. Plain text and GitHub-flavoured
    markdown only - no raw HTML - since this is printed straight to a
    terminal."""
    comments = payload.get("comments", [])
    rule = "=" * 72
    lines = [rule, "PR REVIEW PAYLOAD PREVIEW",
             f"event: {payload.get('event', '')}",
             f"comments: {len(comments)}", rule, "",
             "REVIEW BODY", "-" * 72, payload.get("body", ""), ""]
    for n, c in enumerate(comments, start=1):
        lines.append("-" * 72)
        lines.append(f"COMMENT {n}/{len(comments)} - {c.get('path', '')}:{c.get('line', '')} "
                      f"({c.get('side', '')})")
        lines.append("-" * 72)
        lines.append(c.get("body", ""))
        lines.append("")
    return "\n".join(lines)


def cmd_preview(args):
    try:
        payload = load(args.payload)
    except FileNotFoundError:
        print(f"error: payload file not found: {args.payload}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: could not read payload file {args.payload}: {exc}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"error: payload file {args.payload} is not valid JSON: {exc}", file=sys.stderr)
        return 1
    if not isinstance(payload, dict):
        print(f"error: payload root must be a JSON object, got {type(payload).__name__}",
              file=sys.stderr)
        return 1
    if "comments" not in payload:
        print(f"error: payload file {args.payload} is missing required field 'comments'",
              file=sys.stderr)
        return 1
    print(render_preview(payload))
    return 0


def main():
    p = argparse.ArgumentParser(prog="pr-review-anchor.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate"); v.add_argument("ledger"); v.set_defaults(fn=cmd_validate)
    r = sub.add_parser("reanchor")
    r.add_argument("ledger"); r.add_argument("--worktree", required=True)
    r.add_argument("--diff", required=True); r.set_defaults(fn=cmd_reanchor)
    y = sub.add_parser("payload")
    y.add_argument("ledger"); y.add_argument("--select", nargs="*")
    y.add_argument("--include-refuted", nargs="*",
                    help="Re-admit specific REFUTED item ids past the default exclusion "
                         "(per id only - never a blanket override)")
    y.add_argument("--body-file", required=True); y.set_defaults(fn=cmd_payload)
    w = sub.add_parser("preview")
    w.add_argument("payload"); w.set_defaults(fn=cmd_preview)
    args = p.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
