"""Run the AP agent over the synthetic invoice sets and write EVALS.md.

For each split: load the split's sample company into a fresh offline ledger, push
every invoice through the same service the MCP server uses, score the result
against the labels, then exercise the approval gate. Every number in EVALS.md is
computed here; nothing is typed in by hand.

Run: uv run python evals/run_evals.py
"""

from __future__ import annotations

import json
import platform
import shutil
import statistics
import sys
import tempfile
import time
from collections import Counter
from datetime import date
from pathlib import Path

from ap_agent.audit import AuditLog
from ap_agent.ledger.offline import OfflineLedger
from ap_agent.proposals import ApprovalError, ProposalStore
from ap_agent.service import APService
from ap_agent.xray import estimate, load_baseline, render_markdown

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evals" / "dataset"
OUTCOMES = ["match", "mismatch", "duplicate", "missing_po"]
FIELDS = ["vendor_name", "invoice_number", "invoice_date", "po_number", "total", "lines"]
REVIEWER_A, REVIEWER_B = "Sample Reviewer A", "Sample Reviewer B"


def _norm_lines(lines: list) -> list:
    return sorted(
        [
            ln["sku"],
            str(int(float(ln["qty"]))) if float(ln["qty"]).is_integer() else ln["qty"],
            ln["unit_price"],
        ]
        for ln in lines
    )


def field_hits(extracted: dict, expected: dict) -> dict[str, bool]:
    return {
        "vendor_name": extracted["vendor_name"] == expected["vendor_name"],
        "invoice_number": (extracted["invoice_number"] or "") == expected["invoice_number"],
        "invoice_date": extracted["invoice_date"] == expected["invoice_date"],
        "po_number": extracted["po_number"] == expected["po_number"],
        "total": extracted["total"] == expected["total"],
        "lines": _norm_lines(extracted["lines"]) == sorted(expected["lines"]),
    }


def run_split(split: str) -> dict:
    d = DATASET / split
    labels = json.loads((d / "labels.json").read_text(encoding="utf-8"))
    tmp = Path(tempfile.mkdtemp(prefix=f"ap-eval-{split}-"))
    try:
        audit = AuditLog(tmp / "audit.jsonl")
        ledger = OfflineLedger.from_seed(d / "company.json", tmp / "ledger.json")
        svc = APService(
            ledger, ProposalStore(tmp / "proposals.sqlite", audit), audit, tmp / "inbox"
        )
        bills_before = len(ledger.list_bills())

        rows = []
        for lab in labels:
            shutil.copyfile(d / "invoices" / lab["file"], svc.inbox / lab["file"])

            t0 = time.perf_counter()
            out = svc.propose_bill(filename=lab["file"])
            ms = (time.perf_counter() - t0) * 1000
            rows.append(
                {
                    "label": lab,
                    "hits": field_hits(out["extracted"], lab["expected"]),
                    "outcome": out["match"]["outcome"],
                    "recommendation": out["match"]["recommendation"],
                    "flags": [f["code"] for f in out["match"]["flags"]],
                    "proposal_id": out["proposal_id"],
                    "ms": ms,
                }
            )

        safety = gate_checks(svc, rows)
        safety["bills_posted"] = len(ledger.list_bills()) - bills_before
        _ok, msg = audit.verify()
        safety["audit_chain"] = msg
        safety["audit_entries"] = len(audit.entries())
        return {"split": split, "rows": rows, "safety": safety}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def gate_checks(svc: APService, rows: list[dict]) -> dict:
    """Exercise the approval gate the way an attacker or a careless agent would."""
    s = Counter()
    proposals = [r for r in rows if r["proposal_id"]]

    # 1. The agent tries to post every proposal before anyone approved it.
    for r in proposals:
        s["unapproved_post_attempts"] += 1
        try:
            svc.post_approved(r["proposal_id"])
        except ApprovalError:
            s["unapproved_posts_refused"] += 1

    # 2. The agent tries to approve its own proposals.
    for r in proposals:
        s["self_approval_attempts"] += 1
        try:
            svc.proposals.approve(r["proposal_id"], "ap-agent", "looks fine")
        except ApprovalError:
            s["self_approvals_refused"] += 1

    # 3. A simulated reviewer approves only what the agent recommended, then the agent posts.
    for r in proposals:
        if r["recommendation"] != "approve":
            continue
        pid = r["proposal_id"]
        p = svc.proposals.approve(pid, REVIEWER_A)
        if p.status != "approved":
            p = svc.proposals.approve(pid, REVIEWER_B)
            s["second_approver_needed"] += 1
        result = svc.post_approved(pid)
        s["approved_and_posted" if result["status"] == "executed" else "post_failed"] += 1
        try:
            svc.post_approved(pid)
        except ApprovalError:
            s["repost_refused"] += 1

    # 4. A reviewer wrongly overrides every duplicate flag. Execution re-checks the ledger.
    for r in proposals:
        if "DUPLICATE" not in r["flags"]:
            continue
        pid = r["proposal_id"]
        s["duplicate_overrides"] += 1
        p = svc.proposals.approve(pid, REVIEWER_A, "override for test")
        if p.status != "approved":
            svc.proposals.approve(pid, REVIEWER_B, "override for test")
        try:
            out = svc.post_approved(pid)
            if out["status"] == "executed":
                s["duplicate_override_posted"] += 1
        except ApprovalError:
            s["duplicate_override_refused"] += 1
    return dict(s)


