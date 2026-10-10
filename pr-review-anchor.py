#!/usr/bin/env python3
"""Ledger validation, head-drift re-anchoring, and review payload building (GitHub
or Reviewable) for the PR review pipeline. Standard library only."""
import argparse, functools, json, os, re, sys
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger_tools as lt  # noqa: E402

# The S5 refuter's four terminal verdicts. Every item must carry one: a
# placeholder like "PENDING" means a refuter's answer was never recorded, and
# the payload builder's exclusion is REFUTED-only, so an unrecorded verdict
# would post as though it had been confirmed. validate() rejects anything
# outside this tuple for exactly that reason.
from ledger_tools import (  # noqa: E402,F401
    SEVERITIES, VERDICTS, demotion_reason, fix_slot, parse_changed_lines)

# The S5 refuter's verdict on the finding's `**Fix:**` slot, which is separate
# from its verdict on the claim. A claim is verified by observation; a fix is a
# proposal about code that does not exist yet, so it needs its own answer and
# its own evidence. Required on every item the payload can publish - i.e. every
# item whose `verdict` is not REFUTED - because `detail` (Fix slot included)
# posts to the author verbatim, and an unanswered fix is indistinguishable from
# a checked one once it is in the ledger.
FIX_VERDICTS = ("SOUND", "REWRITTEN", "UNVERIFIABLE")

# The S5 refuter's verdict on whether the `claim` sentence DESCRIBES the defect
# that was verified - separate again from whether the finding is true. A verdict
# answers "is this true", which a claim narrower than the defect passes, so a
# scope error survives verification in both directions: overstated (reaches past
# the evidence) and understated (names one mode, view or caller when the
# mechanism is general). The claim leads every inline comment, so it is the first
# thing an author reads and the last thing anything checks. Required wherever a
# fix verdict is, and for the same reason: it posts verbatim.
#
# No UNVERIFIABLE member - a claim the refuter could not settle is already
# INCONCLUSIVE at the verdict level, and a second way to say "I could not tell"
# invites using it to avoid the question.
CLAIM_VERDICTS = ("SOUND", "REWRITTEN")

# Phrases that turn a `Fix:` slot from a prescription into a suggestion. The
# contract's rule is that a fix is unhedged about what was verified and silent
# about what was not: where the refuter cannot verify a concrete edit, it states
# the constraint any fix must satisfy - which is imperative - rather than
# offering the author a choice it has not made. Kept deliberately tight, since
# a false positive here blocks a whole ledger; each pattern is a phrase that
# cannot occur in an imperative instruction.
HEDGE_PATTERNS = (
    r"\bconsider\s+(?:whether|if|\w+ing)\b",
    r"\byou\s+(?:might|may|could)\s+want\b",
    r"\bit\s+(?:might|may)\s+be\s+(?:worth|better|preferable|wise)\b",
    r"\bone\s+option\s+(?:is|would\s+be)\b",
    r"\bperhaps\b",
    r"\bif\s+you\s+(?:prefer|like)\b",
    r"\bno(?:ne)?\s+(?:fix|change)?\s*is\s+needed\b",
    r"\bno\s+code\s+change\b",
    r"\bnot\s+required\s+(?:for|in)\s+this\s+(?:PR|change|pull\s+request)\b",
    r"\bshould\s+be\s+(?:downgraded|closed|dropped)\b",
    r"\bif\b.{0,120}?\bthen\s+(?:downgrade|close|drop)\b",
)

# Sections for verdicts routed to the review body instead of posting inline.
# This routing is OPT-IN (--body-only-verdicts); by default placement is decided
# by whether a finding can be anchored, not by what verdict it reached.
#
# Anchoring is a mechanical property of the diff; the verdict is a judgement
# about the finding. Conflating them buried most of a review's findings in one
# wall of body text, where a reader cannot see which of them land in the file
# they are currently reading. A finding's status belongs in its label, not in
# its placement - so every inline comment leads with severity AND verdict, and
# only an unanchorable finding falls back to the body.
BODY_ONLY_VERDICTS = {
    "DOWNGRADED": ("Secondary findings", "real, at the severity shown"),
    "INCONCLUSIVE": ("Unresolved findings",
                     "could not be verified either way - judge these by hand"),
}

# How each verdict is labelled in an inline comment header. These publish to a
# PR author who has no glossary for the pipeline's vocabulary, so each one says
# what it means rather than naming a stage of the review.
#
# DOWNGRADED reads the same as CONFIRMED on purpose. With the severity revision
# no longer published (see severity_text), a downgraded finding IS a real
# finding at the severity shown, and a label distinguishing it would only
# describe an internal revision the author never saw.
VERDICT_LABEL = {
    "CONFIRMED": "checked and confirmed",
    "DOWNGRADED": "checked and confirmed",
    "INCONCLUSIVE": "needs a human call",
    "REFUTED": "probably not a real problem - posted at the reviewer's request",
}

# The prose that introduces a verdict's reasoning where that reasoning is about
# the CODE. DOWNGRADED is deliberately absent: "why this was lowered" is review
# bookkeeping, and verdict_reason_block drops the footer entirely rather than
# renaming it. See that function for where a downgrade's reasoning goes instead.
VERDICT_REASON_LABEL = {
    "CONFIRMED": "How this was checked",
    "INCONCLUSIVE": "What could not be checked",
    "REFUTED": "Why this may not hold",
}


# The verdicts whose verdict_reason publishes by default. REFUTED's footer
# only posts when --include-refuted names it, and DOWNGRADED has none.
FOOTER_PUBLISHES = ("CONFIRMED", "INCONCLUSIVE")


def severity_text(it):
    """The severity a finding is posted at, and only that.

    `severity_in` stays in the ledger and the S6 triage table still prints
    `<in> -> <out>`, because that arrow is what tells the reviewer whether the
    pressure-test is calibrated. It does not publish: a PR author reading one
    comment has no use for the revision history of a number they never saw."""
    return str(it.get("severity"))


def verdict_reason_block(it):
    """The published footer carrying the pressure-test's reasoning, or "" when
    that reasoning is about the review rather than about the code.

    DOWNGRADED has no entry in VERDICT_REASON_LABEL, so it renders nothing.
    Whatever a downgrade's reasoning holds that changes what the author needs to
    know - that a path is gated, unreachable, or pre-existing - is folded into
    the finding's own `Why it matters:` slot while the review is drafted, in the
    author's terms. The refuter's `verdict_reason` stays intact in the ledger
    either way; this only decides what publishes."""
    label = VERDICT_REASON_LABEL.get(it.get("verdict"))
    reason = (it.get("verdict_reason") or "").strip()
    return f"_{label}: {reason}_" if label and reason else ""

ATTRIBUTION = "_(AI-assisted, with my guidance)_"


class SelectionError(ValueError):
    """A --select token names neither a real item id nor a real cluster id."""


class PayloadError(ValueError):
    """A selected item can't be safely anchored into a GitHub review comment."""


class IncludeRefutedError(ValueError):
    """An --include-refuted token doesn't name a REFUTED item in this ledger."""


def hedges_in(text):
    """Every hedge phrase present in `text`, as matched, in pattern order."""
    found = []
    for pat in HEDGE_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            found.append(m.group(0))
    return found


