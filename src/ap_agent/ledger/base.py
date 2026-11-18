"""The interface every system of record adapter implements.

Two adapters ship: an offline stand-in (a JSON file shaped like a small QuickBooks
company) and a QuickBooks Online sandbox adapter. The agent, the MCP server and the
evals only ever see this interface.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from ap_agent.models import Bill, BillLine, PurchaseOrder, Receipt, ScheduledPayment, Vendor


class Ledger(Protocol):
    name: str

    def list_vendors(self) -> list[Vendor]: ...

    def list_bills(self, vendor_id: str | None = None, open_only: bool = False) -> list[Bill]: ...

    def list_purchase_orders(self, status: str | None = "Open") -> list[PurchaseOrder]: ...

    def get_purchase_order(self, doc_number: str) -> PurchaseOrder | None: ...

    def list_receipts(self, po_number: str) -> list[Receipt]: ...

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
    ) -> Bill: ...

    def schedule_payment(
        self, *, bill_id: str, amount: Decimal, pay_date: str
    ) -> ScheduledPayment: ...


class LedgerSafetyError(RuntimeError):
    """Raised when an adapter is pointed at anything other than a sandbox."""
