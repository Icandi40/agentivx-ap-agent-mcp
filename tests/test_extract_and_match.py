from decimal import Decimal

from conftest import BIG_INVOICE, CLEAN_INVOICE

from ap_agent.extract import extract_text, parse_date

VENDORS = ["Harbor Point Fasteners LLC", "Bluegate Office Supply"]


def test_formal_invoice_fields():
    inv = extract_text(CLEAN_INVOICE, VENDORS)
    assert inv.vendor_name == "Harbor Point Fasteners LLC"
    assert inv.invoice_number == "INV-777"
    assert inv.invoice_date == "2026-10-14"
    assert inv.due_date == "2026-11-13"
    assert inv.po_number == "PO-2001"  # and not the "PO Box 4410" in the remit address
    assert inv.total == Decimal("144.00")
    assert [(ln.sku, ln.qty, ln.unit_price) for ln in inv.lines] == [
        ("HPF-1042", Decimal(10), Decimal("8.40")),
        ("HPF-2001", Decimal(3), Decimal("20.00")),
    ]


def test_email_invoice_fields():
    inv = extract_text(BIG_INVOICE, VENDORS)
    assert inv.vendor_name == "Bluegate Office Supply"
    assert inv.invoice_number == "9001"
    assert inv.po_number == "PO-2002"
    assert inv.total == Decimal("15000.00")
    assert inv.invoice_date == "2026-10-30"
    assert len(inv.lines) == 1


def test_terse_invoice_and_vendor_variant():
    text = (
        "HARBOR POINT FASTENERS - INVOICE\n"
        "Inv No. HA4471    Dated 14 Oct 2026\n"
        "Ref: P.O. 2001\n\n"
        "10 x Hex bolts (HPF-1042) @ $8.40 = $84.00\n\n"
        "Please pay 84.00 USD by Nov 13, 2026."
    )
    inv = extract_text(text, VENDORS)
    assert inv.vendor_name == "Harbor Point Fasteners LLC"
    assert inv.invoice_number == "HA4471"
    assert inv.invoice_date == "2026-10-14"
    assert inv.po_number == "PO-2001"
    assert inv.total == Decimal("84.00")


def test_missing_fields_are_reported_not_guessed():
    inv = extract_text("Hello, please pay us.", VENDORS)
    assert inv.invoice_number is None and inv.total is None and inv.po_number is None
    assert "no total found" in inv.warnings


def test_parse_date_formats():
    for raw in ["2026-10-05", "10/05/2026", "October 5, 2026", "Oct 5, 2026", "05 Oct 2026"]:
        assert parse_date(raw) == "2026-10-05"


def _check(svc, text):
    return svc.check_invoice(text=text)["match"]


def test_clean_invoice_matches(svc):
    m = _check(svc, CLEAN_INVOICE)
    assert m["outcome"] == "match" and m["recommendation"] == "approve" and m["flags"] == []


def test_price_variance_is_flagged(svc):
    m = _check(
        svc, CLEAN_INVOICE.replace("$8.40 | $84.00", "$8.90 | $89.00").replace("144.00", "149.00")
    )
    assert m["outcome"] == "mismatch"
    assert "PRICE_VARIANCE" in [f["code"] for f in m["flags"]]


def test_billing_more_than_received_is_flagged(svc):
    text = CLEAN_INVOICE.replace("| 3 | $20.00 | $60.00", "| 5 | $20.00 | $100.00").replace(
        "144.00", "184.00"
    )
    m = _check(svc, text)
    assert m["outcome"] == "mismatch"
    assert "QTY_NOT_RECEIVED" in [f["code"] for f in m["flags"]]


def test_total_that_does_not_add_up_is_flagged(svc):
    m = _check(svc, CLEAN_INVOICE.replace("Total Due: $144.00", "Total Due: $244.00"))
    assert "TOTAL_MISMATCH" in [f["code"] for f in m["flags"]]


def test_po_from_another_vendor_is_flagged(svc):
    m = _check(svc, CLEAN_INVOICE.replace("PO-2001", "PO-2002"))
    codes = [f["code"] for f in m["flags"]]
    assert m["outcome"] == "mismatch" and "VENDOR_MISMATCH" in codes


def test_missing_and_unknown_po(svc):
    assert _check(svc, CLEAN_INVOICE.replace("PO Number: PO-2001\n", ""))["outcome"] == "missing_po"
    assert _check(svc, CLEAN_INVOICE.replace("PO-2001", "PO-9999"))["outcome"] == "missing_po"


def test_duplicate_of_ledger_bill(svc):
    m = _check(svc, CLEAN_INVOICE.replace("INV-777", "INV-500"))
    assert m["outcome"] == "duplicate"


def test_duplicate_of_pending_proposal(svc):
    first = svc.propose_bill(text=CLEAN_INVOICE)
    assert first["proposal_id"]
    again = svc.check_invoice(text=CLEAN_INVOICE + "\n(resent)")
    assert again["match"]["outcome"] == "duplicate"
