"""QuickBooks Online adapter, locked to the Intuit sandbox.

Uses the QuickBooks Online Accounting API v3 over plain HTTPS (httpx). The base
URL is checked on construction: anything other than the sandbox host is refused,
so this code cannot write to a production company file.

QuickBooks Online has no goods receipt object in its public API, so receipts for
the three-way match come from a receiving log (CSV) that a warehouse or receiving
team keeps. That is how many small businesses on QBO actually work.
"""

from __future__ import annotations

import csv
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from ap_agent.ledger.base import LedgerSafetyError
from ap_agent.models import (
    Bill,
    BillLine,
    POLine,
    PurchaseOrder,
    Receipt,
    ScheduledPayment,
    Vendor,
    money,
)

SANDBOX_HOST = "sandbox-quickbooks.api.intuit.com"
SANDBOX_BASE_URL = f"https://{SANDBOX_HOST}"
MINOR_VERSION = "75"


def assert_sandbox(base_url: str) -> None:
    host = urlparse(base_url).hostname or ""
    if urlparse(base_url).scheme != "https" or host != SANDBOX_HOST:
        raise LedgerSafetyError(
            f"Refusing to use {base_url!r}. This project only talks to {SANDBOX_BASE_URL}."
        )


def _ref(obj: dict[str, Any] | None) -> str:
    return str((obj or {}).get("value", ""))


def vendor_from_qbo(v: dict[str, Any]) -> Vendor:
    terms = v.get("TermRef", {}).get("name", "")
    days = 30
    for token in str(terms).split():
        if token.isdigit():
            days = int(token)
    return Vendor(
        id=str(v["Id"]),
        display_name=v.get("DisplayName", ""),
        email=(v.get("PrimaryEmailAddr") or {}).get("Address", ""),
        terms_days=days,
    )


def po_from_qbo(p: dict[str, Any]) -> PurchaseOrder:
    lines: list[POLine] = []
    for ln in p.get("Line", []):
        detail = ln.get("ItemBasedExpenseLineDetail")
        if detail:
            item = detail.get("ItemRef") or {}
            lines.append(
                POLine(
                    sku=str(item.get("name") or item.get("value") or ""),
                    description=ln.get("Description", ""),
                    qty=Decimal(str(detail.get("Qty", 0))),
                    unit_price=money(detail.get("UnitPrice", 0)),
                )
            )
        elif ln.get("AccountBasedExpenseLineDetail"):
            lines.append(
                POLine(
                    sku=str(ln.get("Description", "")),
                    description=ln.get("Description", ""),
                    qty=Decimal(1),
                    unit_price=money(ln.get("Amount", 0)),
                )
            )
    return PurchaseOrder(
        id=str(p["Id"]),
        doc_number=str(p.get("DocNumber") or p["Id"]),
        vendor_id=_ref(p.get("VendorRef")),
        status=p.get("POStatus", "Open"),
        txn_date=p.get("TxnDate", ""),
        lines=lines,
    )


def bill_from_qbo(b: dict[str, Any]) -> Bill:
    lines: list[BillLine] = []
    for ln in b.get("Line", []):
        if "Amount" in ln and ln.get("DetailType", "").endswith("LineDetail"):
            lines.append(
                BillLine(
                    sku="",
                    description=ln.get("Description", ""),
                    qty=Decimal(1),
                    unit_price=money(ln["Amount"]),
                    amount=money(ln["Amount"]),
                )
            )
    return Bill(
        id=str(b["Id"]),
        vendor_id=_ref(b.get("VendorRef")),
        doc_number=str(b.get("DocNumber", "")),
        txn_date=b.get("TxnDate", ""),
        due_date=b.get("DueDate", b.get("TxnDate", "")),
        total=money(b.get("TotalAmt", 0)),
        balance=money(b.get("Balance", 0)),
        lines=lines,
        memo=b.get("PrivateNote", ""),
    )


def load_receiving_log(path: Path | None) -> list[Receipt]:
    if path is None or not Path(path).exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return [
            Receipt(
                po_number=row["po_number"],
                sku=row["sku"],
                qty_received=Decimal(row["qty_received"]),
                received_date=row.get("received_date", ""),
            )
            for row in csv.DictReader(fh)
        ]