def validate(ledger, worktree=None):
    if not isinstance(ledger, dict):
        return [f"ledger root must be a JSON object, got {type(ledger).__name__}"]
    if ledger.get("schema") != lt.SCHEMA:
        return [f"ledger schema {ledger.get('schema')!r} is not supported - this is schema "
                f"{ledger.get('schema')} and {lt.SCHEMA} is required; re-run /pr-review"]
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
        # severity_in is what the generator assigned, before the refuter's
        # revision. Without it a refuter that downgrades and refutes in one
        # step leaves a triage table where every dropped finding reads as
        # low-severity noise, hiding that high-severity findings were dropped.
        if it.get("severity_in") not in SEVERITIES:
            errs.append(f"item {iid!r} severity_in {it.get('severity_in')!r} not one of "
                        f"{SEVERITIES} (record the severity the generator assigned)")
        if it.get("verdict") not in VERDICTS:
            errs.append(f"item {iid!r} verdict {it.get('verdict')!r} not one of {VERDICTS} "
                        "(every item must carry a terminal verdict before the ledger is ready)")
        if not str(it.get("verdict_reason") or "").strip():
            errs.append(f"item {iid!r} is missing required field 'verdict_reason'")
        # The fix is answered separately from the claim, and only where it can
        # reach an author: a REFUTED item posts nowhere, so holding its fix to
        # this bar would block ledgers over text nobody will read.
        if it.get("verdict") != "REFUTED":
            if it.get("fix_verdict") not in FIX_VERDICTS:
                errs.append(f"item {iid!r} fix_verdict {it.get('fix_verdict')!r} not one of "
                            f"{FIX_VERDICTS} (the Fix: slot posts verbatim, so it needs its own "
                            "verified answer before the ledger is ready)")
            if not str(it.get("fix_reason") or "").strip():
                errs.append(f"item {iid!r} is missing required field 'fix_reason' "
                            "(what was checked about the fix, with file:line evidence)")
            hedged = hedges_in(fix_slot(it.get("detail")))
            if hedged:
                errs.append(f"item {iid!r} Fix: slot is hedged ({', '.join(repr(h) for h in hedged)}) "
                            "- state the concrete edit, or state the constraint any fix must "
                            "satisfy; do not offer the author a choice you have not made")
            # The claim is answered separately again. Scoped to the same
            # publishable items as the fix: a REFUTED claim reaches nobody.
            # An unanswered INCONCLUSIVE item is exempt too: there was nothing
            # for a refuter to verify the claim against.
            unanswered = it.get("unanswered") is True and it.get("verdict") == "INCONCLUSIVE"
            if not unanswered and it.get("claim_verdict") not in CLAIM_VERDICTS:
                errs.append(f"item {iid!r} claim_verdict {it.get('claim_verdict')!r} not one of "
                            f"{CLAIM_VERDICTS} (the claim leads every inline comment, so whether it "
                            "describes the verified defect needs its own answer)")
            if not unanswered and not str(it.get("claim_reason") or "").strip():
                errs.append(f"item {iid!r} is missing required field 'claim_reason' "
                            "(what the claim was matched against, with file:line evidence)")
            for msg in lt.claim_shape_errors(it.get("claim")):
                errs.append(f"item {iid!r} {msg}")
            for msg in lt.why_it_matters_errors(it.get("detail")):
                errs.append(f"item {iid!r} {msg}")
            if it.get("claim_verdict") == "REWRITTEN" and (
                    not it.get("claim_original") or it.get("claim_original") == it.get("claim")):
                errs.append(f"item {iid!r} claim_verdict is REWRITTEN but claim_original is "
                            "missing or identical to claim - the rewrite was not applied")
            if it.get("fix_verdict") == "REWRITTEN" and (
                    not it.get("fix_original")
                    or it.get("fix_original") == fix_slot(it.get("detail"))):
                errs.append(f"item {iid!r} fix_verdict is REWRITTEN but fix_original is "
                            "missing or identical to the Fix: slot - the rewrite was not applied")
            for field in ("claim", "detail"):
                for phrase in lt.process_vocabulary_in(it.get(field)):
                    errs.append(f"item {iid!r} {field} contains review-process wording "
                                f"{phrase!r} - the author never saw this pipeline")
            # The reason posts as the comment's footer on these verdicts (see
            # verdict_reason_block). A bare `#123` is allowed there: reasons
            # cite PRs and issues that way.
            if it.get("verdict") in FOOTER_PUBLISHES:
                for phrase in lt.process_vocabulary_in(it.get("verdict_reason"), bare_refs=False):
                    errs.append(f"item {iid!r} verdict_reason contains review-process wording "
                                f"{phrase!r} - it posts under the finding; reword it with `edit`")
        if it.get("verdict") == "DOWNGRADED":
            if lt.SEV_RANK.get(it.get("severity"), -1) <= lt.SEV_RANK.get(it.get("severity_in"), 99):
                errs.append(f"item {iid!r} is DOWNGRADED but severity {it.get('severity')!r} is "
                            f"not lower than severity_in {it.get('severity_in')!r} - a narrowed "
                            "claim at the same severity is CONFIRMED with a rewritten claim")
            if it.get("fold_note") not in ("folded", "bookkeeping"):
                errs.append(f"item {iid!r} is DOWNGRADED without fold_note 'folded' or "
                            "'bookkeeping'")
        if it.get("verdict") == "REFUTED" and (
                "REWRITTEN" in (it.get("claim_verdict"), it.get("fix_verdict"))
                or it.get("claim_original") or it.get("fix_original")):
            errs.append(f"item {iid!r} is REFUTED but carries a rewrite - a refuted claim has "
                        "nothing to rewrite; if something true survives it is DOWNGRADED")
        if any(lt.is_ellipsis_line(l) for l in (it.get("anchor_excerpt") or "").splitlines()):
            errs.append(f"item {iid!r} anchor_excerpt contains an ellipsis line - run `excerpt`")
        for field in ("file", "claim", "anchor_excerpt"):
            if not it.get(field):
                errs.append(f"item {iid!r} is missing required field '{field}'")

    int_ids = [it["id"] for it in items if isinstance(it.get("id"), int) and not isinstance(it.get("id"), bool)]
    if len(set(int_ids)) != len(int_ids):
        errs.append("duplicate item ids")
    elif int_ids and int_ids != list(range(1, len(int_ids) + 1)):
        errs.append(f"ids are not contiguous 1..N in list order (got {int_ids[:6]})")

    rank = {s: n for n, s in enumerate(SEVERITIES)}
    keyed = [(rank.get(it.get("severity"), 99), it.get("file") or "") for it in items]
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

    if worktree:
        errs.extend(lt.excerpt_errors(ledger, worktree))
    return errs


def draft_errors(ledger):
    """Read-only. The checks a draft ledger (S4, before any brief) must pass:
    what `brief` and `ingest` rely on and nothing a refuter or later step
    fills in. Every item's `cluster` and every cluster's `item_ids` must
    agree both ways, because refuter units are built from both - an item a
    cluster does not list is never dispatched, and an unclustered item a
    cluster lists is dispatched twice."""
    if ledger.get("schema") != lt.SCHEMA:
        return [f"ledger schema {ledger.get('schema')!r} is not supported; {lt.SCHEMA} is required"]
    errs = [f"draft is missing '{f}'" for f in ("head_sha", "merge_base", "worktree")
            if not ledger.get(f)]
    items, ids = ledger.get("items", []), set()
    for n, it in enumerate(items):
        iid = it.get("id")
        if not isinstance(iid, int) or isinstance(iid, bool):
            errs.append(f"item[{n}] id {iid!r} is not an integer")
        elif iid in ids:
            errs.append(f"duplicate item id {iid}")
        ids.add(iid)
        if it.get("severity_in") not in SEVERITIES:
            errs.append(f"item {iid!r} severity_in {it.get('severity_in')!r} not one of {SEVERITIES}")
        # Checked at draft time too, so the slot is written with the finding,
        # not patched in after the refuters have read it.
        for msg in lt.why_it_matters_errors(it.get("detail")):
            errs.append(f"item {iid!r} {msg}")
    by_id = {it.get("id"): it for it in items}
    listed = {}
    for c in ledger.get("clusters", []):
        cid = c.get("id")
        if not isinstance(c.get("item_ids"), list):
            errs.append(f"{cid} has no item_ids list")
            continue
        listed[cid] = set(c["item_ids"])
        for ref in c["item_ids"]:
            if ref not in by_id:
                errs.append(f"{cid} lists item {ref}, which is not in items")
            elif by_id[ref].get("cluster") != cid:
                errs.append(f"{cid} lists item {ref}, whose cluster is {by_id[ref].get('cluster')!r}")
    known = {c.get("id") for c in ledger.get("clusters", [])}
    for it in items:
        cl = it.get("cluster")
        if cl and cl not in known:
            errs.append(f"item {it.get('id')} has cluster {cl!r}, which is not in clusters")
        elif cl in listed and it.get("id") not in listed[cl]:
            errs.append(f"item {it.get('id')} has cluster {cl!r}, but {cl}'s item_ids do not list it")
    return errs


