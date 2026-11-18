"""QuickBooks Online adapter tests. No network: httpx.MockTransport plays Intuit."""

import json
from decimal import Decimal

import httpx
import pytest

from ap_agent.ledger.base import LedgerSafetyError
from ap_agent.ledger.qbo import QBOLedger, assert_sandbox
from ap_agent.models import BillLine


@pytest.mark.parametrize(
    "url",
    [
        "https://quickbooks.api.intuit.com",
        "http://sandbox-quickbooks.api.intuit.com",
        "https://sandbox-quickbooks.api.intuit.com.evil.test",
    ],
)
def test_only_sandbox_host_allowed(url):
    with pytest.raises(LedgerSafetyError):
        assert_sandbox(url)


def make(handler, tokens=None):
    calls = tokens if tokens is not None else []

    def token(force: bool) -> str:
        calls.append(force)
        return "fresh" if force else "stale"

    return QBOLedger(
        realm_id="123",
        get_access_token=token,
        expense_account_id="7",
        bank_account_id="35",
        transport=httpx.MockTransport(handler),
    )


def test_reads_map_qbo_shapes():
    def handler(req: httpx.Request) -> httpx.Response:
        q = req.url.params["query"]
        assert req.url.path == "/v3/company/123/query"
        if "from Vendor" in q:
            return httpx.Response(
                200,
                json={
                    "QueryResponse": {
                        "Vendor": [
                            {
                                "Id": "56",
                                "DisplayName": "Bob's Burger Joint",
                                "TermRef": {"name": "Net 30"},
                            }
                        ]
                    }
                },
            )
        if "from PurchaseOrder" in q:
            return httpx.Response(
                200,
                json={
                    "QueryResponse": {
                        "PurchaseOrder": [
                            {
                                "Id": "200",
                                "DocNumber": "1005",
                                "POStatus": "Open",
                                "VendorRef": {"value": "56"},
                                "Line": [
                                    {
                                        "Amount": 25,
                                        "ItemBasedExpenseLineDetail": {
                                            "ItemRef": {"name": "Pump", "value": "11"},
                                            "Qty": 1,
                                            "UnitPrice": 25,
                                        },
                                    }
                                ],
                            }
                        ]
                    }
                },
            )
        return httpx.Response(200, json={"QueryResponse": {}})

    led = make(handler)
    assert led.list_vendors()[0].display_name == "Bob's Burger Joint"
    po = led.get_purchase_order("1005")
    assert po is not None and po.lines[0].sku == "Pump" and po.total == Decimal("25.00")


def test_create_bill_body_and_token_refresh_on_401():
    seen = []
    tokens: list[bool] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.headers["Authorization"] == "Bearer stale":
            return httpx.Response(401)
        body = json.loads(req.content)
        return httpx.Response(
            200,
            json={
                "Bill": {
                    "Id": "901",
                    "VendorRef": body["VendorRef"],
                    "DocNumber": body["DocNumber"],
                    "TxnDate": body["TxnDate"],
                    "DueDate": body["DueDate"],
                    "TotalAmt": 84.0,
                    "Balance": 84.0,
                    "Line": body["Line"],
                }
            },
        )

    led = make(handler, tokens)
    bill = led.create_bill(
        vendor_id="56",
        doc_number="INV-777",
        txn_date="2026-10-14",
        due_date="2026-11-13",
        po_number="PO-2001",
        memo="AP agent PR-1. Approved by Jordan.",
        lines=[BillLine("HPF-1042", "Hex bolts", Decimal(10), Decimal("8.40"), Decimal("84.00"))],
    )
    assert tokens == [False, True]  # retried once with a refreshed token
    body = json.loads(seen[-1].content)
    assert seen[-1].url.path == "/v3/company/123/bill"
    assert body["Line"][0]["AccountBasedExpenseLineDetail"]["AccountRef"]["value"] == "7"
    assert "Approved by Jordan" in body["PrivateNote"]
    assert bill.id == "901" and bill.total == Decimal("84.00")


def test_payment_uses_sandbox_account_reference_only():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(
                200,
                json={
                    "Bill": {
                        "Id": "901",
                        "VendorRef": {"value": "56"},
                        "TotalAmt": 84,
                        "Balance": 84,
                    }
                },
            )
        body = json.loads(req.content)
        assert body["CheckPayment"]["BankAccountRef"] == {"value": "35"}
        assert set(body) == {"VendorRef", "PayType", "TotalAmt", "TxnDate", "CheckPayment", "Line"}
        return httpx.Response(
            200, json={"BillPayment": {"Id": "77", "TotalAmt": 84, "TxnDate": body["TxnDate"]}}
        )

    pay = make(handler).schedule_payment(bill_id="901", amount=Decimal(84), pay_date="2026-12-01")
    assert pay.id == "77" and pay.bank_account_ref == "35"


def test_payment_over_balance_refused():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "Bill": {"Id": "901", "VendorRef": {"value": "56"}, "TotalAmt": 84, "Balance": 10}
            },
        )

    with pytest.raises(ValueError):
        make(handler).schedule_payment(bill_id="901", amount=Decimal(84), pay_date="2026-12-01")
