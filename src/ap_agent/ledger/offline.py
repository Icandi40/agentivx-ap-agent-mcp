"""Offline stand-in for the QuickBooks Online sandbox.

A single JSON file holds vendors, purchase orders, a receiving log, bills and
scheduled payments. It is what the tests and evals run against, so they need no
credentials and make no network calls. All data in it is synthetic sample data.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from ap_agent.filelock import exclusive
from ap_agent.models import (
    Bill,
    BillLine,
    POLine,
    PurchaseOrder,
    Receipt,
    ScheduledPayment,
    Vendor,
    money,
    po_key,
    to_jsonable,
)


def _po_from(d: dict[str, Any]) -> PurchaseOrder:
    return PurchaseOrder(
        id=d["id"],
        doc_number=d["doc_number"],
        vendor_id=d["vendor_id"],
        status=d.get("status", "Open"),
        txn_date=d.get("txn_date", ""),
        lines=[
            POLine(
                sku=ln["sku"],
                description=ln.get("description", ""),
                qty=Decimal(str(ln["qty"])),
                unit_price=money(ln["unit_price"]),
            )
            for ln in d["lines"]
        ],
    )


def _bill_from(d: dict[str, Any]) -> Bill:
    return Bill(
        id=d["id"],
        vendor_id=d["vendor_id"],
        doc_number=d["doc_number"],
        txn_date=d["txn_date"],
        due_date=d.get("due_date", d["txn_date"]),
        total=money(d["total"]),
        balance=money(d.get("balance", d["total"])),
        po_number=d.get("po_number"),
        memo=d.get("memo", ""),
        lines=[
            BillLine(
                sku=ln["sku"],
                description=ln.get("description", ""),
                qty=Decimal(str(ln["qty"])),
                unit_price=money(ln["unit_price"]),
                amount=money(ln["amount"]),
            )
            for ln in d.get("lines", [])
        ],
    )


class OfflineLedger:
    name = "offline-sandbox"

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(self.path)

    def _read(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
        for key in ("vendors", "purchase_orders", "receipts", "bills", "payments"):
            data.setdefault(key, [])
        return data

    @property
    def _data(self) -> dict[str, Any]:
        """A fresh copy from disk. Other processes (CLI, review page, MCP server) write
        this file too, so nothing is cached between calls."""
        with exclusive(self.path):
            return self._read()

    @classmethod
    def from_seed(cls, seed: Path, state_file: Path) -> OfflineLedger:
        """Copy a seed company file into a working state file and open it."""
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(seed.read_text(encoding="utf-8"), encoding="utf-8")
        return cls(state_file)

    def _save(self, data: dict[str, Any]) -> None:
        """Write atomically. Call only while holding the file lock."""
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # Reads

    def list_vendors(self) -> list[Vendor]:
        return [Vendor(**v) for v in self._data["vendors"]]

    def list_bills(self, vendor_id: str | None = None, open_only: bool = False) -> list[Bill]:
        bills = [_bill_from(b) for b in self._data["bills"]]
        if vendor_id:
            bills = [b for b in bills if b.vendor_id == vendor_id]
        if open_only:
            bills = [b for b in bills if b.balance > 0]
        return bills

    def list_purchase_orders(self, status: str | None = "Open") -> list[PurchaseOrder]:
        pos = [_po_from(p) for p in self._data["purchase_orders"]]
        if status:
            pos = [p for p in pos if p.status.lower() == status.lower()]
        return pos

    def get_purchase_order(self, doc_number: str) -> PurchaseOrder | None:
        for p in self._data["purchase_orders"]:
            if po_key(p["doc_number"]) == po_key(doc_number):
                return _po_from(p)
        return None

    def list_receipts(self, po_number: str) -> list[Receipt]:
        return [
            Receipt(
                po_number=r["po_number"],
                sku=r["sku"],
                qty_received=Decimal(str(r["qty_received"])),
                received_date=r.get("received_date", ""),
            )
            for r in self._data["receipts"]
            if po_key(r["po_number"]) == po_key(po_number)
        ]

    # Writes. Only the approval executor calls these.

    def create_bill(
        self,
        *,
        vendor_id: str,
        doc_number: str,
        txn_date: str,
        due_date: str,
        lines: list[BillLine],
        po_number: str | None,
        memo: str,
    ) -> Bill:
        with exclusive(self.path):
            data = self._read()
            bill_id = f"B{len(data['bills']) + 1:05d}"
            total = money(sum((ln.amount for ln in lines), Decimal(0)))
            bill = Bill(
                id=bill_id,
                vendor_id=vendor_id,
                doc_number=doc_number,
                txn_date=txn_date,
                due_date=due_date,
                total=total,
                balance=total,
                lines=lines,
                po_number=po_number,
                memo=memo,
            )
            data["bills"].append(to_jsonable(bill))
            self._save(data)
            return bill

    def schedule_payment(self, *, bill_id: str, amount: Decimal, pay_date: str) -> ScheduledPayment:
        with exclusive(self.path):
            data = self._read()
            bill = next((b for b in data["bills"] if b["id"] == bill_id), None)
            if bill is None:
                raise ValueError(f"bill {bill_id} not found")
            if money(amount) > money(bill["balance"]):
                raise ValueError("payment exceeds open balance")
            pay = ScheduledPayment(
                id=f"P{len(data['payments']) + 1:05d}",
                bill_id=bill_id,
                vendor_id=bill["vendor_id"],
                amount=money(amount),
                pay_date=pay_date,
                bank_account_ref="sandbox-checking",
            )
            data["payments"].append(to_jsonable(pay))
            bill["balance"] = str(money(bill["balance"]) - money(amount))
            self._save(data)
            return pay
