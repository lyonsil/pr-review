#!/usr/bin/env python3
"""Ledger validation, head-drift re-anchoring, and GitHub review payload building
for the PR review pipeline. Standard library only."""
import argparse, json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger_tools as lt  # noqa: E402

# The S5 refuter's four terminal verdicts. Every item must carry one: a
# placeholder like "PENDING" means a refuter's answer was never recorded, and
# the payload builder's exclusion is REFUTED-only, so an unrecorded verdict
# would post as though it had been confirmed. validate() rejects anything
# outside this tuple for exactly that reason.
from ledger_tools import SEVERITIES, VERDICTS, fix_slot, parse_changed_lines  # noqa: E402,F401

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

    if worktree:
        errs.extend(lt.excerpt_errors(ledger, worktree))
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
    errs = validate(led, getattr(args, "worktree", None))
    if errs:
        for e in errs:
            print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"ledger ok: {len(led.get('items', []))} items, {len(led.get('clusters', []))} clusters")
    return 0


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
    span = max((item.get("line_end") or item.get("line") or new_line)
               - (item.get("line") or new_line), 0)
    file_changed = changed.get(item["file"], set())
    target = new_line if new_line in file_changed else min(
        (n for n in file_changed if new_line < n <= new_line + span), default=None)
    if target is None:
        reason = "not on a changed line; cannot be posted inline"
        return "off-diff", new_line, f"{reason} ({note})" if note else reason
    if target == item.get("line"):
        return "ok", target, note
    moved_reason = f"moved {item.get('line')} -> {target}"
    return "moved", target, f"{moved_reason} ({note})" if note else moved_reason


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

    counts = {"ok": 0, "moved": 0, "off-diff": 0, "dead": 0}
    corrected = []
    for it in led.get("items", []):
        status, new_line, why = reanchor_item(it, args.worktree, changed)
        if status in ("ok", "moved") and new_line != it.get("line"):
            span_end = (it.get("line_end") or it.get("line")) - it.get("line") + new_line
            new_end = max(span_end, new_line)
            try:
                excerpt = lt.read_lines(args.worktree, it["file"], new_line, new_end)
            except lt.LedgerError as exc:
                status, why = "dead", str(exc)
            else:
                it["line"], it["line_end"] = new_line, new_end
                it["anchor_excerpt"] = "\n".join(excerpt)
        elif status == "off-diff" and new_line:
            # The matched excerpt is unchanged (it matched verbatim at
            # new_line), so line_end must move with line by the same
            # span-preserving math as the ok/moved branch - leaving it
            # behind silently shrinks the range a later pass searches, and
            # can miss a changed line that is still inside the true span.
            span_end = (it.get("line_end") or it.get("line")) - it.get("line") + new_line
            it["line"], it["line_end"] = new_line, max(span_end, new_line)
        it["anchor_status"], it["anchor_note"] = status, why
        # on_changed_line is a property of the diff. A dead excerpt tells us
        # nothing about the diff, so it leaves the recorded value alone.
        if status != "dead":
            was = it.get("on_changed_line")
            now = status in ("ok", "moved")
            it["on_changed_line"] = now
            if was is not None and bool(was) != now:
                corrected.append((it.get("id"), bool(was), now))
        counts[status] += 1
        print(f"{status:6} #{it.get('id')!s:<3} {it.get('file')}:{new_line or it.get('line')} {why}")

    if getattr(args, "head", None):
        led["anchored_sha"] = args.head
    if getattr(args, "dry_run", False):
        print("dry run: ledger not written")
    else:
        try:
            lt.write_ledger(args.ledger, led, "reanchor")
        except OSError as exc:
            print(f"error: could not write ledger file {args.ledger}: {exc}", file=sys.stderr)
            return 1

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


