import json
from datetime import date, timedelta

import pytest
from conftest import BIG_INVOICE, CLEAN_INVOICE

from ap_agent.proposals import ApprovalError


def _bills(svc):
    return svc.ledger.list_bills()


def test_proposal_writes_nothing_to_ledger(svc):
    before = len(_bills(svc))
    out = svc.propose_bill(text=CLEAN_INVOICE)
    assert out["status"] == "pending"
    assert len(_bills(svc)) == before


def test_unapproved_proposal_cannot_be_posted(svc):
    pid = svc.propose_bill(text=CLEAN_INVOICE)["proposal_id"]
    with pytest.raises(ApprovalError):
        svc.post_approved(pid)
    assert svc.audit.entries(proposal_id=pid)[-1]["event"] == "execution_refused"


@pytest.mark.parametrize("name", ["ap-agent", "Agent", "claude", " mcp "])
def test_agent_cannot_approve(svc, name):
    pid = svc.propose_bill(text=CLEAN_INVOICE)["proposal_id"]
    with pytest.raises(ApprovalError):
        svc.proposals.approve(pid, name)


def test_approve_then_post_records_approver(svc):
    pid = svc.propose_bill(text=CLEAN_INVOICE)["proposal_id"]
    svc.proposals.approve(pid, "Jordan Reviewer")
    out = svc.post_approved(pid)
    assert out["status"] == "executed"
    bill = next(b for b in _bills(svc) if b.id == out["result"]["bill_id"])
    assert "Jordan Reviewer" in bill.memo
    executed = [e for e in svc.audit.entries(proposal_id=pid) if e["event"] == "proposal_executed"]
    assert executed[0]["details"]["approved_by"] == ["Jordan Reviewer"]


def test_post_twice_is_refused(svc):
    pid = svc.propose_bill(text=CLEAN_INVOICE)["proposal_id"]
    svc.proposals.approve(pid, "Jordan Reviewer")
    svc.post_approved(pid)
    with pytest.raises(ApprovalError):
        svc.post_approved(pid)


def test_flagged_proposal_needs_override_note(svc):
    text = CLEAN_INVOICE.replace("Total Due: $144.00", "Total Due: $244.00")
    pid = svc.propose_bill(text=text)["proposal_id"]
    with pytest.raises(ApprovalError, match="override note"):
        svc.proposals.approve(pid, "Jordan Reviewer")
    p = svc.proposals.approve(pid, "Jordan Reviewer", "vendor confirmed freight charge by phone")
    assert p.status == "approved"


def test_large_bill_needs_two_distinct_people(svc):
    out = svc.propose_bill(text=BIG_INVOICE)
    pid = out["proposal_id"]
    assert out["approvals_needed"] == 2
    p = svc.proposals.approve(pid, "Jordan Reviewer")
    assert p.status == "pending"
    with pytest.raises(ApprovalError):
        svc.proposals.approve(pid, "jordan reviewer")  # same person, different case
    with pytest.raises(ApprovalError):
        svc.post_approved(pid)
    assert svc.proposals.approve(pid, "Avery Controller").status == "approved"
    assert svc.post_approved(pid)["status"] == "executed"


def test_payload_tampering_after_approval_is_refused(svc):
    pid = svc.propose_bill(text=CLEAN_INVOICE)["proposal_id"]
    svc.proposals.approve(pid, "Jordan Reviewer")
    p = svc.proposals.get(pid)
    p.payload["lines"][0]["amount"] = "8400.00"  # someone edits the stored draft
    svc.proposals._db.execute(
        "update proposals set payload = ? where id = ?", (json.dumps(p.payload), pid)
    )
    with pytest.raises(ApprovalError, match="changed after approval"):
        svc.post_approved(pid)


def test_duplicate_rechecked_at_posting(svc):
    a = svc.propose_bill(text=CLEAN_INVOICE)["proposal_id"]
    b = svc.propose_bill(text=CLEAN_INVOICE + "\nresent")["proposal_id"]
    svc.proposals.approve(a, "Jordan Reviewer")
    svc.post_approved(a)
    svc.proposals.approve(b, "Jordan Reviewer", "override by mistake")
    with pytest.raises(ApprovalError, match="duplicate"):
        svc.post_approved(b)


def test_rejected_proposal_cannot_be_posted(svc):
    pid = svc.propose_bill(text=CLEAN_INVOICE)["proposal_id"]
    svc.proposals.reject(pid, "Jordan Reviewer", "not our order")
    with pytest.raises(ApprovalError):
        svc.post_approved(pid)
    with pytest.raises(ApprovalError):
        svc.proposals.approve(pid, "Avery Controller")


def test_payment_proposal_flow(svc):
    pid = svc.propose_bill(text=CLEAN_INVOICE)["proposal_id"]
    svc.proposals.approve(pid, "Jordan Reviewer")
    bill_id = svc.post_approved(pid)["result"]["bill_id"]
    later = (date.today() + timedelta(days=10)).isoformat()
    with pytest.raises(ValueError):
        svc.propose_payment(bill_id=bill_id, amount="999.00", pay_date=later)
    with pytest.raises(ValueError):
        svc.propose_payment(bill_id=bill_id, amount="144.00", pay_date="2020-01-01")
    pay = svc.propose_payment(bill_id=bill_id, amount="144.00", pay_date=later)
    with pytest.raises(ApprovalError):
        svc.post_approved(pay["proposal_id"])
    svc.proposals.approve(pay["proposal_id"], "Avery Controller")
    out = svc.post_approved(pay["proposal_id"])
    assert out["status"] == "executed"
    assert next(b for b in _bills(svc) if b.id == bill_id).balance == 0


def test_incomplete_invoice_gets_no_proposal(svc):
    out = svc.propose_bill(text="Harbor Point Fasteners LLC\nplease pay soon")
    assert out["proposal_id"] is None and out["status"] == "needs_human_entry"


def test_inbox_path_traversal_blocked(svc):
    with pytest.raises(ValueError):
        svc.check_invoice(filename="../ledger.json")


def test_audit_chain_detects_edits(svc):
    svc.propose_bill(text=CLEAN_INVOICE)
    assert svc.audit.verify() == (True, "ok")
    lines = svc.audit.path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[1])
    entry["actor"] = "someone-else"
    lines[1] = json.dumps(entry)
    svc.audit.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, msg = svc.audit.verify()
    assert not ok and "line 2" in msg