class QBOLedger:
    """System of record adapter for a QuickBooks Online sandbox company."""

    name = "qbo-sandbox"

    def __init__(
        self,
        *,
        realm_id: str,
        get_access_token: Callable[[bool], str],
        expense_account_id: str,
        bank_account_id: str,
        receiving_log: Path | None = None,
        base_url: str = SANDBOX_BASE_URL,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        assert_sandbox(base_url)
        self.realm_id = realm_id
        self._get_token = get_access_token
        self.expense_account_id = expense_account_id
        # A chart of accounts id inside the sandbox company. Never a bank account number.
        self.bank_account_id = bank_account_id
        self.receiving_log = receiving_log
        self._http = httpx.Client(base_url=base_url, transport=transport, timeout=30)

    def _request(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        url = f"/v3/company/{self.realm_id}/{path}"
        params = {"minorversion": MINOR_VERSION, **kw.pop("params", {})}
        for attempt in (0, 1):
            headers = {
                "Authorization": f"Bearer {self._get_token(attempt == 1)}",
                "Accept": "application/json",
            }
            resp = self._http.request(method, url, params=params, headers=headers, **kw)
            if resp.status_code == 401 and attempt == 0:
                continue  # refresh the token once and retry
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("unreachable")

    def _query(self, entity: str, sql: str) -> list[dict[str, Any]]:
        data = self._request("GET", "query", params={"query": sql})
        return data.get("QueryResponse", {}).get(entity, [])

    # Reads

    def list_vendors(self) -> list[Vendor]:
        rows = self._query("Vendor", "select * from Vendor where Active = true maxresults 1000")
        return [vendor_from_qbo(v) for v in rows]

    def list_bills(self, vendor_id: str | None = None, open_only: bool = False) -> list[Bill]:
        clauses = []
        if vendor_id:
            clauses.append(f"VendorRef = '{int(vendor_id)}'")
        if open_only:
            clauses.append("Balance > '0'")
        where = (" where " + " and ".join(clauses)) if clauses else ""
        rows = self._query("Bill", f"select * from Bill{where} maxresults 1000")
        return [bill_from_qbo(b) for b in rows]

    def list_purchase_orders(self, status: str | None = "Open") -> list[PurchaseOrder]:
        rows = self._query("PurchaseOrder", "select * from PurchaseOrder maxresults 1000")
        pos = [po_from_qbo(p) for p in rows]
        if status:
            pos = [p for p in pos if p.status.lower() == status.lower()]
        return pos

    def get_purchase_order(self, doc_number: str) -> PurchaseOrder | None:
        for po in self.list_purchase_orders(status=None):
            if po.doc_number.upper() == doc_number.upper():
                return po
        return None

    def list_receipts(self, po_number: str) -> list[Receipt]:
        return [
            r
            for r in load_receiving_log(self.receiving_log)
            if r.po_number.upper() == po_number.upper()
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
        body = {
            "VendorRef": {"value": vendor_id},
            "DocNumber": doc_number[:21],  # QBO limit
            "TxnDate": txn_date,
            "DueDate": due_date,
            "PrivateNote": (memo + (f" PO {po_number}" if po_number else ""))[:4000],
            "Line": [
                {
                    "DetailType": "AccountBasedExpenseLineDetail",
                    "Amount": float(ln.amount),
                    "Description": f"{ln.sku} {ln.description} x{ln.qty} @ {ln.unit_price}".strip(),
                    "AccountBasedExpenseLineDetail": {
                        "AccountRef": {"value": self.expense_account_id}
                    },
                }
                for ln in lines
            ],
        }
        data = self._request("POST", "bill", json=body)
        bill = bill_from_qbo(data["Bill"])
        bill.po_number = po_number
        return bill

    def schedule_payment(self, *, bill_id: str, amount: Decimal, pay_date: str) -> ScheduledPayment:
        bill = bill_from_qbo(self._request("GET", f"bill/{bill_id}")["Bill"])
        if money(amount) > bill.balance:
            raise ValueError("payment exceeds open balance")
        body = {
            "VendorRef": {"value": bill.vendor_id},
            "PayType": "Check",
            "TotalAmt": float(money(amount)),
            "TxnDate": pay_date,
            "CheckPayment": {"BankAccountRef": {"value": self.bank_account_id}},
            "Line": [
                {
                    "Amount": float(money(amount)),
                    "LinkedTxn": [{"TxnId": bill_id, "TxnType": "Bill"}],
                }
            ],
        }
        data = self._request("POST", "billpayment", json=body)
        bp = data["BillPayment"]
        return ScheduledPayment(
            id=str(bp["Id"]),
            bill_id=bill_id,
            vendor_id=bill.vendor_id,
            amount=money(bp.get("TotalAmt", amount)),
            pay_date=bp.get("TxnDate", pay_date),
            bank_account_ref=self.bank_account_id,
        )
