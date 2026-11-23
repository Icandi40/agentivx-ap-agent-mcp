"""Several processes share the audit log, ledger file and proposal store.

Regression for a bug found while recording a demo: the review page kept its own
in-memory copy of the last audit hash, so after a CLI command appended to the log,
the page's next entry chained to a stale hash and `ap-agent audit` reported
"chain broken". The same stale-copy pattern could also drop ledger writes or let
two processes post one approved proposal twice.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from decimal import Decimal
from pathlib import Path

from conftest import CLEAN_INVOICE

from ap_agent.audit import AuditLog
from ap_agent.ledger.offline import OfflineLedger
from ap_agent.models import BillLine


def _spawn(code: str, *args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(code), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_two_audit_writers_in_one_process_keep_the_chain(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    page, cli = AuditLog(path), AuditLog(path)  # like the review page and a CLI command
    page.write("ledger_read", "ap-agent", entity="PurchaseOrder")
    cli.write("proposal_executed", "ap-agent", proposal_id="PR-1")
    page.write("ledger_read", "ap-agent", entity="PurchaseOrder")  # used to chain to a stale hash
    assert page.verify() == (True, "ok")
    entries = page.entries()
    assert [e["prev"] for e in entries[1:]] == [e["hash"] for e in entries[:-1]]


def test_audit_writers_in_separate_processes_keep_the_chain(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    code = """
        import sys
        from pathlib import Path
        from ap_agent.audit import AuditLog
        log = AuditLog(Path(sys.argv[1]))
        for i in range(40):
            log.write("ledger_read", sys.argv[2], n=i)
    """
    procs = [_spawn(code, str(path), f"writer-{k}") for k in range(4)]
    for p in procs:
        _, err = p.communicate(timeout=120)
        assert p.returncode == 0, err
    log = AuditLog(path)
    assert len(log.entries()) == 160
    assert log.verify() == (True, "ok")


def test_two_ledger_handles_do_not_lose_each_others_bills(tmp_path: Path, company_file: Path):
    state = tmp_path / "ledger.json"
    first = OfflineLedger.from_seed(company_file, state)  # e.g. the MCP server, opened early
    second = OfflineLedger(state)  # e.g. a CLI command
    line = [BillLine("HPF-1042", "Hex bolts", Decimal(1), Decimal("8.40"), Decimal("8.40"))]
    kwargs = dict(txn_date="2026-11-01", due_date="2026-12-01", lines=line, po_number=None, memo="")
    a = second.create_bill(vendor_id="1", doc_number="A-1", **kwargs)
    b = first.create_bill(vendor_id="1", doc_number="B-2", **kwargs)
    docs = {bill.doc_number for bill in first.list_bills()}
    assert {"A-1", "B-2"} <= docs  # the earlier handle did not overwrite the later write
    assert a.id != b.id


def test_two_processes_posting_one_proposal_post_it_once(svc, tmp_path: Path):
    pid = svc.propose_bill(text=CLEAN_INVOICE)["proposal_id"]
    svc.proposals.approve(pid, "Jordan Reviewer")
    home = svc.audit.path.parent
    code = """
        import json, sys
        from pathlib import Path
        from ap_agent.audit import AuditLog
        from ap_agent.ledger.offline import OfflineLedger
        from ap_agent.proposals import ApprovalError, ProposalStore
        home = Path(sys.argv[1])
        audit = AuditLog(home / "audit.jsonl")
        store = ProposalStore(home / "proposals.sqlite", audit)
        try:
            p = store.execute(sys.argv[2], OfflineLedger(home / "ledger.json"))
            print(json.dumps({"status": p.status}))
        except ApprovalError as exc:
            print(json.dumps({"refused": str(exc)}))
    """
    procs = [_spawn(code, str(home), pid) for _ in range(3)]
    results = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err
        results.append(json.loads(out.strip().splitlines()[-1]))
    assert sum(r.get("status") == "executed" for r in results) == 1
    assert sum("refused" in r for r in results) == 2
    assert [b.doc_number for b in svc.ledger.list_bills()].count("INV-777") == 1
    assert svc.audit.verify() == (True, "ok")