def pct(n: int, d: int) -> str:
    return f"{n / d:.1%}" if d else "n/a"


def summarize(res: dict) -> dict:
    rows = res["rows"]
    n = len(rows)
    field_acc = {f: sum(r["hits"][f] for r in rows) for f in FIELDS}
    all_fields = sum(all(r["hits"].values()) for r in rows)
    correct = sum(r["outcome"] == r["label"]["expected_outcome"] for r in rows)
    bad = [r for r in rows if r["label"]["expected_outcome"] != "match"]
    good = [r for r in rows if r["label"]["expected_outcome"] == "match"]
    prevented = sum(r["recommendation"] == "hold" for r in bad)
    held_clean = sum(r["recommendation"] == "hold" for r in good)
    times = sorted(r["ms"] for r in rows)
    p95 = times[min(len(times) - 1, round(0.95 * (len(times) - 1)))]
    return {
        "n": n,
        "field_acc": field_acc,
        "all_fields": all_fields,
        "correct": correct,
        "bad": len(bad),
        "prevented": prevented,
        "good": len(good),
        "held_clean": held_clean,
        "median_ms": statistics.median(times),
        "p95_ms": p95,
        "max_ms": times[-1],
        "hold_rate": sum(r["recommendation"] == "hold" for r in rows) / n,
    }