def build_payload(ledger, tokens, body_intro, include_refuted=None,
                  body_only_verdicts=None):
    """Build the GitHub reviews API body for the selected items, anchored one
    comment per distinct (file, line) site. A cluster whose selected items span
    more than one site gets a 'Similar fix as #a, #b.' cross-reference at each of
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
    exclusion, it is not a second selection mechanism.

    body_only_verdicts optionally forces whole verdicts into the body even when
    they anchor cleanly. It defaults to empty: placement follows anchoring, and
    each comment carries its verdict in its header instead.

    Every `claim`/`detail` that publishes - inline, demoted, or in a
    body-only-verdicts section - has its `{{#n}}` cross-reference tokens
    rendered as plain `#n`; a token naming an id that does not publish
    anywhere in this payload prints a stderr warning rather than posting a
    dead link. `body_intro`'s literal `{count}` is filled with the number of
    ids that publish."""
    selected = resolve_selection(ledger, tokens)
    admitted = resolve_include_refuted(ledger, include_refuted)
    body_only = set(body_only_verdicts or ())
    by_id = {it.get("id"): it for it in ledger.get("items", [])}

    items, demoted, dropped, readmitted, secondary = [], [], [], [], []
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

        if it.get("anchor_status") == "dead":
            demoted.append((it, "dead anchor", it.get("anchor_note")
                             or "the code this finding pointed at has moved or gone "
                                "since the review ran"))
        # The S5 refuter records on_changed_line, and reanchor records
        # anchor_status: "off-diff" when a moved anchor lands outside the
        # diff - either means the finding does not sit on a line this PR
        # touched, and GitHub rejects the ENTIRE review when any one comment
        # anchors off a changed line - demoted for the same reason as a dead
        # anchor. Only an explicit False or "off-diff" demotes: a missing or
        # null on_changed_line means the field was never filled in (a ledger
        # that has not been through S5 or reanchor), which must stay postable
        # rather than silently demoting every item.
        elif it.get("anchor_status") == "off-diff" or it.get("on_changed_line") is False:
            demoted.append((it, "not on a changed line", it.get("anchor_note")
                             or "this finding is not on a line the PR changed, so GitHub "
                                "cannot attach a comment to it"))
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
    if secondary:
        # Kept, like demoted items - but for a verdict reason, not an anchoring
        # one. Say so separately so the operator can tell "the refuter judged
        # this smaller than claimed" from "GitHub would reject this anchor".
        detail = ", ".join(f"#{it['id']} ({verdict})"
                            for it, verdict in sorted(secondary, key=lambda d: d[0]["id"]))
        print(f"notice: {len(secondary)} selected item(s) carried in the review body rather "
              f"than posted inline - {detail}", file=sys.stderr)
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

    if not items and not demoted and not secondary:
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

    # Every id that actually publishes somewhere - inline, demoted, or in a
    # body-only-verdicts section - fills {count} and is what a {{#n}} token is
    # checked against: a token pointing at a REFUTED or otherwise-dropped id
    # is a broken cross-reference even though that id is still in the ledger.
    posted = ({it["id"] for it in items} | {it["id"] for it, _, _ in demoted}
              | {it["id"] for it, _ in secondary})

    outside = []

    def tok(it, text):
        rendered, refs = lt.render_tokens(text)
        outside.extend((it["id"], r) for r in sorted(refs - posted))
        return rendered

    sites = {}
    for it in items:
        sites.setdefault((it["file"], it["line"]), []).append(it)

    cluster_sites = {}
    for it in items:
        if it.get("cluster"):
            cluster_sites.setdefault(it["cluster"], set()).add((it["file"], it["line"]))

    # Which other selected findings land in the same file. This is the payoff
    # for deciding placement by anchoring: a reader in one file sees what else
    # is wrong in it without reading the whole review first.
    file_sites = {}
    for it in items:
        file_sites.setdefault(it["file"], set()).add(it["id"])

    comments = []
    for (path, line), group in sorted(sites.items()):
        blocks = []
        for it in sorted(group, key=lambda x: x["id"]):
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
            block = verdict_reason_block(it)
            if block:
                text += "\n\n" + block
            cl = it.get("cluster")
            if cl and len(cluster_sites.get(cl, ())) > 1:
                others = sorted(o["id"] for o in items
                                if o.get("cluster") == cl and (o["file"], o["line"]) != (path, line))
                if others:
                    text += "\n\nSimilar fix as " + ", ".join(f"#{o}" for o in others) + "."
            same_file = sorted(file_sites.get(path, set()) - {it["id"]})
            if same_file:
                text += ("\n\n_Other findings in this file: "
                         + ", ".join(f"#{o}" for o in same_file) + "_")
            blocks.append(text)
        comments.append({"path": path, "line": line, "side": "RIGHT",
                         "body": "\n\n---\n\n".join(blocks)})

    # The attribution footer must appear exactly once and be the LAST thing
    # in the body, after the demoted-findings section below. A body file the
    # author already wrote to their own standing convention may well already
    # contain it - strip any existing occurrence(s) first so it is never
    # duplicated or left stranded ahead of the demoted section, then
    # re-append it once at the very end.
    body = body_intro.replace("{count}", str(len(posted))).strip()
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
            block = verdict_reason_block(it)
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
            block = verdict_reason_block(it)
            if block:
                lines.append(""); lines.append(block)
            lines.append("")
        section = "\n".join(lines).rstrip()
        body = (body + "\n\n" + section).strip() if body else section

    body = (body + "\n\n" + ATTRIBUTION).strip()

    # A {{#n}} cross-reference to an id that never publishes anywhere in this
    # payload (REFUTED, dropped, or simply not selected) would print as a
    # link to nothing an author can find - warn rather than posting it silently.
    for a, b in sorted(set(outside)):
        print(f"warning: #{a} references #{b}, which is not in this review", file=sys.stderr)

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
                                 include_refuted=getattr(args, "include_refuted", None),
                                 body_only_verdicts=getattr(args, "body_only_verdicts", None))
    except (SelectionError, PayloadError, IncludeRefutedError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    json.dump(payload, sys.stdout, indent=2)
    return 0


def preview_chunks(payload):
    """Break a GitHub review payload dict into the ordered pieces `render_preview`
    concatenates: a header/body chunk, then one chunk per comment. Each chunk is
    self-contained text (no trailing separator between chunks beyond what
    `render_preview` supplies via "\n".join), so pages built from whole chunks by
    `preview_pages` reassemble to exactly `render_preview`'s output."""
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
    """Render a complete, non-truncated human-readable rendering of a GitHub
    review payload dict (the JSON `payload` writes) - a header with the event
    and comment count, the review body in full, then every comment in full
    with its path:line and entire body text. Every text field is emitted
    verbatim (no strip/elide/cap) so the rendering is byte-for-byte what
    `preview`'s caller is about to POST. Plain text and GitHub-flavoured
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
            head += " (one comment that exceeds the page limit; shown whole)"
        out.append(head + "\n" + text)
    return out


def _lt_errors(exc):
    for line in str(exc).splitlines():
        print(f"error: {line}", file=sys.stderr)
    return 1


def cmd_brief(args):
    try:
        led = lt.load_json(args.ledger)
        ctx = lt.load_json(args.context, what="context")
        ldir = os.path.dirname(os.path.abspath(args.ledger))
        text, meta = lt.render_brief(led, args.key, ctx, ldir)
    except lt.LedgerError as exc:
        return _lt_errors(exc)
    bdir = os.path.join(ldir, "briefs"); os.makedirs(bdir, exist_ok=True)
    md = os.path.join(bdir, f"{args.key}.md")
    with open(md, "w", encoding="utf-8") as fh:
        fh.write(text)
    with open(os.path.join(bdir, f"{args.key}.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh)
    print(md)
    return 0


def cmd_ingest(args):
    try:
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
    except lt.LedgerError as exc:
        return _lt_errors(exc)
    print("\n".join(notes))
    return 0


def cmd_excerpt(args):
    try:
        led = lt.load_json(args.ledger)
        lt.fill_excerpts(led, args.worktree)
        lt.write_ledger(args.ledger, led, "excerpt")
    except lt.LedgerError as exc:
        return _lt_errors(exc)
    print(f"excerpted {len(led.get('items', []))} item(s)")
    return 0


def cmd_changed_lines(args):
    try:
        led = lt.load_json(args.ledger)
        try:
            with open(args.diff, encoding="utf-8", errors="replace") as fh:
                diff_text = fh.read()
        except FileNotFoundError:
            raise lt.LedgerError(f"diff file not found: {args.diff}")
        except OSError as exc:
            raise lt.LedgerError(f"could not read diff file {args.diff}: {exc}")
        notes = lt.mark_changed_lines(led, args.worktree, parse_changed_lines(diff_text))
        lt.write_ledger(args.ledger, led, "changed-lines")
    except lt.LedgerError as exc:
        return _lt_errors(exc)
    print("\n".join(notes))
    return 0


def cmd_renumber(args):
    try:
        led = lt.load_json(args.ledger)
        mapping = lt.renumber(led)
        lt.write_ledger(args.ledger, led, "renumber")
    except lt.LedgerError as exc:
        return _lt_errors(exc)
    for old in sorted(old for old, new in mapping.items() if old != new):
        print(f"{old} -> {mapping[old]}")
    return 0


def cmd_edit(args):
    try:
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
    except lt.LedgerError as exc:
        return _lt_errors(exc)
    print(f"item {iid} {args.field} updated")
    return 0


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
    v.add_argument("--worktree"); v.set_defaults(fn=cmd_validate)
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
    ed.add_argument("--field", required=True); ed.add_argument("--text-file", required=True)
    ed.set_defaults(fn=cmd_edit)
    args = p.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