def _lt_errors(exc):
    for line in str(exc).splitlines():
        print(f"error: {line}", file=sys.stderr)
    return 1


def _cli(fn):
    """Every subcommand's error contract in one place: a `LedgerError`, a
    failed file operation, or undecodable input becomes `error:` lines on
    stderr and exit 1, never a traceback."""
    @functools.wraps(fn)
    def run(args):
        try:
            return fn(args)
        except lt.LedgerError as exc:
            return _lt_errors(exc)
        except UnicodeDecodeError as exc:
            return _lt_errors(f"input is not valid UTF-8: {exc}")
        except OSError as exc:
            return _lt_errors(exc)
    return run


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


@_cli
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
    if not isinstance(led, dict):
        print(f"error: ledger root must be a JSON object, got {type(led).__name__}", file=sys.stderr)
        return 1
    draft = getattr(args, "draft", False)
    errs = draft_errors(led) if draft else validate(led, getattr(args, "worktree", None))
    if errs:
        for e in errs:
            print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"{'draft' if draft else 'ledger'} ok: {len(led.get('items', []))} items, "
          f"{len(led.get('clusters', []))} clusters")
    return 0


def _resolve_anchor(item, worktree, changed):
    """Where `item`'s anchor lands at this head: (status, line, line_end, why).

    `line`/`line_end` are None for a dead anchor. Otherwise the excerpt is
    re-matched, the whole range shifts by the drift (matched line minus the
    ledger's line), and `lt.first_changed_line` picks the start inside the
    shifted range - the end stays at the shifted end, never re-derived from
    the moved start. An off-diff anchor keeps the shifted range as is."""
    path = os.path.join(worktree, item["file"])
    if not os.path.isfile(path):
        return "dead", None, None, "file no longer present at head"
    with open(path, encoding="utf-8", errors="replace") as fh:
        content = fh.read().splitlines()
    excerpt = item.get("anchor_excerpt", "").splitlines()
    if not excerpt:
        return "dead", None, None, "empty anchor excerpt"
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
        return "dead", None, None, "anchor excerpt no longer present"
    old = item.get("line") or hits[0]
    matched = min(hits, key=lambda h: abs(h - old))
    note = ""
    if len(hits) > 1:
        others = [h for h in hits if h != matched]
        note = f"ambiguous: excerpt also matches at line(s) {others}; nearest to {old} chosen"
    end = max((item.get("line_end") or old) + (matched - old), matched)
    target = lt.first_changed_line(changed.get(item["file"], set()), matched, end)
    if target is None:
        reason = "not on a changed line; cannot be posted inline"
        return "off-diff", matched, end, f"{reason} ({note})" if note else reason
    if target == item.get("line"):
        return "ok", target, end, note
    moved_reason = f"moved {item.get('line')} -> {target}"
    return "moved", target, end, f"{moved_reason} ({note})" if note else moved_reason


def reanchor_item(item, worktree, changed):
    """Read-only. `_resolve_anchor` without the range end: (status, line, why)."""
    status, line, _end, why = _resolve_anchor(item, worktree, changed)
    return status, line, why


@_cli
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
            diff_text = fh.read()
        changed, files = parse_changed_lines(diff_text), lt.diff_files(diff_text)
    except FileNotFoundError:
        print(f"error: diff file not found: {args.diff}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: could not read diff file {args.diff}: {exc}", file=sys.stderr)
        return 1

    counts = {"ok": 0, "moved": 0, "off-diff": 0, "dead": 0}
    corrected = []
    for it in led.get("items", []):
        status, new_line, new_end, why = _resolve_anchor(it, args.worktree, changed)
        kept = (it.get("line"), it.get("line_end") or it.get("line"))
        if status in ("ok", "moved") and (new_line, new_end) != kept:
            try:
                excerpt = lt.read_lines(args.worktree, it["file"], new_line, new_end)
            except lt.LedgerError as exc:
                status, why = "dead", str(exc)
            else:
                it["line"], it["line_end"] = new_line, new_end
                it["anchor_excerpt"] = "\n".join(excerpt)
        elif status == "off-diff":
            # The matched excerpt is unchanged (it matched verbatim), so the
            # whole range moves by the drift - leaving line_end behind would
            # shrink the range a later pass searches for a changed line.
            it["line"], it["line_end"] = new_line, new_end
        it["anchor_status"], it["anchor_note"] = status, why
        # Whether the file is in the diff at all, whatever became of the
        # anchor: a file with no added line, or with no hunk at all, counts.
        it["file_in_diff"] = it["file"] in files
        # on_changed_line is a property of the diff. A dead excerpt tells us
        # nothing about the diff, so it leaves the recorded value alone.
        if status != "dead":
            was = it.get("on_changed_line")
            now = status in ("ok", "moved")
            it["on_changed_line"] = now
            if was is not None and bool(was) != now:
                corrected.append((it.get("id"), bool(was), now))
        counts[status] += 1
        # The line the ledger now holds: a dead anchor (including a recut
        # that could not be read) keeps its old line.
        print(f"{status:6} #{it.get('id')!s:<3} {it.get('file')}:{it.get('line')} {why}")

    if getattr(args, "head", None):
        led["anchored_sha"] = args.head
    if getattr(args, "dry_run", False):
        print("dry run: ledger not written")
    else:
        lt.write_ledger(args.ledger, led, "reanchor")

    if corrected:
        # on_changed_line is derived from the diff, not trusted from the
        # ledger, so any change here means the recorded value no longer
        # matches - worth knowing about the ledger's other answers too.
        detail = ", ".join(f"#{iid} {was} -> {now}" for iid, was, now in corrected)
        print(f"\nnotice: on_changed_line changed against the diff for "
              f"{len(corrected)} item(s) - {detail}", file=sys.stderr)

    print(f"\nok={counts['ok']} moved={counts['moved']} off-diff={counts['off-diff']} "
          f"dead={counts['dead']}")
    return 0


