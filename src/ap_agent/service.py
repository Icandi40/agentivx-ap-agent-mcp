"""The AP service: everything the agent is allowed to do, in one place.

The MCP server is a thin wrapper over this class. Note what is *not* here: there is
no approve or reject method. Those live only in the review page and the CLI.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from ap_agent.audit import AuditLog
from ap_agent.extract import extract_file, extract_text, pdf_to_text
from ap_agent.ledger.base import Ledger
from ap_agent.matching import (
    PriorDocument,
    priors_from_bills,
    resolve_vendor,
    three_way_match,
)
from ap_agent.models import money, to_jsonable
from ap_agent.proposals import Proposal, ProposalStore

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SEED = REPO_ROOT / "data" / "sample" / "company.json"


@dataclass
class Paths:
    home: Path

    @property
    def ledger(self) -> Path:
        return self.home / "ledger.json"

    @property
    def proposals(self) -> Path:
        return self.home / "proposals.sqlite"

    @property
    def audit(self) -> Path:
        return self.home / "audit.jsonl"

    @property
    def inbox(self) -> Path:
        return self.home / "inbox"


def default_paths() -> Paths:
    return Paths(Path(os.environ.get("AP_AGENT_HOME", REPO_ROOT / ".ap-agent-state")))


def build_ledger(paths: Paths) -> Ledger:
    """Pick the system of record from AP_AGENT_LEDGER: offline (default) or qbo."""
    mode = os.environ.get("AP_AGENT_LEDGER", "offline").lower()
    if mode == "qbo":
        from ap_agent.ledger.qbo import QBOLedger
        from ap_agent.ledger.qbo_auth import TokenStore

        store = TokenStore()
        log = os.environ.get("AP_AGENT_RECEIVING_LOG")
        return QBOLedger(
            realm_id=store.realm_id,
            get_access_token=store.access_token,
            expense_account_id=os.environ["AP_QBO_EXPENSE_ACCOUNT_ID"],
            bank_account_id=os.environ["AP_QBO_BANK_ACCOUNT_ID"],
            receiving_log=Path(log) if log else None,
        )
    from ap_agent.ledger.offline import OfflineLedger

    if not paths.ledger.exists():
        seed = Path(os.environ.get("AP_AGENT_SEED", DEFAULT_SEED))
        return OfflineLedger.from_seed(seed, paths.ledger)
    return OfflineLedger(paths.ledger)


def invoice_ref(text: str) -> str:
    return "INV-" + hashlib.sha256(text.encode()).hexdigest()[:12].upper()


class APService:
    actor = "ap-agent"

    def __init__(self, ledger: Ledger, proposals: ProposalStore, audit: AuditLog, inbox: Path):
        self.ledger = ledger
        self.proposals = proposals
        self.audit = audit
        self.inbox = Path(inbox)
        self.inbox.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_paths(cls, paths: Paths | None = None) -> APService:
        paths = paths or default_paths()
        paths.home.mkdir(parents=True, exist_ok=True)
        audit = AuditLog(paths.audit)
        return cls(build_ledger(paths), ProposalStore(paths.proposals, audit), audit, paths.inbox)

    # Reads from the system of record

    def vendors(self) -> list[dict[str, Any]]:
        out = to_jsonable(self.ledger.list_vendors())
        self.audit.write("ledger_read", self.actor, entity="Vendor", count=len(out))
        return out

    def open_bills(self, vendor_id: str | None = None) -> list[dict[str, Any]]:
        out = to_jsonable(self.ledger.list_bills(vendor_id=vendor_id, open_only=True))
        self.audit.write("ledger_read", self.actor, entity="Bill", count=len(out))
        return out

    def purchase_orders(self, status: str | None = "Open") -> list[dict[str, Any]]:
        pos = self.ledger.list_purchase_orders(status=status)
        out = [{**to_jsonable(p), "total": str(p.total)} for p in pos]
        self.audit.write("ledger_read", self.actor, entity="PurchaseOrder", count=len(out))
        return out

    def purchase_order(self, po_number: str) -> dict[str, Any] | None:
        po = self.ledger.get_purchase_order(po_number)
        if po is None:
            return None
        receipts = self.ledger.list_receipts(po.doc_number)
        self.audit.write("ledger_read", self.actor, entity="PurchaseOrder", po_number=po_number)
        return {**to_jsonable(po), "total": str(po.total), "receipts": to_jsonable(receipts)}

    # Invoice handling

    def read_invoice(self, *, text: str | None = None, filename: str | None = None) -> str:
        if (text is None) == (filename is None):
            raise ValueError("pass exactly one of text or filename")
        if text is not None:
            return text
        path = (self.inbox / str(filename)).resolve()
        if self.inbox.resolve() not in path.parents:
            raise ValueError("filename must be a file inside the invoice inbox")
        if not path.is_file():
            raise FileNotFoundError(f"{filename} is not in the inbox")
        return pdf_to_text(path) if path.suffix.lower() == ".pdf" else path.read_text("utf-8")

    def check_invoice(self, *, text: str | None = None, filename: str | None = None) -> dict:
        started = time.perf_counter()
        raw = self.read_invoice(text=text, filename=filename)
        ref = invoice_ref(raw)
        self.audit.write("invoice_received", self.actor, invoice_ref=ref, source=filename or "text")

        vendors = self.ledger.list_vendors()
        inv = extract_text(raw, [v.display_name for v in vendors])
        self.audit.write(
            "fields_extracted",
            self.actor,
            invoice_ref=ref,
            vendor=inv.vendor_name,
            invoice_number=inv.invoice_number,
            po_number=inv.po_number,
            total=inv.total,
            lines=len(inv.lines),
            warnings=inv.warnings,
        )

        po = self.ledger.get_purchase_order(inv.po_number) if inv.po_number else None
        receipts = self.ledger.list_receipts(po.doc_number) if po else []
        vendor = resolve_vendor(inv.vendor_name, vendors)
        priors = priors_from_bills(self.ledger.list_bills(vendor_id=vendor.id)) if vendor else []
        priors += self._pending_bill_docs()
        result = three_way_match(inv, vendors=vendors, po=po, receipts=receipts, prior_docs=priors)
        elapsed = (time.perf_counter() - started) * 1000
        self.audit.write(
            "match_completed",
            self.actor,
            invoice_ref=ref,
            outcome=result.outcome,
            recommendation=result.recommendation,
            flags=[f.code for f in result.flags],
            elapsed_ms=round(elapsed, 2),
        )
        return {
            "invoice_ref": ref,
            "extracted": to_jsonable(inv),
            "match": to_jsonable(result),
            "elapsed_ms": round(elapsed, 2),
        }

    def _pending_bill_docs(self) -> list[PriorDocument]:
        out = []
        for p in self.proposals.list():
            if p.kind == "create_bill" and p.status in {"pending", "approved"}:
                out.append(
                    PriorDocument(
                        p.payload["vendor_id"],
                        p.payload["doc_number"],
                        Decimal(p.payload["total"]),
                        "proposal",
                    )
                )
        return out

    def propose_bill(self, *, text: str | None = None, filename: str | None = None) -> dict:
        """Check an invoice and draft a bill proposal. Nothing is written to the ledger."""
        checked = self.check_invoice(text=text, filename=filename)
        inv, match = checked["extracted"], checked["match"]
        missing = [
            k
            for k, v in {
                "vendor": match["vendor_id"],
                "invoice_number": inv["invoice_number"],
                "total": inv["total"],
                "line items": inv["lines"],
            }.items()
            if not v
        ]
        if missing:
            self.audit.write(
                "proposal_skipped",
                self.actor,
                invoice_ref=checked["invoice_ref"],
                reason="missing " + ", ".join(missing),
            )
            return {
                **checked,
                "proposal_id": None,
                "status": "needs_human_entry",
                "reason": "Cannot draft a bill without: " + ", ".join(missing),
            }

        vendor = next(v for v in self.ledger.list_vendors() if v.id == match["vendor_id"])
        txn_date = inv["invoice_date"] or date.today().isoformat()
        due = (
            inv["due_date"]
            or (date.fromisoformat(txn_date) + timedelta(days=vendor.terms_days)).isoformat()
        )
        payload = {
            "invoice_ref": checked["invoice_ref"],
            "vendor_id": vendor.id,
            "vendor_name": vendor.display_name,
            "doc_number": inv["invoice_number"],
            "txn_date": txn_date,
            "due_date": due,
            "po_number": match["po_number"],
            "total": inv["total"],
            "lines": inv["lines"],
        }
        p = self.proposals.create(
            "create_bill",
            payload,
            amount=money(inv["total"]),
            recommendation=match["recommendation"],
            flags=match["flags"],
        )
        return {**checked, "proposal_id": p.id, "status": p.status, "approvals_needed": p.needed}

    def propose_payment(self, *, bill_id: str, amount: str, pay_date: str) -> dict:
        bills = {b.id: b for b in self.ledger.list_bills(open_only=True)}
        bill = bills.get(bill_id)
        if bill is None:
            raise ValueError(f"{bill_id} is not an open bill")
        amt = money(amount)
        if amt <= 0 or amt > bill.balance:
            raise ValueError(f"amount must be between 0.01 and the open balance {bill.balance}")
        if date.fromisoformat(pay_date) < date.today():
            raise ValueError("pay_date cannot be in the past")
        payload = {
            "invoice_ref": None,
            "bill_id": bill_id,
            "vendor_id": bill.vendor_id,
            "bill_doc_number": bill.doc_number,
            "amount": str(amt),
            "pay_date": pay_date,
        }
        p = self.proposals.create(
            "schedule_payment", payload, amount=amt, recommendation="approve", flags=[]
        )
        return {"proposal_id": p.id, "status": p.status, "approvals_needed": p.needed}

    def proposal(self, pid: str) -> dict:
        return proposal_view(self.proposals.get(pid))

    def post_approved(self, pid: str) -> dict:
        return proposal_view(self.proposals.execute(pid, self.ledger, actor=self.actor))

    def audit_trail(self, pid: str) -> list[dict]:
        p = self.proposals.get(pid)
        ref = p.payload.get("invoice_ref")
        entries = self.audit.entries(invoice_ref=ref) if ref else []
        seen = {e["hash"] for e in entries}
        entries += [e for e in self.audit.entries(proposal_id=pid) if e["hash"] not in seen]
        return sorted(entries, key=lambda e: e["ts"])


def proposal_view(p: Proposal) -> dict:
    return {
        "id": p.id,
        "kind": p.kind,
        "status": p.status,
        "amount": p.amount,
        "recommendation": p.recommendation,
        "flags": p.flags,
        "approvals": [{"by": a["by"], "at": a["at"], "note": a["note"]} for a in p.approvals],
        "approvals_needed": p.needed,
        "rejection": p.rejection,
        "result": p.result,
        "payload": p.payload,
    }


def extract_path(path: Path, known_vendors: list[str] | None = None) -> dict:
    return to_jsonable(extract_file(path, known_vendors))
