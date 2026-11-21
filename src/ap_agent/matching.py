"""Three-way match: invoice vs purchase order vs receiving log.

Order of checks, which also decides the single outcome label:
1. duplicate   the vendor already has a bill (or a pending proposal) with this invoice number
2. missing_po  no PO reference, or the reference is not a PO in the system of record
3. mismatch    any blocking flag from the line by line comparison
4. match       everything agrees within tolerance

Only "match" earns an approve recommendation. Every other outcome is held for a
person, with the reasons written out as flags.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from ap_agent.extract import name_similarity
from ap_agent.models import (
    Bill,
    ExtractedInvoice,
    Flag,
    MatchResult,
    PurchaseOrder,
    Receipt,
    Vendor,
    money,
)

PRICE_TOLERANCE = Decimal("0.01")  # per unit, in dollars
TOTAL_TOLERANCE = Decimal("0.01")
VENDOR_MATCH_THRESHOLD = 0.85


@dataclass
class PriorDocument:
    """A bill already in the ledger, or a bill proposal still waiting for a decision."""

    vendor_id: str
    doc_number: str
    total: Decimal
    source: str  # "ledger" or "proposal"


def normalize_doc(doc: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (doc or "").upper()).lstrip("0")


def resolve_vendor(name: str | None, vendors: list[Vendor]) -> Vendor | None:
    if not name:
        return None
    best, best_score = None, 0.0
    for v in vendors:
        score = name_similarity(name, v.display_name)
        if score > best_score:
            best, best_score = v, score
    return best if best_score >= VENDOR_MATCH_THRESHOLD else None


def priors_from_bills(bills: list[Bill]) -> list[PriorDocument]:
    return [PriorDocument(b.vendor_id, b.doc_number, b.total, "ledger") for b in bills]


def three_way_match(
    inv: ExtractedInvoice,
    *,
    vendors: list[Vendor],
    po: PurchaseOrder | None,
    receipts: list[Receipt],
    prior_docs: list[PriorDocument],
) -> MatchResult:
    flags: list[Flag] = []
    vendor = resolve_vendor(inv.vendor_name, vendors)
    vendor_id = vendor.id if vendor else None

    if vendor is None:
        flags.append(
            Flag("UNKNOWN_VENDOR", f"Vendor {inv.vendor_name!r} is not in the vendor list")
        )
    if inv.invoice_number is None:
        flags.append(Flag("NO_INVOICE_NUMBER", "Invoice number could not be read"))
    if inv.total is None:
        flags.append(Flag("NO_TOTAL", "Invoice total could not be read"))

    # 1. Duplicate
    if vendor_id and inv.invoice_number:
        key = normalize_doc(inv.invoice_number)
        for prior in prior_docs:
            if prior.vendor_id == vendor_id and normalize_doc(prior.doc_number) == key:
                where = "an existing bill" if prior.source == "ledger" else "a pending proposal"
                flags.append(
                    Flag(
                        "DUPLICATE", f"Invoice {inv.invoice_number} matches {where} for this vendor"
                    )
                )
                return MatchResult("duplicate", "hold", vendor_id, inv.po_number, flags)

    # 2. Missing PO
    if not inv.po_number:
        flags.append(Flag("MISSING_PO", "Invoice does not reference a purchase order"))
        return MatchResult("missing_po", "hold", vendor_id, None, flags)
    if po is None:
        flags.append(Flag("MISSING_PO", f"{inv.po_number} is not a purchase order in the ledger"))
        return MatchResult("missing_po", "hold", vendor_id, inv.po_number, flags)

    # 3. Line by line comparison
    if vendor_id and po.vendor_id != vendor_id:
        flags.append(Flag("VENDOR_MISMATCH", f"{po.doc_number} was issued to a different vendor"))
    if po.status.lower() != "open":
        flags.append(Flag("PO_CLOSED", f"{po.doc_number} is {po.status}"))

    po_lines = {ln.sku: ln for ln in po.lines}
    received: dict[str, Decimal] = defaultdict(Decimal)
    for r in receipts:
        received[r.sku] += r.qty_received

    if not inv.lines:
        flags.append(Flag("NO_LINES", "No line items could be read, so lines cannot be matched"))
    line_sum = Decimal(0)
    for ln in inv.lines:
        line_sum += ln.amount
        if money(ln.qty * ln.unit_price) != ln.amount:
            flags.append(
                Flag("LINE_MATH", f"{ln.sku}: {ln.qty} x {ln.unit_price} is not {ln.amount}")
            )
        po_line = po_lines.get(ln.sku or "")
        if po_line is None:
            flags.append(Flag("ITEM_NOT_ON_PO", f"{ln.sku} is not on {po.doc_number}"))
            continue
        if abs(ln.unit_price - po_line.unit_price) > PRICE_TOLERANCE:
            flags.append(
                Flag(
                    "PRICE_VARIANCE",
                    f"{ln.sku}: invoiced {ln.unit_price}, PO price {po_line.unit_price}",
                )
            )
        if ln.qty > po_line.qty:
            flags.append(Flag("QTY_OVER_PO", f"{ln.sku}: invoiced {ln.qty}, ordered {po_line.qty}"))
        got = received.get(ln.sku or "", Decimal(0))
        if ln.qty > got:
            flags.append(
                Flag(
                    "QTY_NOT_RECEIVED",
                    f"{ln.sku}: invoiced {ln.qty}, received {got}",
                )
            )
    if inv.total is not None and inv.lines and abs(money(line_sum) - inv.total) > TOTAL_TOLERANCE:
        flags.append(
            Flag("TOTAL_MISMATCH", f"Lines add to {money(line_sum)}, invoice says {inv.total}")
        )

    blocking = [f for f in flags if f.severity == "block"]
    if blocking:
        return MatchResult("mismatch", "hold", vendor_id, po.doc_number, flags)
    return MatchResult("match", "approve", vendor_id, po.doc_number, flags)
