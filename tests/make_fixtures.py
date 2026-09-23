"""One-off: extract the audited defects from real ledgers into a small fixture.
Kept so the fixture can be regenerated; not run by the test suite."""
import json, sys

SOURCES = {
    # scratchpad ledger path (under /tmp/claude-1000/) : ids to keep
    "-home-lyonsm-scripture-editors/b57ce8d2-d8e1-4483-9eb4-1caca9143247/scratchpad/pr-review/pr13/ledger.json": [4, 14, 19],
    "-home-lyonsm-paranext-core/d2ca2fcf-6e00-4a89-8849-6ad558d74b7a/scratchpad/pr-review/pr2838/ledger.json": [8, 19],
    "-home-lyonsm-paranext-core/6a905e89-f246-4eb4-90d5-709a1a36bcc9/scratchpad/pr-review/pr2653/ledger.json": [11],
}
EXPECT = {  # (source index, id) -> substring the new validate must report
    # pr13 item 4's anchor_excerpt line `...(isStandardView` is TypeScript
    # spread syntax, not a truncation ellipsis - is_ellipsis_line() correctly
    # does not fire on it. The item's actual defect is its `detail`
    # referencing "item #5" from the review batch, which process_vocabulary_in
    # catches. Corrected per Controller Ruling R2 - see the commit message.
    (0, 4): "review-process wording", (0, 14): "hedged", (0, 19): "review-process wording",
    (1, 8): "DOWNGRADED", (1, 19): "REFUTED",
    (2, 11): "ellipsis",
}

out = []
for n, (path, ids) in enumerate(SOURCES.items()):
    led = json.load(open("/tmp/claude-1000/" + path))
    for it in led["items"]:
        if it["id"] in ids:
            out.append({"source": n, "item": it, "expect": EXPECT[(n, it["id"])]})
json.dump(out, open(sys.argv[1], "w"), indent=2)
