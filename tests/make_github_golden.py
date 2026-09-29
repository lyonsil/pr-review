"""Write tests/fixtures/github-golden.json: GitHub's payload stdout, stderr and
exit code for one fixed ledger under several argument shapes, as produced by
the pr-review-anchor.py named on the command line. Not run by the test suite.

Regenerate only for a deliberate change to what GitHub publishes, and from the
script that defines it:

    python3 -B tests/make_github_golden.py <pr-review-anchor.py> tests/fixtures/github-golden.json
"""
import json, os, subprocess, sys, tempfile

ATTR = "_(AI-assisted, with my guidance)_"


def item(i, sev, f, line, verdict="CONFIRMED", **kw):
    d = {"id": i, "severity": sev, "severity_in": sev, "file": f, "line": line,
         "line_end": line, "anchor_excerpt": "x", "claim": f"Claim {i} in observable terms.",
         "detail": f"**What happens:** {i} happens.\n\n**Why it matters:** it does.\n\n"
                   f"**Fix:** change {i}.",
         "cluster": None, "verdict": verdict, "verdict_reason": f"Reason {i}.",
         "anchor_status": "ok", "on_changed_line": True}
    d.update(kw)
    return d


# Every GitHub text path plan_review renders: a merged site, a multi-site
# cluster, the same-file line, each verdict label and reason footer, an
# off-diff finding (in and out of the diff's files), a dead anchor, an
# off-diff finding with no line, a REFUTED finding, a token to an id outside
# the review, and an intro carrying {count} and the attribution footer.
LEDGER = {"schema": 2, "items": [
    item(1, "high", "a.ts", 10, cluster="C1",
         detail="**What happens:** see {{#3}}.\n\n**Why it matters:** x.\n\n**Fix:** y."),
    item(2, "medium", "a.ts", 10, "DOWNGRADED", cluster="C1"),
    item(3, "low", "b.ts", 5, "INCONCLUSIVE", cluster="C1"),
    item(4, "blocker", "a.ts", 20, anchor_status="off-diff", on_changed_line=False,
         anchor_note="not on a changed line; cannot be posted inline", file_in_diff=True),
    item(5, "medium", "c.ts", 7, anchor_status="dead",
         anchor_note="anchor excerpt no longer present"),
    item(6, "low", "d.ts", 3, anchor_status="off-diff", on_changed_line=False,
         file_in_diff=False),
    item(7, "high", "a.ts", 30, "REFUTED"),
    item(8, "low", "a.ts", 40, detail="**What happens:** like {{#99}}.\n\n**Fix:** z."),
    item(9, "medium", "e.ts", None, anchor_status="off-diff", on_changed_line=False),
], "clusters": [{"id": "C1", "title": "t", "shared_fix": "s", "item_ids": [1, 2, 3]}]}

BODY = "Reviewed {count} findings.\n\n" + ATTR + "\n"

RUNS = [
    [],
    ["--include-refuted", "7", "--body-only-verdicts", "DOWNGRADED"],
    ["--select", "1", "3", "4", "--include-refuted", "7"],
    ["--select", "7"],
]


def main(script, out):
    with tempfile.TemporaryDirectory() as d:
        lp, bp = os.path.join(d, "ledger.json"), os.path.join(d, "body.md")
        with open(lp, "w", encoding="utf-8") as fh:
            json.dump(LEDGER, fh)
        with open(bp, "w", encoding="utf-8", newline="") as fh:
            fh.write(BODY)
        runs = []
        for extra in RUNS:
            p = subprocess.run([sys.executable, "-B", script, "payload", lp, "--body-file", bp,
                                *extra], capture_output=True)
            runs.append({"args": extra, "code": p.returncode,
                         "stdout": p.stdout.decode("utf-8").replace("\r\n", "\n"),
                         "stderr": p.stderr.decode("utf-8").replace("\r\n", "\n")})
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"ledger": LEDGER, "body": BODY, "runs": runs}, fh, indent=2)
        fh.write("\n")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