def resolve_selection(ledger, tokens):
    """Expand --select tokens ('all', integer ids, ranges like '4-9', cluster ids
    like 'C3') into a set of item ids. Every token must resolve to something
    real in THIS ledger: a typo'd id, a backwards range (`a > b`), or an id
    that isn't in the ledger raises rather than silently posting a smaller set
    than the caller asked for."""
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
            elif re.fullmatch(r"\d+-\d+", tok):
                a, b = (int(x) for x in tok.split("-"))
                if a > b:
                    unrecognized.append(tok)
                for requested in range(a, b + 1):
                    if requested in known_ids:
                        ids.add(requested)
                    else:
                        unknown.append(str(requested))
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
    on_changed_line: false - those still place it exactly as they would any
    other finding (see plan_review)."""
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


@dataclass(frozen=True)
class SiteFinding:
    """One finding's contribution to a site: which finding it is, and the two
    facts about it that decide how forcefully a backend raises the site.

    `severity` is the severity the finding PUBLISHES at (see severity_text),
    never `severity_in`: a backend weighting a comment by a number the reader
    never sees would put its urgency out of step with its own header.

    Recorded per finding rather than summarised per site, because a site is
    merged text and how several findings combine into one thread's urgency is
    a backend's rule, not the plan's (see site_disposition)."""
    item_id: int
    severity: str
    verdict: str


@dataclass(frozen=True)
class Site:
    """One anchored location and the whole of the text that publishes there.

    `findings` records which findings' text `body` holds, in the order it
    holds them, so a renderer can classify the site and a caller can report
    what posted per finding without parsing the markdown back apart.
    `item_ids` is derived from it rather than stored beside it: two stored
    lists of the same thing can drift, and a site naming findings it did not
    post misattributes a published comment.

    `off_diff` marks a site outside the PR's diff. Only a plan built for a
    backend that accepts such a comment ever holds one (see plan_review)."""
    path: str
    line: int
    body: str
    findings: tuple
    off_diff: bool = False

    @property
    def item_ids(self):
        """The ids of the findings `body` holds, in the order it holds them."""
        return tuple(f.item_id for f in self.findings)


@dataclass(frozen=True)
class ReviewPlan:
    """What a review publishes and where, decided and rendered to markdown but
    not yet shaped for any one backend's API. A renderer translates it into
    request fields and decides nothing about placement or wording.

    `sites` is ordered by (file, line) so the same ledger and selection give
    the same output in the same order every run: the approval gate is a read
    of the rendered text, and a shuffled order makes every re-run look like a
    changed review.

    `body_findings` records the findings `body` carries - those that could
    not be attached to a line and those a body-only verdict routed there - as
    SiteFinding entries, so a backend that raises the body as a comment of its
    own can weigh it as it weighs a site."""
    body: str
    sites: tuple
    body_findings: tuple = ()


# The opening line of a comment that lands on a line the PR does not touch.
# Without it a reader meets a comment on code this change did not write, with
# nothing saying why it is there - and both readings that leaves open (the
# reviewer misread the diff; the comment landed on the wrong line) end with the
# finding ignored. It states the situation and nothing about how the review
# reached it, and is count-neutral so a site merging several findings carries
# it once. Only a backend that posts off-diff comments inline ever emits it:
# GitHub cannot attach one at all, and its review body says as much in the
# heading those findings sit under.
OFF_DIFF_NOTE = ("_This comment is about the code on this line, which this pull request "
                 "does not change._")


def render_review_body(body_intro, count, demoted, secondary, tok):
    """The review body: the operator's framing with `{count}` filled, then the
    findings that could not be attached to a line, then one section per
    body-only verdict, then the attribution footer.

    `demoted` is (item, reason, note) triples and `secondary` is (item,
    verdict) pairs; which findings those are is plan_review's decision, made
    per backend. `tok` renders `{{#n}}` tokens and records any reference to a
    finding outside the review.

    The attribution footer must appear exactly once and be the LAST thing
    in the body, after the demoted-findings section below. A body file the
    author already wrote to their own standing convention may well already
    contain it - strip any existing occurrence(s) first so it is never
    duplicated or left stranded ahead of the demoted section, then
    re-append it once at the very end."""
    body = body_intro.replace("{count}", str(count)).strip()
    if ATTRIBUTION in body:
        body = body.replace(ATTRIBUTION, "").rstrip()

    if demoted:
        lines = ["## Findings that could not be attached to a line in this diff", ""]
        for it, _reason, note in sorted(demoted, key=lambda d: d[0]["id"]):
            loc = f"{it.get('file')}:{it['line']}" if it.get("line") is not None else it.get("file", "?")
            verdict = it.get("verdict")
            label = VERDICT_LABEL.get(verdict)
            head = f"**#{it['id']}** ({severity_text(it)}"
            head += f" · {label})" if label else ")"
            lines.append(f"{head} {loc} - {tok(it, it['claim'])}")
            detail = tok(it, (it.get("detail") or "").strip())
            if detail:
                lines.append(""); lines.append(detail)
            # These are here only because they could not anchor, so they carry
            # the same verdict reasoning an inline comment would have shown.
            block = tok(it, verdict_reason_block(it))
            if block:
                lines.append(""); lines.append(block)
            lines.append(""); lines.append(f"_Not attached to a line: {note}_"); lines.append("")
        demoted_section = "\n".join(lines).rstrip()
        body = (body + "\n\n" + demoted_section).strip() if body else demoted_section

    # One section per body-only verdict, in the order BODY_ONLY_VERDICTS
    # declares them, so a run's body reads the same way every time.
    for verdict, (heading, gloss) in BODY_ONLY_VERDICTS.items():
        group = [it for it, v in secondary if v == verdict]
        if not group:
            continue
        lines = [f"## {heading} ({gloss})", ""]
        for it in sorted(group, key=lambda x: x["id"]):
            loc = f"{it.get('file')}:{it['line']}" if it.get("line") is not None else it.get("file", "?")
            lines.append(f"**#{it['id']}** ({severity_text(it)}) {loc} - {tok(it, it['claim'])}")
            detail = tok(it, (it.get("detail") or "").strip())
            if detail:
                lines.append(""); lines.append(detail)
            block = tok(it, verdict_reason_block(it))
            if block:
                lines.append(""); lines.append(block)
            lines.append("")
        section = "\n".join(lines).rstrip()
        body = (body + "\n\n" + section).strip() if body else section

    return (body + "\n\n" + ATTRIBUTION).strip()


def plan_review(ledger, tokens, body_intro, include_refuted=None,
                body_only_verdicts=None, off_diff_inline=False):
    """Decide what the selected items publish and where, and render every
    published word, as a ReviewPlan: one site per distinct (file, line), and
    the review body. A cluster whose selected items span more than one site
    gets a 'Similar fix as #a, #b.' cross-reference at each of its sites,
    listing every OTHER site's id(s) - never just one, however many sites the
    cluster spans.

    `off_diff_inline` is the one thing about the backend this needs to know:
    whether it can attach a comment to a line the PR does not change. GitHub
    cannot - it rejects the ENTIRE review over one such comment - so with it
    False an off-diff finding is demoted into the body like a dead anchor.
    With it True an off-diff finding that has a file and a line becomes a
    site of its own, opening with OFF_DIFF_NOTE, and the cross-references
    count it like any other site, because on that backend it is one - unless
    `file_in_diff` is False, since a file the pull request does not touch
    has no entry on that backend to attach it to, so it goes in the body.
    Placement is a property of the backend, never of the provider's name, so
    this takes the capability and not the provider. On either backend a site
    line that is not a positive integer is refused, before approval rather
    than in the request.

    A selected item is never dropped for anchoring reasons - it was still
    verified and selected. A dead anchor goes in the body on every backend:
    the excerpt is gone, so nobody can say which line it belongs on.

    REFUTED is the one true exclusion: a finding judged not real belongs
    nowhere in the output unless --include-refuted names it, checked before
    the demotion checks so a REFUTED item is excluded rather than demoted
    even when it is also dead. A re-admitted REFUTED item then follows the
    normal rules above.

    include_refuted names specific REFUTED item ids to post anyway (per id,
    never blanket - see resolve_include_refuted). An id must still be in
    `tokens` (the --select set) to appear; the flag only lifts the REFUTED
    exclusion, it is not a second selection mechanism.

    body_only_verdicts optionally forces whole verdicts into the body even when
    they anchor cleanly. It defaults to empty: placement follows anchoring, and
    each comment carries its verdict in its header instead.

    Every `claim`/`detail` that publishes - inline, demoted, or in a
    body-only-verdicts section - has its `{{#n}}` cross-reference tokens
    rendered as plain `#n`; a token naming an id that does not publish
    anywhere in this review prints a stderr warning rather than posting a
    dead link. `body_intro`'s literal `{count}` is filled with the number of
    ids that publish."""
    selected = resolve_selection(ledger, tokens)
    admitted = resolve_include_refuted(ledger, include_refuted)
    body_only = set(body_only_verdicts or ())
    by_id = {it.get("id"): it for it in ledger.get("items", [])}

    items, off_diff, demoted, dropped, readmitted, secondary = [], [], [], [], [], []
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

        # Opt-in only. By default this set is empty and a DOWNGRADED or
        # INCONCLUSIVE finding posts inline like any other, carrying its
        # verdict in the comment header - see BODY_ONLY_VERDICTS for why.
        elif it.get("verdict") in body_only:
            secondary.append((it, it["verdict"]))
            continue

        # demotion_reason is the single source of truth for this routing
        # rule - render_report's inline/body count uses the same function so
        # the two never disagree about what actually posts inline.
        reason = demotion_reason(it)
        if reason == "dead anchor":
            demoted.append((it, reason, it.get("anchor_note")
                             or "the code this finding pointed at has moved or gone "
                                "since the review ran"))
        # The S5 refuter records on_changed_line, and reanchor records
        # anchor_status: "off-diff" when a moved anchor lands outside the
        # diff - either means the finding does not sit on a line this PR
        # touched, and GitHub rejects the ENTIRE review when any one comment
        # anchors off a changed line - demoted for the same reason as a dead
        # anchor. demotion_reason treats a missing or null on_changed_line as
        # postable: the field was never filled in (a ledger that has not
        # been through S5 or reanchor), which must stay postable rather than
        # silently demoting every item.
        elif reason == "not on a changed line":
            if not off_diff_inline:
                demoted.append((it, reason, it.get("anchor_note")
                                 or "this finding is not on a line the PR changed, so GitHub "
                                    "cannot attach a comment to it"))
            # A backend that accepts off-diff comments still needs somewhere
            # to put one. With no file or no line there is no location on any
            # backend, so the finding goes in the body - and says so in those
            # terms, since "not on a changed line" would be the wrong reason
            # on a backend where being off the diff is no obstacle.
            elif not it.get("file") or it.get("line") is None:
                demoted.append((it, "no file or line", it.get("anchor_note")
                                 or "this finding has no file and line to attach a comment to"))
            # Such a backend keys a comment to one of the pull request's own
            # files, so a file the diff never touches has no entry to key it
            # to. Only an explicit False moves it: a ledger written before the
            # field existed plans as it always did. The note is fixed rather
            # than taken from anchor_note, whose off-diff wording gives
            # GitHub's reason, not this one.
            elif it.get("file_in_diff") is False:
                demoted.append((it, "file not in the pull request",
                                "this finding is in a file the pull request does not change, "
                                "so the review has no file to attach a comment to"))
            else:
                off_diff.append(it)
        else:
            items.append(it)

    if demoted:
        detail = ", ".join(f"#{it['id']} ({reason})"
                            for it, reason, _ in sorted(demoted, key=lambda d: d[0]["id"]))
        if not off_diff_inline:
            # Distinct from the exclusion warning below: these items WERE kept -
            # just moved out of comments[] into the review body, because GitHub
            # rejects the entire review over one off-diff comment.
            print(f"warning: {len(demoted)} selected item(s) moved into the review body "
                  f"(cannot be anchored to a changed line) - {detail}", file=sys.stderr)
        else:
            # The same accounting in terms true of a backend that posts off-diff
            # findings inline: only findings with nowhere in the pull request to
            # go land here - a dead anchor, no file or line, or a file the pull
            # request does not touch. The review-level comment carrying them is
            # raised as its most demanding finding (see render_reviewable), so
            # the operator learns before the preview that it publishes resolved
            # only when none of them asks for a reply.
            print(f"warning: {len(demoted)} selected item(s) moved into the review-level comment, "
                  f"which publishes resolved only when nothing in it asks for a reply (no "
                  f"location in this pull request to attach a comment to) - {detail}",
                  file=sys.stderr)
    if off_diff:
        # Kept inline, but on code the PR does not change - the operator should
        # know which comments will open with OFF_DIFF_NOTE before reading them.
        detail = ", ".join(f"#{iid}" for iid in sorted(it["id"] for it in off_diff))
        print(f"notice: {len(off_diff)} selected item(s) are not on a line this PR changes and "
              f"post inline, each site opening with a note saying so - {detail}",
              file=sys.stderr)
    if secondary:
        # Kept, like demoted items - but for a verdict reason, not an anchoring
        # one. Say so separately so the operator can tell "the refuter judged
        # this smaller than claimed" from "GitHub would reject this anchor".
        # On a backend with a review-level comment that is where they go,
        # raised under the same rule as the demoted findings beside them.
        detail = ", ".join(f"#{it['id']} ({verdict})"
                            for it, verdict in sorted(secondary, key=lambda d: d[0]["id"]))
        if not off_diff_inline:
            print(f"notice: {len(secondary)} selected item(s) carried in the review body rather "
                  f"than posted inline - {detail}", file=sys.stderr)
        else:
            print(f"notice: {len(secondary)} selected item(s) carried in the review-level comment "
                  f"rather than posted inline, which publishes resolved only when nothing in it "
                  f"asks for a reply - {detail}", file=sys.stderr)
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

    if not items and not off_diff and not demoted and not secondary:
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
    # Off-diff sites are exempt by construction: they only become sites with
    # a file and a line.
    unanchored = sorted(it.get("id") for it in items if not it.get("file") or it.get("line") is None)
    if unanchored:
        raise PayloadError("item id(s) missing file/line, cannot be anchored to a comment: "
                            + ", ".join(f"#{i}" for i in unanchored))

    # A line that is not a positive integer names no line of any file, so it
    # is refused here, before approval, on every backend rather than sent -
    # Reviewable, which creates each discussion in its own call, would reject
    # it halfway through posting. It is checked before the sites are sorted,
    # which cannot order a string line against an integer one, so the refusal
    # is an `error:` line and never a traceback.
    bad = sorted(it.get("id") for it in items + off_diff
                 if not isinstance(it["line"], int) or isinstance(it["line"], bool)
                 or it["line"] < 1)
    if bad:
        raise PayloadError("item id(s) have a line that is not a positive integer, cannot be "
                           "attached to a comment: " + ", ".join(f"#{i}" for i in bad))

    # Every id that actually publishes somewhere - inline, demoted, or in a
    # body-only-verdicts section - fills {count} and is what a {{#n}} token is
    # checked against: a token pointing at a REFUTED or otherwise-dropped id
    # is a broken cross-reference even though that id is still in the ledger.
    posted = ({it["id"] for it in items} | {it["id"] for it in off_diff}
              | {it["id"] for it, _, _ in demoted} | {it["id"] for it, _ in secondary})

    outside = []

    def tok(it, text):
        rendered, refs = lt.render_tokens(text)
        outside.extend((it["id"], r) for r in sorted(refs - posted))
        return rendered

    # Keyed by (file, line, off_diff) so an off-diff finding never merges into
    # an on-diff site on the same line: the merged comment would open with
    # OFF_DIFF_NOTE and tell the reader the on-diff finding's line is
    # unchanged, which is false.
    sites = {}
    for it in items:
        sites.setdefault((it["file"], it["line"], False), []).append(it)
    for it in off_diff:
        sites.setdefault((it["file"], it["line"], True), []).append(it)

    # Everything that posts inline on this backend. Both cross-reference maps
    # are built from it, so a site outside the diff is named from the other
    # sites exactly when it is a comment the reader can go and find.
    inline = items + off_diff

    cluster_sites = {}
    for it in inline:
        if it.get("cluster"):
            cluster_sites.setdefault(it["cluster"], set()).add((it["file"], it["line"]))

    # Which other selected findings land in the same file. This is the payoff
    # for deciding placement by anchoring: a reader in one file sees what else
    # is wrong in it without reading the whole review first.
    file_sites = {}
    for it in inline:
        file_sites.setdefault(it["file"], set()).add(it["id"])

    anchored = []
    for (path, line, off), group in sorted(sites.items()):
        ordered = sorted(group, key=lambda x: x["id"])
        blocks = []
        for it in ordered:
            verdict = it.get("verdict")
            label = VERDICT_LABEL.get(verdict)
            head = f"**#{it['id']}** - {severity_text(it)}"
            if label:
                head += f" · {label}"
            text = head + "\n\n" + tok(it, it["claim"])
            detail = tok(it, (it.get("detail") or "").strip())
            if detail:
                text += "\n\n" + detail
            # Carries the pressure-test's reasoning when it is about the code.
            # Empty for a downgrade - see verdict_reason_block.
            block = tok(it, verdict_reason_block(it))
            if block:
                text += "\n\n" + block
            cl = it.get("cluster")
            if cl and len(cluster_sites.get(cl, ())) > 1:
                # A sibling on this same (file, line) is not "elsewhere", even
                # when it is a separate off-diff site - the same-file line below
                # names it.
                others = sorted(o["id"] for o in inline
                                if o.get("cluster") == cl
                                and (o["file"], o["line"]) != (path, line))
                if others:
                    text += "\n\nSimilar fix as " + ", ".join(f"#{o}" for o in others) + "."
            same_file = sorted(file_sites.get(path, set()) - {it["id"]})
            if same_file:
                text += ("\n\n_Other findings in this file: "
                         + ", ".join(f"#{o}" for o in same_file) + "_")
            blocks.append(text)
        site_body = "\n\n---\n\n".join(blocks)
        # Once per site, not per finding: the note is a fact about where the
        # merged comment lands, and repeated per block it would read as part
        # of each finding.
        if off:
            site_body = OFF_DIFF_NOTE + "\n\n" + site_body
        anchored.append(Site(
            path=path, line=line, body=site_body, off_diff=off,
            findings=tuple(SiteFinding(item_id=it["id"], severity=severity_text(it),
                                       verdict=it.get("verdict")) for it in ordered)))

    body = render_review_body(body_intro, len(posted), demoted, secondary, tok)

    # A {{#n}} cross-reference to an id that never publishes anywhere in this
    # payload (REFUTED, dropped, or simply not selected) would print as a
    # link to nothing an author can find - warn rather than posting it silently.
    for a, b in sorted(set(outside)):
        print(f"warning: #{a} references #{b}, which is not in this review", file=sys.stderr)

    carried = [it for it, _, _ in demoted] + [it for it, _ in secondary]
    return ReviewPlan(body=body, sites=tuple(anchored), body_findings=tuple(
        SiteFinding(item_id=it["id"], severity=severity_text(it), verdict=it.get("verdict"))
        for it in carried))


def build_payload(ledger, tokens, body_intro, include_refuted=None,
                  body_only_verdicts=None):
    """Build the GitHub reviews API body for the selected items: the review
    text, the event, and one comment per anchored site. What publishes, where,
    and in what words is plan_review's decision (see it for the placement and
    cross-reference rules); this only restates the plan in GitHub's fields.

    The plan is built without off-diff sites, because GitHub rejects the
    ENTIRE review over a single comment on a line outside its diff - so those
    findings arrive in the body instead. Every comment anchors on the RIGHT
    side because the plan's lines are new-side lines re-anchored against head;
    the event is COMMENT because the review leaves findings for the author
    rather than approving or blocking."""
    plan = plan_review(ledger, tokens, body_intro, include_refuted=include_refuted,
                       body_only_verdicts=body_only_verdicts, off_diff_inline=False)
    return {"body": plan.body, "event": "COMMENT",
            "comments": [{"path": site.path, "line": site.line, "side": "RIGHT",
                          "body": site.body} for site in plan.sites]}


# Reviewable's starter dispositions, weakest first. `informing` publishes
# already resolved; `discussing` resolves once the responder stands down;
# `blocking` clears only when the raiser stands down too. A reply or an
# acknowledgement draws from a different set (satisfied|discussing|blocking|
# working) - `informing` is starter-only - and this module only ever opens
# discussions, so it writes the starter set alone.
DISPOSITION_ORDER = ("informing", "discussing", "blocking")


def finding_disposition(severity, verdict):
    """The Reviewable disposition one finding is raised at.

    REFUTED first, whatever the severity: such a finding reaches a site only
    because --include-refuted re-admitted it, and its header tells the reader
    it is probably not a real problem. It is there to be seen, not to ask
    anything of anybody, and `informing` is the only disposition that leaves
    nothing in anyone's unresolved queue.

    INCONCLUSIVE next, whatever the severity: it asks the reader for a
    judgement they can settle themselves, and `discussing` resolves on their
    answer alone, where `blocking` would leave that answer waiting on the
    raiser to stand down over a question the raiser could not settle.

    Otherwise only `blocker` holds the merge, because `blocking` is the only
    disposition that does, and it is the right price for a blocker and far
    too high for anything below it."""
    if verdict == "REFUTED":
        return "informing"
    if verdict == "INCONCLUSIVE":
        return "discussing"
    return "blocking" if severity == "blocker" else "discussing"


def strongest_disposition(findings):
    """The STRONGEST disposition any of `findings` (SiteFinding entries)
    carries, or the weakest when there are none. One comment has one
    disposition, and taking the strongest is the only rule that cannot
    silently soften a blocker carried with lesser findings."""
    return max((finding_disposition(f.severity, f.verdict) for f in findings),
               key=DISPOSITION_ORDER.index, default=DISPOSITION_ORDER[0])


def site_disposition(site):
    """The disposition a whole site is raised at: the strongest any of its
    findings carries, so a blocker merged with lesser findings on the same
    line still holds the merge. The weakest-when-empty default is defensive:
    `plan_review` builds every site from at least one finding."""
    return strongest_disposition(site.findings)


def render_reviewable(plan):
    """Shape a plan into the Reviewable artifact: a review-level framing
    comment carrying the body, and one discussion per site. Like
    build_payload it decides nothing about placement or wording; the
    disposition is the one thing it adds, because that is Reviewable
    vocabulary derived from the plan's neutral facts.

    The framing comment is raised like a site holding the findings the body
    carries: their strongest disposition, so a blocker whose anchor died still
    holds the merge rather than publishing resolved. With none it is
    `informing` - it carries the review's context, and `informing` publishes
    resolved, so it never waits in anyone's queue for an answer to a
    paragraph that asked nothing.

    A discussion names its `path` and `line` and nothing else about where it
    goes. Reviewable locates a comment by an opaque file key and a revision,
    neither derivable without calling its API, and this module makes no
    network calls so that an artifact can be built and approved offline. The
    posting step resolves both after approval; a key invented here would be
    plausible, wrong, and already approved by a human by the time anything
    noticed.

    The framing comment names the findings it carries in `item_ids`, as a
    discussion does, so the posting step can report each of them with the
    comment's link and account for them after a failed create.

    A line Reviewable cannot take - anything but a positive integer - never
    reaches here: the plan refuses it, before the sites are sorted and before
    approval."""
    return {"provider": "reviewable",
            "framing": {"markdownBody": plan.body,
                        "disposition": strongest_disposition(plan.body_findings),
                        "item_ids": [f.item_id for f in plan.body_findings]},
            "discussions": [{"path": site.path, "line": site.line,
                             "markdownBody": site.body,
                             "disposition": site_disposition(site),
                             "item_ids": list(site.item_ids)} for site in plan.sites]}


def build_reviewable(ledger, tokens, body_intro, include_refuted=None,
                     body_only_verdicts=None):
    """Build the Reviewable artifact for the selected items. The plan is built
    WITH off-diff sites: Reviewable accepts a comment on a line outside the
    diff, surfaces it in the file view and the Discussions tab, and
    auto-expands the collapsed region to show it, so those findings post
    inline rather than being buried in the framing comment, where a reader
    meets them away from the code they are about."""
    return render_reviewable(plan_review(
        ledger, tokens, body_intro, include_refuted=include_refuted,
        body_only_verdicts=body_only_verdicts, off_diff_inline=True))


PROVIDERS = {"github": build_payload, "reviewable": build_reviewable}


@_cli
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

    # Both backends run the same selection, exclusions and accounting; the
    # provider picks only the capability the plan is built with and the shape
    # it is rendered into.
    build = PROVIDERS[getattr(args, "provider", None) or "github"]
    try:
        payload = build(led, args.select or ["all"], intro,
                        include_refuted=getattr(args, "include_refuted", None),
                        body_only_verdicts=getattr(args, "body_only_verdicts", None))
    except (SelectionError, PayloadError, IncludeRefutedError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    json.dump(payload, sys.stdout, indent=2)
    return 0


def named_ids(comment):
    """` - #n, #m` naming the findings a Reviewable comment carries, or
    nothing when it names none."""
    ids = comment.get("item_ids") or []
    return " - " + ", ".join(f"#{i}" for i in ids) if ids else ""


def reviewable_preview_chunks(payload):
    """The Reviewable artifact as `preview_chunks` pieces: a header and framing
    chunk, the framing comment named by the finding ids it carries, then one
    chunk per discussion carrying its path:line, the
    disposition it will be raised at, the finding ids it holds, and its whole
    body.

    Nothing is abbreviated, and on this backend that is not only neatness.
    Reviewable drafts are scoped to the identity that wrote them in both
    directions - the agent's drafts are invisible to the human in Reviewable,
    and the human's to the agent - so reading drafts in Reviewable before
    publishing is not available, and this rendering is the only surface both
    parties can read. The disposition is shown because it is the part of the
    commitment the text does not carry: `blocking` holds the merge until the
    raiser stands down, and this is the only place an approver sees it."""
    discussions = payload.get("discussions", [])
    framing = payload.get("framing") or {}
    rule = "=" * 72
    chunks = ["\n".join([rule, "PR REVIEW PAYLOAD PREVIEW (reviewable)",
                         f"discussions: {len(discussions)}",
                         f"framing disposition: {framing.get('disposition', '')}", rule, "",
                         "REVIEW FRAMING COMMENT" + named_ids(framing), "-" * 72,
                         framing.get("markdownBody", ""), ""])]
    for n, d in enumerate(discussions, start=1):
        head = (f"DISCUSSION {n}/{len(discussions)} - {d.get('path', '')}:{d.get('line', '')} "
                f"[{d.get('disposition', '')}]") + named_ids(d)
        chunks.append("\n".join(["-" * 72, head, "-" * 72, d.get("markdownBody", ""), ""]))
    return chunks


def preview_chunks(payload):
    """Break a review payload dict into the ordered pieces `render_preview`
    concatenates: a header/body chunk, then one chunk per comment. Each chunk is
    self-contained text (no trailing separator between chunks beyond what
    `render_preview` supplies via "\n".join), so pages built from whole chunks by
    `preview_pages` reassemble to exactly `render_preview`'s output.

    A Reviewable artifact names its provider and renders as one; anything else
    is a GitHub review payload. The shape is told apart by that field and not
    by which keys happen to be present, so a half-written artifact is never
    previewed faithfully as the other backend's request."""
    if payload.get("provider") == "reviewable":
        return reviewable_preview_chunks(payload)
    comments = payload.get("comments", [])
    rule = "=" * 72
    chunks = ["\n".join([rule, "PR REVIEW PAYLOAD PREVIEW", f"event: {payload.get('event', '')}",
                         f"comments: {len(comments)}", rule, "", "REVIEW BODY", "-" * 72,
                         payload.get("body", ""), ""])]
    for n, c in enumerate(comments, start=1):
        chunks.append("\n".join(["-" * 72,
                                 f"COMMENT {n}/{len(comments)} - {c.get('path', '')}:"
                                 f"{c.get('line', '')} ({c.get('side', '')})",
                                 "-" * 72, c.get("body", ""), ""]))
    return chunks


