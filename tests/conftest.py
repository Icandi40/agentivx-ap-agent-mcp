"""Shared fixtures: a tiny synthetic company and a service wired to temp files."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ap_agent.audit import AuditLog
from ap_agent.ledger.offline import OfflineLedger
from ap_agent.proposals import ProposalStore
from ap_agent.service import APService

COMPANY = {
    "vendors": [
        {"id": "1", "display_name": "Harbor Point Fasteners LLC", "email": "", "terms_days": 30},
        {"id": "2", "display_name": "Bluegate Office Supply", "email": "", "terms_days": 30},
    ],
    "purchase_orders": [
        {
            "id": "10",
            "doc_number": "PO-2001",
            "vendor_id": "1",
            "status": "Open",
            "lines": [
                {"sku": "HPF-1042", "description": "Hex bolts", "qty": 10, "unit_price": "8.40"},
                {"sku": "HPF-2001", "description": "Washers", "qty": 5, "unit_price": "20.00"},
            ],
        },
        {
            "id": "11",
            "doc_number": "PO-2002",
            "vendor_id": "2",
            "status": "Open",
            "lines": [
                {"sku": "BGO-3001", "description": "Desks", "qty": 100, "unit_price": "150.00"}
            ],
        },
    ],
    "receipts": [
        {"po_number": "PO-2001", "sku": "HPF-1042", "qty_received": 10},
        {"po_number": "PO-2001", "sku": "HPF-2001", "qty_received": 3},
        {"po_number": "PO-2002", "sku": "BGO-3001", "qty_received": 100},
    ],
    "bills": [
        {
            "id": "H1",
            "vendor_id": "1",
            "doc_number": "INV-500",
            "txn_date": "2026-09-01",
            "total": "50.00",
            "balance": "0.00",
        }
    ],
    "payments": [],
}

CLEAN_INVOICE = """Harbor Point Fasteners LLC
Remit to: PO Box 4410, Sample City
INVOICE
Invoice #: INV-777
Invoice Date: 10/14/2026
Due Date: November 13, 2026
PO Number: PO-2001
SKU | Description | Qty | Unit Price | Amount
HPF-1042 | Hex bolts | 10 | $8.40 | $84.00
HPF-2001 | Washers | 3 | $20.00 | $60.00
Subtotal: $144.00
Total Due: $144.00
"""

BIG_INVOICE = """From: Dana <billing@bluegate.test>
Subject: Invoice 9001 from Bluegate Office Supply

Please find our invoice below for P.O. # 2002.

- Desks, item BGO-3001: 100 units at $150.00 each ($15,000.00)

Amount due: $15,000.00 USD, payable by Dec 1, 2026.
Invoice date: Oct 30, 2026
"""


@pytest.fixture
def company_file(tmp_path: Path) -> Path:
    p = tmp_path / "seed.json"
    p.write_text(json.dumps(COMPANY), encoding="utf-8")
    return p


@pytest.fixture
def svc(tmp_path: Path, company_file: Path) -> APService:
    audit = AuditLog(tmp_path / "state" / "audit.jsonl")
    ledger = OfflineLedger.from_seed(company_file, tmp_path / "state" / "ledger.json")
    store = ProposalStore(tmp_path / "state" / "proposals.sqlite", audit)
    return APService(ledger, store, audit, tmp_path / "state" / "inbox")
