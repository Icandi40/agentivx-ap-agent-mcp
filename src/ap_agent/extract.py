"""Pull invoice fields out of email text or a PDF.

This extractor is rule based on purpose: it is deterministic, free to run, and
its accuracy can be measured exactly by the eval script. A model based extractor
can sit behind the same function signature later; the evals would then measure it
the same way.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from pathlib import Path

from ap_agent.models import ExtractedInvoice, InvoiceLine, money

MONEY = r"(?:USD\s*)?\$?\s*(\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+\.\d{2}|\d+)(?:\s*USD)?"
SKU = r"\b((?!PO-|INV-)[A-Z]{2,4}-\d{3,5})\b"

INVOICE_NO_PATTERNS = [
    r"\binv(?:oice)?\.?\s*(?:#|no\.?|number|num\.?|id)\s*[:#]?\s*([A-Z0-9][A-Z0-9\-/]*\d[A-Z0-9\-/]*)",
    r"\binvoice\s*:\s*([A-Z0-9][A-Z0-9\-/]*\d[A-Z0-9\-/]*)",
    r"\binvoice\s+([A-Z0-9][A-Z0-9\-/]*\d[A-Z0-9\-/]*)\s+(?:from|for|attached)\b",
]
PO_PATTERN = (
    r"\b(?:P\.?\s?O\.?(?!\s*Box)|purchase\s+order)\s*(?:#|no\.?|number|ref(?:erence)?)?"
    r"\s*[:#]?\s*(?:PO-?)?(\d{3,6})\b"
)
VENDOR_LABELS = r"^(?:vendor|supplier|bill(?:ed)?\s+from|remit\s+to|from)\s*[:\-]\s*(.+)$"
SUBJECT_VENDOR = r"^subject:.*?\binvoice\b.*?\bfrom\s+(.+?)\s*$"
TOTAL_LABELS = [
    r"(?:amount|balance|total)\s+due",
    r"please\s+(?:pay|remit)",
    r"invoice\s+total",
    r"grand\s+total",
    r"(?<!sub)(?<!sub\s)\btotal\b(?!\s+(?:qty|quantity|items|units))",
]
DATE_FORMATS = [
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%B %d, %Y",
    "%b %d, %Y",
    "%B %d %Y",
    "%b %d %Y",
    "%d %B %Y",
    "%d %b %Y",
    "%b. %d, %Y",
]
DATE_RE = (
    r"(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|"
    r"[A-Z][a-z]{2,8}\.? \d{1,2},? \d{4}|\d{1,2} [A-Z][a-z]{2,8} \d{4})"
)

LINE_FORMATS = [
    # Table row: SKU | description | qty | unit price | amount  (pipes or 2+ spaces)
    re.compile(
        SKU
        + r"\s*(?:\||\s{2,})\s*(.+?)\s*(?:\||\s{2,})\s*(\d+(?:\.\d+)?)\s*(?:\||\s{2,})\s*"
        + MONEY
        + r"\s*(?:\||\s{2,})\s*"
        + MONEY
        + r"\s*$"
    ),
    # 12 x Hex bolts (HB-1042) @ $8.40 = $100.80
    re.compile(
        r"^\s*(\d+(?:\.\d+)?)\s*x\s+(.+?)\s*\("
        + SKU[2:-2]
        + r"\)\s*@\s*"
        + MONEY
        + r"\s*(?:each\s*)?=\s*"
        + MONEY
    ),
    # - Hex bolts, item HB-1042: 12 units at $8.40 each ($100.80)
    re.compile(
        r"^\s*[-*]?\s*(.+?),\s*(?:item|part|sku)\s*" + SKU + r"\s*:\s*(\d+(?:\.\d+)?)\s*"
        r"(?:units?|pcs|ea|each)?\s*at\s*"
        + MONEY
        + r"\s*(?:each|/ea|per unit)?\s*\(\s*"
        + MONEY
        + r"\s*\)",
        re.IGNORECASE,
    ),
]


def normalize_name(name: str) -> str:
    name = name.lower()
    name = re.sub(r"[^a-z0-9& ]", " ", name)
    name = re.sub(r"\b(inc|llc|ltd|co|corp|corporation|company|the)\b", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def name_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_name(a), normalize_name(b)).ratio()


def parse_date(text: str) -> str | None:
    text = text.strip().rstrip(".")
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _find_date(lines: list[str], labels: str) -> str | None:
    for line in lines:
        m = re.search(rf"\b(?:{labels})\b\s*[:\-]?\s*" + DATE_RE, line, re.IGNORECASE)
        if m:
            parsed = parse_date(m.group(1))
            if parsed:
                return parsed
    return None


def _to_money(raw: str) -> Decimal | None:
    try:
        return money(raw)
    except (InvalidOperation, ValueError):
        return None


def _parse_line(line: str) -> InvoiceLine | None:
    m = LINE_FORMATS[0].search(line)
    if m:
        sku, desc, qty, unit, amt = m.groups()
        return InvoiceLine(sku, desc.strip(), Decimal(qty), money(unit), money(amt))
    m = LINE_FORMATS[1].search(line)
    if m:
        qty, desc, sku, unit, amt = m.groups()
        return InvoiceLine(sku, desc.strip(), Decimal(qty), money(unit), money(amt))
    m = LINE_FORMATS[2].search(line)
    if m:
        desc, sku, qty, unit, amt = m.groups()
        return InvoiceLine(sku, desc.strip(), Decimal(qty), money(unit), money(amt))
    return None


def _find_total(lines: list[str]) -> Decimal | None:
    for label in TOTAL_LABELS:
        found: Decimal | None = None
        for line in lines:
            m = re.search(label + r"\s*(?:\(USD\))?\s*[:\-]?\s*" + MONEY, line, re.IGNORECASE)
            if m:
                found = _to_money(m.group(1))
        if found is not None:
            return found
    return None


def _find_vendor(lines: list[str], known_vendors: list[str] | None) -> str | None:
    candidates: list[str] = []
    for line in lines:
        m = re.search(SUBJECT_VENDOR, line, re.IGNORECASE)
        if m:
            candidates.append(m.group(1))
    for line in lines:
        m = re.search(VENDOR_LABELS, line.strip(), re.IGNORECASE)
        if m:
            value = re.sub(r"<[^>]*>", "", m.group(1)).strip()
            candidates.append(value)
    first = next((ln.strip() for ln in lines if ln.strip()), "")
    if first and not re.match(r"^(invoice|subject|from|to|date)\b", first, re.IGNORECASE):
        candidates.append(first)

    if not known_vendors:
        return candidates[0] if candidates else None

    best_name, best_score = None, 0.0
    for cand in candidates:
        for vendor in known_vendors:
            score = name_similarity(cand, vendor)
            if score > best_score:
                best_name, best_score = vendor, score
    if best_score >= 0.85:
        return best_name
    # Fall back to scanning the whole document for a known vendor name.
    body = normalize_name(" ".join(lines))
    for vendor in known_vendors:
        if normalize_name(vendor) and normalize_name(vendor) in body:
            return vendor
    return candidates[0] if candidates else None


def extract_text(text: str, known_vendors: list[str] | None = None) -> ExtractedInvoice:
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n")]
    inv = ExtractedInvoice()

    inv.vendor_name = _find_vendor(lines, known_vendors)

    for pattern in INVOICE_NO_PATTERNS:
        for line in lines:
            m = re.search(pattern, line, re.IGNORECASE)
            if m:
                inv.invoice_number = m.group(1).strip().upper()
                break
        if inv.invoice_number:
            break

    inv.invoice_date = _find_date(lines, r"invoice\s+date|date\s+issued|issued|dated|date")
    inv.due_date = _find_date(lines, r"due\s+date|payment\s+due|due")
    if inv.due_date and inv.due_date == inv.invoice_date:
        # "Date" matched the due date line; look for a date-only label again.
        inv.invoice_date = _find_date(lines, r"invoice\s+date|date\s+issued|issued|dated")

    for line in lines:
        m = re.search(PO_PATTERN, line, re.IGNORECASE)
        if m:
            inv.po_number = f"PO-{m.group(1)}"
            break

    inv.lines = [parsed for line in lines if (parsed := _parse_line(line))]
    inv.total = _find_total(lines)

    if not inv.lines:
        inv.warnings.append("no line items found")
    if inv.total is None:
        inv.warnings.append("no total found")
    if inv.invoice_number is None:
        inv.warnings.append("no invoice number found")
    return inv


def pdf_to_text(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def extract_file(path: Path, known_vendors: list[str] | None = None) -> ExtractedInvoice:
    path = Path(path)
    text = pdf_to_text(path) if path.suffix.lower() == ".pdf" else path.read_text(encoding="utf-8")
    return extract_text(text, known_vendors)


def today_iso() -> str:
    return date.today().isoformat()