def render_preview(payload):
    """Render a complete, non-truncated human-readable rendering of a review
    payload dict (the JSON `payload` writes) - for GitHub, a header with the
    event and comment count, the review body in full, then every comment in
    full with its path:line and entire body text; for Reviewable, the same
    shape in its own fields (see reviewable_preview_chunks). Every text field
    is emitted verbatim (no strip/elide/cap) so the rendering is byte-for-byte
    what `preview`'s caller is about to POST. Plain text and GitHub-flavoured
    markdown only - no raw HTML - since this is printed straight to a
    terminal."""
    return "\n".join(preview_chunks(payload))


PAGE_LIMIT = 15000


def preview_pages(payload, limit=PAGE_LIMIT):
    """Pack whole chunks into pages of at most `limit` bytes. A chunk larger than
    the limit gets a page of its own - a comment is never split, because the
    user must read each one whole before approving it."""
    pages, cur = [], []
    size = lambda parts: len("\n".join(parts).encode("utf-8"))
    for ch in preview_chunks(payload):
        if cur and size(cur + [ch]) > limit:
            pages.append(cur); cur = []
        cur.append(ch)
    if cur:
        pages.append(cur)
    out = []
    for n, parts in enumerate(pages, start=1):
        text = "\n".join(parts)
        head = f"page {n}/{len(pages)}"
        if len(text.encode("utf-8")) > limit:
            head += " (this page exceeds the page limit; shown whole)"
        out.append(head + "\n" + text)
    return out