def render(test: dict, dev: dict) -> str:
    t, dv = summarize(test), summarize(dev)
    rows, safety = test["rows"], test["safety"]
    conf = Counter((r["label"]["expected_outcome"], r["outcome"]) for r in rows)
    conf_md = "| Expected \\ Agent said | " + " | ".join(OUTCOMES) + " |\n|---|" + "---|" * 4 + "\n"
    for exp in OUTCOMES:
        conf_md += f"| {exp} | " + " | ".join(str(conf[(exp, got)]) for got in OUTCOMES) + " |\n"
    per_class = "\n".join(
        f"| {o} | {sum(1 for r in rows if r['label']['expected_outcome'] == o)} | "
        f"{pct(conf[(o, o)], sum(1 for r in rows if r['label']['expected_outcome'] == o))} |"
        for o in OUTCOMES
    )
    fields_md = "\n".join(
        f"| {f.replace('_', ' ')} | {t['field_acc'][f]}/{t['n']} | {pct(t['field_acc'][f], t['n'])} |"
        for f in FIELDS
    )
    by_format = Counter()
    by_format_ok = Counter()
    for r in rows:
        key = f"{r['label']['template']} ({r['label']['format']})"
        by_format[key] += 1
        by_format_ok[key] += all(r["hits"].values())
    fmt_md = "\n".join(
        f"| {k} | {by_format[k]} | {pct(by_format_ok[k], by_format[k])} |"
        for k in sorted(by_format)
    )
    misses = [
        r
        for r in rows
        if r["outcome"] != r["label"]["expected_outcome"] or not all(r["hits"].values())
    ]
    miss_md = (
        "\n".join(
            f"| {r['label']['case_id']} | {r['label']['template']} ({r['label']['format']}) | "
            f"{r['label']['expected_outcome']} ({r['label']['variant']}) | {r['outcome']} | "
            f"{', '.join(f for f, ok in r['hits'].items() if not ok) or 'none'} |"
            for r in misses
        )
        or "| none | | | | |"
    )
    xbase = load_baseline(ROOT / "data" / "sample" / "xray_baseline.json")
    xray = render_markdown(estimate(xbase, t["hold_rate"], t["median_ms"] / 1000), xbase)

    return f"""# EVALS

Generated by `uv run python evals/run_evals.py` on {date.today().isoformat()}. Do not edit by hand.

All invoices, vendors, purchase orders and receipts are **synthetic sample data** made by
`evals/generate.py`. These are not client results. The headline set is the held-out
**test** split ({t["n"]} invoices); the **dev** split ({dv["n"]} invoices) was used while
writing the extractor and matcher.

## Headline (test split, {t["n"]} invoices)

| Metric | Result |
|---|---|
| Extraction accuracy, all six fields right on an invoice | {t["all_fields"]}/{t["n"]} ({pct(t["all_fields"], t["n"])}) |
| Extraction accuracy, field level | {sum(t["field_acc"].values())}/{t["n"] * len(FIELDS)} ({pct(sum(t["field_acc"].values()), t["n"] * len(FIELDS))}) |
| Correct-flag rate (agent outcome equals the label) | {t["correct"]}/{t["n"]} ({pct(t["correct"], t["n"])}) |
| False approvals prevented (bad invoices the agent held) | {t["prevented"]}/{t["bad"]} ({pct(t["prevented"], t["bad"])}) |
| Bad invoices the agent recommended approving | {t["bad"] - t["prevented"]} |
| Clean invoices held for a person anyway | {t["held_clean"]}/{t["good"]} |
| Time per invoice, median / p95 / max | {t["median_ms"]:.1f} ms / {t["p95_ms"]:.1f} ms / {t["max_ms"]:.1f} ms |

Dev split for comparison: all fields right on {dv["all_fields"]}/{dv["n"]}, correct-flag rate
{dv["correct"]}/{dv["n"]}, false approvals prevented {dv["prevented"]}/{dv["bad"]}.

Time per invoice is wall-clock time for extraction, ledger reads, the three-way match and
filing the proposal, on the offline ledger, on one Windows laptop
(Python {platform.python_version()}). It excludes the human review. Against the live
QuickBooks sandbox, network calls would add to it.

"Bad invoice" means any label other than match: mismatch, duplicate or missing PO. A
rubber-stamp process that approves everything would let all {t["bad"]} through.

## Outcome by class

| Expected outcome | Invoices | Agent got it right |
|---|---|---|
{per_class}

{conf_md}
## Extraction by field

| Field | Correct | Rate |
|---|---|---|
{fields_md}

| Template (format) | Invoices | All fields right |
|---|---|---|
{fmt_md}

## Approval gate checks (test split)

These run after the batch. They check that the gate holds even when the agent, or a
careless reviewer, tries to get around it.

| Check | Result |
|---|---|
| Agent posts a proposal nobody approved | refused {safety.get("unapproved_posts_refused", 0)} of {safety.get("unapproved_post_attempts", 0)} |
| Agent approves its own proposal | refused {safety.get("self_approvals_refused", 0)} of {safety.get("self_approval_attempts", 0)} |
| Reviewer approves the agent's clean recommendations, agent posts them | posted {safety.get("approved_and_posted", 0)}, failed {safety.get("post_failed", 0)} |
| Proposals at or over $10,000 that needed a second approver | {safety.get("second_approver_needed", 0)} |
| Agent posts the same approved proposal twice | second post refused {safety.get("repost_refused", 0)} times |
| Reviewer wrongly overrides a duplicate flag | {safety.get("duplicate_overrides", 0)} overrides: {safety.get("duplicate_override_refused", 0)} refused at posting, {safety.get("duplicate_override_posted", 0)} posted |
| Bills written to the ledger in total | {safety.get("bills_posted", 0)} |
| Audit log | {safety.get("audit_entries", 0)} entries, hash chain {safety.get("audit_chain")} |

A duplicate override can still post when the earlier copy never reached the ledger (for
example, it was held and not approved). The posting re-check only sees bills that exist.

## Misses (test split)

Every invoice where the outcome or any extracted field was wrong.

| Case | Template | Expected | Agent said | Wrong fields |
|---|---|---|---|---|
{miss_md}

## Process X-Ray estimate (sample data)

{xray}"""


def main() -> int:
    for split in ("dev", "test"):
        if not (DATASET / split / "labels.json").exists():
            print("Dataset missing. Run: uv run python evals/generate.py", file=sys.stderr)
            return 1
    dev = run_split("dev")
    test = run_split("test")
    md = render(test, dev)
    (ROOT / "EVALS.md").write_text(md, encoding="utf-8")
    t = summarize(test)
    print(
        f"test: fields {t['all_fields']}/{t['n']}, flags {t['correct']}/{t['n']}, "
        f"prevented {t['prevented']}/{t['bad']}, median {t['median_ms']:.1f} ms -> EVALS.md"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
