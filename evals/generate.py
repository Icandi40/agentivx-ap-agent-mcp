"""Generate a synthetic company and synthetic invoices with known correct outcomes.

Everything here is invented sample data: vendor names, SKUs, prices, people. Two
splits are written with different random seeds:

  dev   used while writing the extractor and the matcher
  test  held out; EVALS.md reports this split

Run: uv run python evals/generate.py
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "dataset"

VENDORS = [
    ("Harbor Point Fasteners LLC", "HPF", "harborpoint-sample.test", 30),
    ("Bluegate Office Supply", "BGO", "bluegate-sample.test", 30),
    ("Cedar Ridge Packaging Co.", "CRP", "cedarridge-sample.test", 45),
    ("Summit Line Electrical Inc.", "SLE", "summitline-sample.test", 30),
    ("Ironleaf Tools Inc.", "ILT", "ironleaf-sample.test", 15),
    ("Brightwater Janitorial", "BWJ", "brightwater-sample.test", 30),
    ("Keystone Lab Supply", "KLS", "keystonelab-sample.test", 60),
    ("Redfern Freight Services", "RFS", "redfern-sample.test", 30),
]
ITEMS = [
    "Hex bolts M8 (box of 100)",
    "Copy paper, letter, 10 reams",
    "Corrugated boxes 18x12x12",
    "LED panel 2x4 40W",
    "Cordless drill 20V",
    "Floor cleaner concentrate 5 gal",
    "Nitrile gloves, case",
    "Pallet wrap 18in",
    "Cable ties 11in (1000)",
    "Safety glasses",
    "Toner cartridge, black",
    "Shipping labels 4x6 (500)",
    "Wire nuts assorted",
    "Mop heads (12)",
    "Pipette tips 200uL (rack)",
    "Drill bit set 29pc",
    "Packing tape 3in (36)",
    "Breaker 20A",
]
PEOPLE = ["Dana Ruiz", "Lee Okafor", "Priya Natarajan", "Sam Whitfield", "Morgan Chu"]

OUTCOME_MIX = {
    "dev": {"match": 8, "mismatch": 6, "duplicate": 3, "missing_po": 3},
    "test": {"match": 24, "mismatch": 18, "duplicate": 9, "missing_po": 9},
    "unseen": {"match": 12, "mismatch": 10, "duplicate": 4, "missing_po": 4},
}
# Layouts per split. "columnar" and "ocr" were added after the extractor was frozen
# (see git history), so the "unseen" split measures layouts it was never written for.
TEMPLATES = {
    "dev": ["formal", "email", "terse"],
    "test": ["formal", "email", "terse"],
    "unseen": ["columnar", "ocr"],
}
MISMATCH_KINDS = ["price", "qty_not_received", "extra_item", "total_wrong", "wrong_vendor"]


@dataclass
class Case:
    case_id: str
    expected_outcome: str
    variant: str
    vendor_id: str
    vendor_name: str
    invoice_number: str
    invoice_date: date
    due_date: date
    po_ref: str | None
    lines: list[dict]
    total: Decimal
    template: str = "formal"
    fmt: str = "txt"
    notes: list[str] = field(default_factory=list)


def m(x: Decimal | float | str) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"))


def build_company(rng: random.Random, n_pos: int) -> dict:
    vendors, catalog = [], {}
    for i, (name, prefix, domain, terms) in enumerate(VENDORS, start=1):
        vid = str(i)
        vendors.append(
            {"id": vid, "display_name": name, "email": f"billing@{domain}", "terms_days": terms}
        )
        catalog[vid] = [
            {
                "sku": f"{prefix}-{rng.randint(100, 9999)}",
                "description": rng.choice(ITEMS),
                "unit_price": str(m(rng.uniform(3, 220))),
            }
            for _ in range(6)
        ]
    pos, receipts = [], []
    for n in range(n_pos):
        vid = rng.choice(vendors)["id"]
        items = rng.sample(catalog[vid], rng.randint(1, 4))
        doc = f"PO-{1001 + n}"
        lines = []
        for it in items:
            qty = rng.randint(2, 60)
            lines.append({**it, "qty": qty})
            # Receiving log: most lines fully received, some partly.
            got = qty if rng.random() > 0.25 else rng.randint(1, qty - 1)
            receipts.append(
                {
                    "po_number": doc,
                    "sku": it["sku"],
                    "qty_received": got,
                    "received_date": str(date(2026, 10, 1) + timedelta(days=rng.randint(0, 30))),
                }
            )
        pos.append(
            {
                "id": str(500 + n),
                "doc_number": doc,
                "vendor_id": vid,
                "status": "Open",
                "txn_date": "2026-09-15",
                "lines": lines,
            }
        )
    return {
        "vendors": vendors,
        "catalog": catalog,
        "purchase_orders": pos,
        "receipts": receipts,
        "bills": [],
        "payments": [],
    }


def inv_number(rng: random.Random, prefix: str) -> str:
    style = rng.choice(["num", "inv", "pre"])
    n = rng.randint(1000, 99999)
    return {"num": str(n), "inv": f"INV-{n}", "pre": f"{prefix}{n}"}[style]


def make_cases(rng: random.Random, company: dict, split: str) -> list[Case]:
    vendors = {v["id"]: v for v in company["vendors"]}
    received = {}
    for r in company["receipts"]:
        received[(r["po_number"], r["sku"])] = r["qty_received"]
    pool = list(company["purchase_orders"])
    rng.shuffle(pool)
    mix = OUTCOME_MIX[split]
    cases: list[Case] = []
    counter = 0

    def new_case(outcome: str, variant: str, po: dict | None, vid: str) -> Case:
        nonlocal counter
        counter += 1
        v = vendors[vid]
        d = date(2026, 10, 5) + timedelta(days=rng.randint(0, 40))
        return Case(
            case_id=f"{split}-{counter:03d}",
            expected_outcome=outcome,
            variant=variant,
            vendor_id=vid,
            vendor_name=v["display_name"],
            invoice_number=inv_number(rng, v["display_name"][:2].upper()),
            invoice_date=d,
            due_date=d + timedelta(days=v["terms_days"]),
            po_ref=po["doc_number"] if po else None,
            lines=[],
            total=Decimal(0),
        )

    def billed_lines(po: dict) -> list[dict]:
        out = []
        for ln in po["lines"]:
            q = received[(po["doc_number"], ln["sku"])]
            out.append(
                {
                    "sku": ln["sku"],
                    "description": ln["description"],
                    "qty": q,
                    "unit_price": m(ln["unit_price"]),
                }
            )
        return out

    def finish(c: Case) -> Case:
        for ln in c.lines:
            ln["amount"] = m(ln["unit_price"] * ln["qty"])
        c.total = m(sum(ln["amount"] for ln in c.lines))
        return c

    for _ in range(mix["match"]):
        po = pool.pop()
        c = new_case("match", "clean", po, po["vendor_id"])
        c.lines = billed_lines(po)
        cases.append(finish(c))

    for i in range(mix["mismatch"]):
        kind = MISMATCH_KINDS[i % len(MISMATCH_KINDS)]
        if kind == "qty_not_received":
            # pick a PO with a partly received line
            po = next(
                (
                    p
                    for p in pool
                    if any(received[(p["doc_number"], ln["sku"])] < ln["qty"] for ln in p["lines"])
                ),
                pool[-1],
            )
            pool.remove(po)
        else:
            po = pool.pop()
        vid = po["vendor_id"]
        if kind == "wrong_vendor":
            vid = rng.choice([v for v in vendors if v != po["vendor_id"]])
        c = new_case("mismatch", kind, po, vid)
        c.lines = billed_lines(po)
        if kind == "price":
            ln = rng.choice(c.lines)
            ln["unit_price"] = m(ln["unit_price"] * Decimal(str(rng.uniform(1.03, 1.12))))
        elif kind == "qty_not_received":
            for ln, pol in zip(c.lines, po["lines"], strict=True):
                ln["qty"] = pol["qty"]  # bills for everything ordered, not what arrived
            if all(received[(po["doc_number"], ln["sku"])] >= ln["qty"] for ln in c.lines):
                c.lines[0]["qty"] += 3
        elif kind == "extra_item":
            c.lines.append(
                {
                    "sku": f"{rng.choice(['MSC', 'ADJ', 'SVC'])}-{rng.randint(100, 999)}",
                    "description": rng.choice(
                        ["Rush handling fee", "Fuel surcharge", "Restocking fee"]
                    ),
                    "qty": 1,
                    "unit_price": m(rng.uniform(25, 150)),
                }
            )
        finish(c)
        if kind == "total_wrong":
            c.total = m(c.total + Decimal(rng.choice([10, 45, 100, 250])) + m(rng.random()))
            c.notes.append("printed total does not equal the sum of lines")
        cases.append(c)

    # Missing PO: either no reference at all, or one that is not in the ledger.
    for i in range(mix["missing_po"]):
        vid = rng.choice(list(vendors))
        c = new_case("missing_po", "no_reference" if i % 2 == 0 else "unknown_po", None, vid)
        items = rng.sample(company["catalog"][vid], rng.randint(1, 3))
        c.lines = [
            {
                "sku": it["sku"],
                "description": it["description"],
                "qty": rng.randint(1, 20),
                "unit_price": m(it["unit_price"]),
            }
            for it in items
        ]
        if c.variant == "unknown_po":
            c.po_ref = f"PO-{rng.randint(8000, 8999)}"
        cases.append(finish(c))

    # Duplicates: half already paid last month (a bill exists), half resubmitted in this batch.
    for i in range(mix["duplicate"]):
        if i % 2 == 0:
            po = pool.pop()
            c = new_case("duplicate", "already_billed", po, po["vendor_id"])
            c.lines = billed_lines(po)
            finish(c)
            company["bills"].append(
                {
                    "id": f"H{len(company['bills']) + 1:04d}",
                    "vendor_id": c.vendor_id,
                    "doc_number": c.invoice_number,
                    "txn_date": "2026-09-20",
                    "due_date": "2026-10-20",
                    "total": str(c.total),
                    "balance": "0.00",
                    "po_number": po["doc_number"],
                    "memo": "sample history",
                    "lines": [],
                }
            )
            cases.append(c)
        else:
            original = rng.choice([c for c in cases if c.expected_outcome == "match"])
            c = Case(**{**original.__dict__})
            counter += 1
            c.case_id = f"{split}-{counter:03d}"
            c.expected_outcome, c.variant = "duplicate", "resubmitted"
            c.lines = [dict(ln) for ln in original.lines]
            c.notes = [f"copy of {original.case_id}"]
            cases.append(c)

    # Shuffle, but keep every resubmission after the invoice it copies.
    rng.shuffle(cases)
    order = {c.case_id: i for i, c in enumerate(cases)}
    for c in cases:
        if c.variant == "resubmitted":
            src = c.notes[0].split()[-1]
            if order[c.case_id] < order[src]:
                order[c.case_id], order[src] = order[src], order[c.case_id]
    cases.sort(key=lambda c: order[c.case_id])

    templates = TEMPLATES[split]
    for i, c in enumerate(cases):
        if c.variant == "resubmitted":
            c.template = rng.choice(templates)
        else:
            c.template = templates[i % len(templates)]
        c.fmt = "pdf" if c.template in {"formal", "columnar"} and rng.random() < 0.5 else "txt"
    return cases


# Rendering


def fmt_date(rng: random.Random, d: date) -> str:
    return rng.choice(
        [
            d.isoformat(),
            d.strftime("%m/%d/%Y"),
            d.strftime("%B %d, %Y").replace(" 0", " "),
            d.strftime("%b %d, %Y").replace(" 0", " "),
            d.strftime("%d %b %Y"),
        ]
    )


def fmt_money(rng: random.Random, x: Decimal, allow_usd: bool = True) -> str:
    s = f"{x:,.2f}"
    return rng.choice([f"${s}", f"${s}", f"{s} USD"] if allow_usd else [f"${s}"])


def vendor_variant(rng: random.Random, name: str) -> str:
    base = name.replace(" LLC", "").replace(" Inc.", "").replace(" Co.", "")
    return rng.choice([name, name, base, base.upper(), base + ", Inc."])


def po_phrase(rng: random.Random, po: str) -> str:
    n = po.split("-")[1]
    return rng.choice(
        [
            f"PO Number: {po}",
            f"P.O. # {n}",
            f"Purchase Order: {po}",
            f"Customer PO: {n}",
            f"Your PO {po}",
        ]
    )


OCR_SWAPS = [("I", "l"), ("O", "0"), ("$", "S"), ("1", "l"), ("o", "0"), (",", ".")]


def ocr_noise(rng: random.Random, text: str) -> str:
    """Imitate a poor scan: swap look-alike characters and break some spacing."""
    out = []
    for line in text.split("\n"):
        if rng.random() < 0.45:
            a, b = rng.choice(OCR_SWAPS)
            idx = [i for i, ch in enumerate(line) if ch == a]
            if idx:
                i = rng.choice(idx)
                line = line[:i] + b + line[i + 1 :]
        if rng.random() < 0.2:
            line = line.replace(": ", ":", 1)
        out.append(line)
    return "\n".join(out)


def render_columnar(rng: random.Random, c: Case, vname: str) -> str:
    """An accounting-package export: label columns, qty first, no pipes, no $ signs."""
    po_label = rng.choice(["Order Ref", "PO Number", "Your Order No.", "Purchase Order"])
    out = [
        vname,
        "TAX INVOICE",
        f"Invoice Number      {c.invoice_number}",
        f"Invoice Date        {fmt_date(rng, c.invoice_date)}",
        f"Payment Terms       Net {(c.due_date - c.invoice_date).days}",
    ]
    if c.po_ref:
        out.append(f"{po_label:<20}{c.po_ref.split('-')[1] if rng.random() < 0.5 else c.po_ref}")
    out += [
        "",
        f"{'Qty':<6}{'Item Code':<12}{'Description':<30}{'Unit Price':>12}{'Line Total':>12}",
    ]
    for ln in c.lines:
        out.append(
            f"{ln['qty']!s:<6}{ln['sku']:<12}{ln['description'][:29]:<30}"
            f"{ln['unit_price']:>12,.2f}{ln['amount']:>12,.2f}"
        )
    out += ["", f"{'Invoice Total (USD)':<60}{c.total:>12,.2f}"]
    return "\n".join(out)


def render(rng: random.Random, c: Case) -> str:
    vname = vendor_variant(rng, c.vendor_name)
    person = rng.choice(PEOPLE)
    if c.template == "columnar":
        return render_columnar(rng, c, vname)
    if c.template == "ocr":
        return ocr_noise(rng, render(rng, Case(**{**c.__dict__, "template": "formal"})))
    if c.template == "formal":
        rows = [
            f"{ln['sku']} | {ln['description']} | {ln['qty']} | "
            f"${ln['unit_price']:,.2f} | ${ln['amount']:,.2f}"
            for ln in c.lines
        ]
        out = [
            vname,
            f"{rng.randint(10, 9999)} Commerce Way, Suite {rng.randint(1, 400)}",
            f"Remit to: PO Box {rng.randint(100, 9999)}, Sample City, ST {rng.randint(10000, 99999)}",
            f"Phone (555) 01{rng.randint(0, 9)}-{rng.randint(1000, 9999)}",
            "",
            "INVOICE",
            f"Invoice #: {c.invoice_number}",
            f"Invoice Date: {fmt_date(rng, c.invoice_date)}",
            f"Due Date: {fmt_date(rng, c.due_date)}",
        ]
        if c.po_ref:
            out.append(po_phrase(rng, c.po_ref))
        out += [
            "Bill To: Sample Buyer Co. (demo company)",
            "",
            "SKU | Description | Qty | Unit Price | Amount",
            *rows,
            "",
            f"Subtotal: ${sum(ln['amount'] for ln in c.lines):,.2f}",
            f"Total Due: {fmt_money(rng, c.total, allow_usd=False)}",
            f"Thank you for 12 years of business. Terms: Net {(c.due_date - c.invoice_date).days}.",
        ]
        return "\n".join(out)
    if c.template == "email":
        rows = [
            f"- {ln['description']}, item {ln['sku']}: {ln['qty']} units at "
            f"${ln['unit_price']:,.2f} each (${ln['amount']:,.2f})"
            for ln in c.lines
        ]
        po_line = (
            f"Please find our invoice below for {po_phrase(rng, c.po_ref)}."
            if c.po_ref
            else "Please find our invoice below for this month's order."
        )
        return "\n".join(
            [
                f"From: {person} <billing@example-vendor.test>",
                "To: ap@sample-buyer.test",
                f"Subject: Invoice {c.invoice_number} from {vname}",
                "",
                "Hi team,",
                "",
                po_line,
                "",
                *rows,
                "",
                f"Amount due: {fmt_money(rng, c.total)}, payable by {fmt_date(rng, c.due_date)}.",
                f"Invoice date: {fmt_date(rng, c.invoice_date)}",
                "",
                "Thanks,",
                person,
                vname,
            ]
        )
    rows = [
        f"{ln['qty']} x {ln['description']} ({ln['sku']}) @ ${ln['unit_price']:,.2f} = "
        f"${ln['amount']:,.2f}"
        for ln in c.lines
    ]
    out = [
        f"{vname.upper()} - INVOICE",
        f"Inv No. {c.invoice_number}    Dated {fmt_date(rng, c.invoice_date)}",
    ]
    if c.po_ref:
        out.append(f"Ref: P.O. {c.po_ref.split('-')[1]}")
    out += ["", *rows, "", f"Please pay {fmt_money(rng, c.total)} by {fmt_date(rng, c.due_date)}."]
    return "\n".join(out)


def write_pdf(text: str, path: Path) -> None:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.set_creation_date(datetime(2026, 11, 18, tzinfo=UTC))  # reproducible bytes
    pdf.add_page()
    pdf.set_font("Helvetica", size=10)
    for line in text.split("\n"):
        pdf.cell(0, 5, line, new_x="LMARGIN", new_y="NEXT")
    pdf.output(str(path))


def generate(split: str, seed: int, n_pos: int) -> None:
    rng = random.Random(seed)
    company = build_company(rng, n_pos)
    cases = make_cases(rng, company, split)
    d = OUT / split
    (d / "invoices").mkdir(parents=True, exist_ok=True)
    for old in (d / "invoices").iterdir():
        old.unlink()
    labels = []
    for c in cases:
        text = render(rng, c)
        name = f"{c.case_id}.{c.fmt}"
        if c.fmt == "pdf":
            write_pdf(text, d / "invoices" / name)
        else:
            (d / "invoices" / name).write_text(text, encoding="utf-8")
        labels.append(
            {
                "case_id": c.case_id,
                "file": name,
                "template": c.template,
                "format": c.fmt,
                "expected_outcome": c.expected_outcome,
                "variant": c.variant,
                "notes": c.notes,
                "expected": {
                    "vendor_name": c.vendor_name,
                    "invoice_number": c.invoice_number.upper(),
                    "invoice_date": c.invoice_date.isoformat(),
                    "po_number": c.po_ref,
                    "total": str(c.total),
                    "lines": [[ln["sku"], str(ln["qty"]), str(ln["unit_price"])] for ln in c.lines],
                },
            }
        )
    company.pop("catalog")
    (d / "company.json").write_text(json.dumps(company, indent=2, default=str), encoding="utf-8")
    (d / "labels.json").write_text(json.dumps(labels, indent=2), encoding="utf-8")
    counts = {k: sum(1 for c in cases if c.expected_outcome == k) for k in OUTCOME_MIX[split]}
    print(f"{split}: {len(cases)} invoices {counts} -> {d}")


def make_demo_sample() -> None:
    """Copy the dev company and three dev invoices into data/sample for the demo."""
    dev = OUT / "dev"
    sample = ROOT.parent / "data" / "sample"
    (sample / "invoices").mkdir(parents=True, exist_ok=True)
    (sample / "company.json").write_text(
        (dev / "company.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    labels = json.loads((dev / "labels.json").read_text(encoding="utf-8"))
    picks = {
        "1-clean": next(x for x in labels if x["expected_outcome"] == "match"),
        "2-price-variance": next(x for x in labels if x["variant"] == "price"),
        "3-no-po": next(x for x in labels if x["expected_outcome"] == "missing_po"),
    }
    for old in (sample / "invoices").iterdir():
        old.unlink()
    for stem, lab in picks.items():
        src = dev / "invoices" / lab["file"]
        (sample / "invoices" / f"demo-{stem}{src.suffix}").write_bytes(src.read_bytes())
    print(f"demo sample -> {sample}")


def main() -> None:
    generate("dev", seed=7, n_pos=40)
    generate("test", seed=20261118, n_pos=90)
    generate("unseen", seed=424242, n_pos=60)
    make_demo_sample()


if __name__ == "__main__":
    main()
