#!/usr/bin/env python3
"""Ledger validation, head-drift re-anchoring, and GitHub review payload building
for the PR review pipeline. Standard library only."""
import argparse, json, os, re, sys

SEVERITIES = ("blocker", "high", "medium", "low")
ATTRIBUTION = "_(AI-assisted, with my guidance)_"


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


def main():
    p = argparse.ArgumentParser(prog="pr-review-anchor.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate"); v.add_argument("ledger"); v.set_defaults(fn=cmd_validate)
    args = p.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
