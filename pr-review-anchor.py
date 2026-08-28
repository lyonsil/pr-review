#!/usr/bin/env python3
"""Ledger validation, head-drift re-anchoring, and GitHub review payload building
for the PR review pipeline. Standard library only."""
import argparse, json, os, re, sys

SEVERITIES = ("blocker", "high", "medium", "low")
ATTRIBUTION = "_(AI-assisted, with my guidance)_"


class SelectionError(ValueError):
    """A --select token names neither a real item id nor a real cluster id."""


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
                ids.update(by_cluster[tok.upper()])
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


def build_payload(ledger, tokens, body_intro):
    """Build the GitHub reviews API body for the selected items, anchored one
    comment per distinct (file, line) site. A cluster whose selected items span
    more than one site gets a 'Same fix as #a, #b.' cross-reference at each of
    its sites, listing every OTHER site's id(s) - never just one, however many
    sites the cluster spans."""
    selected = resolve_selection(ledger, tokens)
    by_id = {it.get("id"): it for it in ledger.get("items", [])}

    items, dropped = [], []
    for iid in selected:
        it = by_id.get(iid)
        if it is None:
            continue
        if it.get("verdict") == "REFUTED":
            dropped.append((iid, "REFUTED"))
        elif it.get("anchor_status") == "dead":
            dropped.append((iid, "dead anchor"))
        else:
            items.append(it)

    if dropped:
        # Selected-but-excluded items are dropped from the payload on purpose
        # (a refuted finding or a dead anchor must never be posted), but the
        # caller asked for them - say so on stderr so nobody believes a
        # finding was posted when it silently wasn't. Stdout stays pure JSON.
        detail = ", ".join(f"#{iid} ({reason})" for iid, reason in sorted(dropped))
        print(f"warning: {len(dropped)} selected item(s) excluded from payload - {detail}",
              file=sys.stderr)

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

    return {"body": (body_intro.strip() + "\n\n" + ATTRIBUTION).strip(),
            "event": "COMMENT", "comments": comments}


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
        payload = build_payload(led, args.select or ["all"], intro)
    except SelectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    json.dump(payload, sys.stdout, indent=2)
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
    y.add_argument("--body-file", required=True); y.set_defaults(fn=cmd_payload)
    args = p.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
