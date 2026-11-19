"""The proposal queue and the approval gate.

The agent (and so the MCP server) can only *propose* a write. A proposal is a frozen
payload plus its SHA-256 hash. A person approves or rejects it through the review
page or the CLI, which are separate programs that the MCP server does not expose.
Execution then checks, in this order:

  * the proposal is approved, by enough distinct people, none of them the agent
  * the payload hash still matches what those people approved
  * for a bill: the invoice is still not a duplicate in the ledger (re-checked now)

and only then calls the ledger. The approvers are written into the ledger memo and
the audit log.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from ap_agent.audit import AuditLog
from ap_agent.ledger.base import Ledger
from ap_agent.matching import normalize_doc
from ap_agent.models import BillLine, money, to_jsonable

AGENT_IDENTITIES = {"agent", "ap-agent", "mcp", "assistant", "claude", "system", "bot"}
SECOND_APPROVER_THRESHOLD = Decimal("10000.00")


class ApprovalError(RuntimeError):
    pass


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def required_approvals(amount: Decimal) -> int:
    return 2 if amount >= SECOND_APPROVER_THRESHOLD else 1


@dataclass
class Proposal:
    id: str
    kind: str  # create_bill | schedule_payment
    payload: dict[str, Any]
    payload_hash: str
    status: str  # pending | approved | rejected | executed | failed
    created_by: str
    created_at: str
    recommendation: str
    flags: list[dict[str, Any]]
    amount: str
    approvals: list[dict[str, Any]] = field(default_factory=list)
    rejection: dict[str, Any] | None = None
    executed_at: str | None = None
    result: dict[str, Any] | None = None

    @property
    def needed(self) -> int:
        return required_approvals(Decimal(self.amount))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class ProposalStore:
    def __init__(self, path: Path, audit: AuditLog) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.audit = audit
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.execute(
            """create table if not exists proposals (
                id text primary key, kind text, payload text, payload_hash text,
                status text, created_by text, created_at text, recommendation text,
                flags text, amount text, approvals text, rejection text,
                executed_at text, result text)"""
        )
        self._db.commit()

    # Persistence

    def _row_to(self, row: tuple) -> Proposal:
        (
            pid,
            kind,
            payload,
            phash,
            status,
            cby,
            cat,
            rec,
            flags,
            amount,
            approvals,
            rejection,
            eat,
            result,
        ) = row
        return Proposal(
            id=pid,
            kind=kind,
            payload=json.loads(payload),
            payload_hash=phash,
            status=status,
            created_by=cby,
            created_at=cat,
            recommendation=rec,
            flags=json.loads(flags),
            amount=amount,
            approvals=json.loads(approvals),
            rejection=json.loads(rejection) if rejection else None,
            executed_at=eat,
            result=json.loads(result) if result else None,
        )

    def get(self, pid: str) -> Proposal:
        row = self._db.execute("select * from proposals where id = ?", (pid,)).fetchone()
        if row is None:
            raise KeyError(f"no proposal {pid}")
        return self._row_to(row)

    def list(self, status: str | None = None) -> list[Proposal]:
        if status:
            rows = self._db.execute(
                "select * from proposals where status = ? order by created_at", (status,)
            ).fetchall()
        else:
            rows = self._db.execute("select * from proposals order by created_at").fetchall()
        return [self._row_to(r) for r in rows]

    def _save(self, p: Proposal) -> None:
        self._db.execute(
            "insert or replace into proposals values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                p.id,
                p.kind,
                json.dumps(p.payload),
                p.payload_hash,
                p.status,
                p.created_by,
                p.created_at,
                p.recommendation,
                json.dumps(p.flags),
                p.amount,
                json.dumps(p.approvals),
                json.dumps(p.rejection) if p.rejection else None,
                p.executed_at,
                json.dumps(p.result) if p.result else None,
            ),
        )
        self._db.commit()

    # Creating proposals (the only write path open to the agent)

    def create(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        amount: Decimal,
        recommendation: str,
        flags: list[dict[str, Any]],
        created_by: str = "ap-agent",
    ) -> Proposal:
        if kind not in {"create_bill", "schedule_payment"}:
            raise ValueError(f"unknown proposal kind {kind}")
        payload = to_jsonable(payload)
        p = Proposal(
            id="PR-" + uuid.uuid4().hex[:10].upper(),
            kind=kind,
            payload=payload,
            payload_hash=payload_hash(payload),
            status="pending",
            created_by=created_by,
            created_at=_now(),
            recommendation=recommendation,
            flags=to_jsonable(flags),
            amount=str(money(amount)),
        )
        with self._lock:
            self._save(p)
        self.audit.write(
            "proposal_created",
            created_by,
            proposal_id=p.id,
            kind=kind,
            amount=p.amount,
            recommendation=recommendation,
            flags=[f["code"] for f in p.flags],
            payload_hash=p.payload_hash,
            invoice_ref=payload.get("invoice_ref"),
        )
        return p

    # Human decisions. Called only by the review page and the CLI, never by MCP tools.

    def approve(self, pid: str, approver: str, note: str = "") -> Proposal:
        try:
            return self._approve(pid, approver.strip(), note)
        except ApprovalError as exc:
            self.audit.write(
                "approval_refused", approver.strip() or "(blank)", proposal_id=pid, reason=str(exc)
            )
            raise

    def _approve(self, pid: str, approver: str, note: str) -> Proposal:
        with self._lock:
            p = self.get(pid)
            if p.status != "pending":
                raise ApprovalError(f"{pid} is {p.status}, not pending")
            if not approver:
                raise ApprovalError("approver name is required")
            if approver.lower() in AGENT_IDENTITIES or approver.lower() == p.created_by.lower():
                raise ApprovalError("the agent cannot approve its own proposal")
            if any(a["by"].lower() == approver.lower() for a in p.approvals):
                raise ApprovalError(f"{approver} has already approved {pid}")
            if p.flags and not note.strip():
                raise ApprovalError("this proposal has flags; an override note is required")
            p.approvals.append(
                {"by": approver, "at": _now(), "note": note, "payload_hash": p.payload_hash}
            )
            if len(p.approvals) >= p.needed:
                p.status = "approved"
            self._save(p)
        self.audit.write(
            "proposal_approved",
            approver,
            proposal_id=pid,
            note=note,
            approvals=len(p.approvals),
            needed=p.needed,
            status=p.status,
            invoice_ref=p.payload.get("invoice_ref"),
        )
        return p

    def reject(self, pid: str, reviewer: str, reason: str) -> Proposal:
        reviewer = reviewer.strip()
        if not reviewer or not reason.strip():
            raise ApprovalError("reviewer name and reason are required")
        if reviewer.lower() in AGENT_IDENTITIES:
            raise ApprovalError("the agent cannot reject proposals")
        with self._lock:
            p = self.get(pid)
            if p.status not in {"pending"}:
                raise ApprovalError(f"{pid} is {p.status}, not pending")
            p.status = "rejected"
            p.rejection = {"by": reviewer, "at": _now(), "reason": reason}
            self._save(p)
        self.audit.write(
            "proposal_rejected",
            reviewer,
            proposal_id=pid,
            reason=reason,
            invoice_ref=p.payload.get("invoice_ref"),
        )
        return p

    # Execution

    def execute(self, pid: str, ledger: Ledger, actor: str = "ap-agent") -> Proposal:
        with self._lock:
            p = self.get(pid)
            try:
                self._check_executable(p, ledger)
            except ApprovalError as exc:
                self.audit.write(
                    "execution_refused",
                    actor,
                    proposal_id=pid,
                    reason=str(exc),
                    invoice_ref=p.payload.get("invoice_ref"),
                )
                raise
            approvers = ", ".join(a["by"] for a in p.approvals)
            try:
                if p.kind == "create_bill":
                    pl = p.payload
                    bill = ledger.create_bill(
                        vendor_id=pl["vendor_id"],
                        doc_number=pl["doc_number"],
                        txn_date=pl["txn_date"],
                        due_date=pl["due_date"],
                        po_number=pl.get("po_number"),
                        memo=f"AP agent {pid}. Approved by {approvers}.",
                        lines=[
                            BillLine(
                                sku=ln["sku"] or "",
                                description=ln["description"],
                                qty=Decimal(ln["qty"]),
                                unit_price=money(ln["unit_price"]),
                                amount=money(ln["amount"]),
                            )
                            for ln in pl["lines"]
                        ],
                    )
                    p.result = to_jsonable({"bill_id": bill.id, "total": bill.total})
                else:
                    pay = ledger.schedule_payment(
                        bill_id=p.payload["bill_id"],
                        amount=money(p.payload["amount"]),
                        pay_date=p.payload["pay_date"],
                    )
                    p.result = to_jsonable({"payment_id": pay.id, "pay_date": pay.pay_date})
                p.status = "executed"
            except Exception as exc:
                p.status = "failed"
                p.result = {"error": str(exc)}
            p.executed_at = _now()
            self._save(p)
        self.audit.write(
            "proposal_executed" if p.status == "executed" else "execution_failed",
            actor,
            proposal_id=pid,
            ledger=ledger.name,
            approved_by=[a["by"] for a in p.approvals],
            result=p.result,
            invoice_ref=p.payload.get("invoice_ref"),
        )
        return p

    def _check_executable(self, p: Proposal, ledger: Ledger) -> None:
        if p.status != "approved":
            raise ApprovalError(f"{p.id} is {p.status}; a person must approve it first")
        humans = {a["by"].lower() for a in p.approvals} - AGENT_IDENTITIES
        if len(humans) < p.needed:
            raise ApprovalError(f"{p.id} needs {p.needed} distinct human approvals")
        if payload_hash(p.payload) != p.payload_hash or any(
            a["payload_hash"] != p.payload_hash for a in p.approvals
        ):
            raise ApprovalError(f"{p.id} payload changed after approval")
        if p.kind == "create_bill":
            key = normalize_doc(p.payload["doc_number"])
            for bill in ledger.list_bills(vendor_id=p.payload["vendor_id"]):
                if normalize_doc(bill.doc_number) == key:
                    raise ApprovalError(
                        f"{p.id} would duplicate bill {bill.id} already in {ledger.name}"
                    )
        if p.kind == "schedule_payment" and p.payload["pay_date"] < date.today().isoformat():
            raise ApprovalError(f"{p.id} pay date is in the past")
