"""Plain data types shared by the ledger adapters, the agent, and the MCP server.

Money is always Decimal, rounded to cents. Nothing here talks to a network.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

CENT = Decimal("0.01")


def money(value: Any) -> Decimal:
    """Convert a number or numeric string to a Decimal rounded to cents."""
    if isinstance(value, Decimal):
        d = value
    else:
        d = Decimal(str(value).replace(",", "").replace("$", "").strip())
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def to_jsonable(obj: Any) -> Any:
    """Recursively turn dataclasses and Decimals into JSON friendly values."""
    if hasattr(obj, "__dataclass_fields__"):
        obj = asdict(obj)
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, Decimal):
        return str(obj)
    return obj


@dataclass
class Vendor:
    id: str
    display_name: str
    email: str = ""
    terms_days: int = 30


@dataclass
class POLine:
    sku: str
    description: str
    qty: Decimal
    unit_price: Decimal

    @property
    def amount(self) -> Decimal:
        return money(self.qty * self.unit_price)


@dataclass
class PurchaseOrder:
    id: str
    doc_number: str
    vendor_id: str
    lines: list[POLine]
    status: str = "Open"
    txn_date: str = ""

    @property
    def total(self) -> Decimal:
        return money(sum((line.amount for line in self.lines), Decimal(0)))


@dataclass
class Receipt:
    """One row of a receiving log: goods that actually arrived against a PO."""

    po_number: str
    sku: str
    qty_received: Decimal
    received_date: str = ""


@dataclass
class BillLine:
    sku: str
    description: str
    qty: Decimal
    unit_price: Decimal
    amount: Decimal


@dataclass
class Bill:
    id: str
    vendor_id: str
    doc_number: str
    txn_date: str
    due_date: str
    total: Decimal
    balance: Decimal
    lines: list[BillLine] = field(default_factory=list)
    po_number: str | None = None
    memo: str = ""


@dataclass
class ScheduledPayment:
    id: str
    bill_id: str
    vendor_id: str
    amount: Decimal
    pay_date: str
    bank_account_ref: str


@dataclass
class InvoiceLine:
    sku: str | None
    description: str
    qty: Decimal
    unit_price: Decimal
    amount: Decimal


@dataclass
class ExtractedInvoice:
    """Fields pulled from an invoice document. Any field can be None if not found."""

    vendor_name: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None
    due_date: str | None = None
    po_number: str | None = None
    total: Decimal | None = None
    lines: list[InvoiceLine] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class Flag:
    code: str
    message: str
    severity: str = "block"  # "block" stops an approve recommendation; "info" does not


@dataclass
class MatchResult:
    outcome: str  # match | mismatch | duplicate | missing_po
    recommendation: str  # approve | hold
    vendor_id: str | None
    po_number: str | None
    flags: list[Flag] = field(default_factory=list)