@_cli
def cmd_brief(args):
    led = lt.load_json(args.ledger)
    ctx = lt.load_json(args.context, what="context")
    ldir = os.path.dirname(os.path.abspath(args.ledger))
    text, meta = lt.render_brief(led, args.key, ctx, ldir)
    bdir = os.path.join(ldir, "briefs")
    md = os.path.join(bdir, f"{args.key}.md")
    try:
        os.makedirs(bdir, exist_ok=True)
        with open(md, "w", encoding="utf-8") as fh:
            fh.write(text)
        with open(os.path.join(bdir, f"{args.key}.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh)
    except OSError as exc:
        raise lt.LedgerError(f"could not write the brief under {bdir}: {exc}")
    print(md)
    return 0


@_cli
def cmd_ingest(args):
    led = lt.load_json(args.ledger)
    ldir = os.path.dirname(os.path.abspath(args.ledger))
    tokens = [t for t in re.split(r"[,\s]+", args.inconclusive or "") if t]
    try:
        ids = [int(t) for t in tokens]
    except ValueError:
        raise lt.LedgerError(f"--inconclusive must be a list of integer ids, "
                             f"got {args.inconclusive!r}")
    notes = lt.ingest(led, os.path.join(ldir, "briefs"), os.path.join(ldir, "verdicts"),
                      lt.template_hash(), inconclusive=ids, reason=args.reason or "")
    lt.write_ledger(args.ledger, led, "ingest")
    print("\n".join(notes))
    return 0


@_cli
def cmd_excerpt(args):
    led = lt.load_json(args.ledger)
    lt.fill_excerpts(led, args.worktree)
    lt.write_ledger(args.ledger, led, "excerpt")
    print(f"excerpted {len(led.get('items', []))} item(s)")
    return 0


@_cli
def cmd_changed_lines(args):
    led = lt.load_json(args.ledger)
    try:
        with open(args.diff, encoding="utf-8", errors="replace") as fh:
            diff_text = fh.read()
    except FileNotFoundError:
        raise lt.LedgerError(f"diff file not found: {args.diff}")
    except OSError as exc:
        raise lt.LedgerError(f"could not read diff file {args.diff}: {exc}")
    notes = lt.mark_changed_lines(led, args.worktree, parse_changed_lines(diff_text),
                                  lt.diff_files(diff_text))
    lt.write_ledger(args.ledger, led, "changed-lines")
    print("\n".join(notes))
    return 0


@_cli
def cmd_renumber(args):
    led = lt.load_json(args.ledger)
    mapping = lt.renumber(led)
    lt.write_ledger(args.ledger, led, "renumber")
    for old in sorted(old for old, new in mapping.items() if old != new):
        print(f"{old} -> {mapping[old]}")
    return 0


@_cli
def cmd_edit(args):
    led = lt.load_json(args.ledger)
    try:
        iid = int(args.id)
    except ValueError:
        raise lt.LedgerError(f"id must be an integer, got {args.id!r}")
    try:
        with open(args.text_file, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        raise lt.LedgerError(f"text file not found: {args.text_file}")
    except UnicodeDecodeError as exc:
        raise lt.LedgerError(f"text file {args.text_file} is not valid UTF-8: {exc}")
    except OSError as exc:
        raise lt.LedgerError(f"could not read text file {args.text_file}: {exc}")
    lt.edit_field(led, iid, args.field, text)
    lt.write_ledger(args.ledger, led, "edit")
    print(f"item {iid} {args.field} updated")
    return 0


@_cli
def cmd_report(args):
    led = lt.load_json(args.ledger)
    text = lt.render_report(led, args.ledger)
    path = os.path.join(os.path.dirname(os.path.abspath(args.ledger)), "report.md")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError as exc:
        print(f"error: could not write report file {path}: {exc}", file=sys.stderr)
        return 1
    print(text, end="")
    return 0


@_cli
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
    # Each backend is checked for the field that carries its comments, so a
    # truncated payload is refused rather than previewed as an empty review.
    if payload.get("provider") == "reviewable":
        if "discussions" not in payload:
            print(f"error: payload file {args.payload} is missing required field 'discussions'",
                  file=sys.stderr)
            return 1
        # The framing comment is part of what is approved: its text and the
        # disposition it is raised at, which can hold the merge.
        framing = payload.get("framing")
        if not (isinstance(framing, dict)
                and all(isinstance(framing.get(k), str) and framing[k]
                        for k in ("markdownBody", "disposition"))):
            print(f"error: payload file {args.payload} is missing required field 'framing' "
                  f"(an object with non-empty markdownBody and disposition)", file=sys.stderr)
            return 1
    elif "comments" not in payload:
        print(f"error: payload file {args.payload} is missing required field 'comments'",
              file=sys.stderr)
        return 1

    full = render_preview(payload)
    txt = os.path.join(os.path.dirname(os.path.abspath(args.payload)), "preview.txt")
    try:
        with open(txt, "w", encoding="utf-8") as fh:
            fh.write(full)
    except OSError as exc:
        print(f"error: could not write preview file {txt}: {exc}", file=sys.stderr)
        return 1

    page = getattr(args, "page", None)
    if page is None:
        print(full)
        return 0
    pages = preview_pages(payload)
    if not 1 <= page <= len(pages):
        print(f"error: page {page} out of range 1..{len(pages)}", file=sys.stderr)
        return 1
    print(pages[page - 1])
    return 0


def main():
    p = argparse.ArgumentParser(prog="pr-review-anchor.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate"); v.add_argument("ledger")
    v.add_argument("--worktree")
    v.add_argument("--draft", action="store_true",
                   help="Check a draft ledger (S4) instead: ids, severity_in, root fields, and "
                        "cluster/item_ids agreement")
    v.set_defaults(fn=cmd_validate)
    r = sub.add_parser("reanchor")
    r.add_argument("ledger"); r.add_argument("--worktree", required=True)
    r.add_argument("--diff", required=True)
    r.add_argument("--head", help="Record this SHA as the ledger's anchored_sha")
    r.add_argument("--dry-run", action="store_true", help="Report status without writing the ledger")
    r.set_defaults(fn=cmd_reanchor)
    y = sub.add_parser("payload")
    y.add_argument("ledger"); y.add_argument("--select", nargs="*")
    y.add_argument("--include-refuted", nargs="*",
                    help="Re-admit specific REFUTED item ids past the default exclusion "
                         "(per id only - never a blanket override)")
    y.add_argument("--body-only-verdicts", nargs="*", choices=sorted(BODY_ONLY_VERDICTS),
                    help="Force these verdicts into the review body even when they anchor "
                         "cleanly. Default: none - placement follows anchoring, and each "
                         "comment carries its verdict in its header")
    y.add_argument("--provider", choices=sorted(PROVIDERS), default="github",
                    help="The backend the artifact is built for. github (default): a reviews API "
                         "body, off-diff findings carried in the review body. reviewable: a "
                         "framing comment plus one discussion per site, off-diff findings inline")
    y.add_argument("--body-file", required=True); y.set_defaults(fn=cmd_payload)
    w = sub.add_parser("preview")
    w.add_argument("payload"); w.add_argument("--page", type=int); w.set_defaults(fn=cmd_preview)
    b = sub.add_parser("brief")
    b.add_argument("ledger"); b.add_argument("--key", required=True)
    b.add_argument("--context", required=True); b.set_defaults(fn=cmd_brief)
    g = sub.add_parser("ingest")
    g.add_argument("ledger"); g.add_argument("--inconclusive"); g.add_argument("--reason")
    g.set_defaults(fn=cmd_ingest)
    e = sub.add_parser("excerpt")
    e.add_argument("ledger"); e.add_argument("--worktree", required=True)
    e.set_defaults(fn=cmd_excerpt)
    c = sub.add_parser("changed-lines")
    c.add_argument("ledger"); c.add_argument("--diff", required=True)
    c.add_argument("--worktree", required=True); c.set_defaults(fn=cmd_changed_lines)
    n = sub.add_parser("renumber")
    n.add_argument("ledger"); n.set_defaults(fn=cmd_renumber)
    ed = sub.add_parser("edit")
    ed.add_argument("ledger"); ed.add_argument("id")
    ed.add_argument("--field", required=True,
                    help="one of: " + ", ".join(lt.EDITABLE_FIELDS)
                    + f" (fix_verdict only to {lt.FIX_VERDICT_DEMOTION})")
    ed.add_argument("--text-file", required=True)
    ed.set_defaults(fn=cmd_edit)
    rp = sub.add_parser("report")
    rp.add_argument("ledger"); rp.set_defaults(fn=cmd_report)
    args = p.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
