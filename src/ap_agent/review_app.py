"""Small review page where a person approves or rejects proposals.

Runs on 127.0.0.1 only. This is a separate process from the MCP server, so the
agent has no way to click these buttons. Reviewer identity is a typed name here;
a real deployment would take it from the company's single sign-on.
"""

from __future__ import annotations

import html
import json
import secrets

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from ap_agent.proposals import ApprovalError, Proposal
from ap_agent.service import APService

CSS = """
:root{--bg:#f6f5f2;--card:#fff;--ink:#1d1d1f;--muted:#6b6b70;--line:#e3e1dc;--ok:#1f7a4d;
--warn:#a5520b;--bad:#b42318;--accent:#2f4a7a}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--card:#1d1d21;--ink:#ececf0;
--muted:#9a9aa3;--line:#2e2e34;--ok:#4cc38a;--warn:#f0a35e;--bad:#ff7b72;--accent:#8fb0ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:920px;margin:0 auto;padding:24px 16px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:24px 0 8px}
.sub{color:var(--muted);margin:0 0 20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;
margin-bottom:12px}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
td,th{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);font-size:14px}
th{color:var(--muted);font-weight:600}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;font-weight:600;
border:1px solid currentColor}
.approve,.executed{color:var(--ok)}.hold,.pending{color:var(--warn)}
.rejected,.failed{color:var(--bad)}.approved{color:var(--accent)}
.flag{color:var(--bad);margin:4px 0}
form{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
input,textarea{font:inherit;padding:8px;border:1px solid var(--line);border-radius:6px;
background:var(--bg);color:var(--ink);flex:1 1 200px}
button{font:inherit;padding:8px 14px;border-radius:6px;border:0;cursor:pointer;color:#fff}
.b-ok{background:var(--ok)}.b-bad{background:var(--bad)}
.err{background:var(--bad);color:#fff;padding:10px;border-radius:8px}
a{color:var(--accent)}code{font-size:13px}
"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head>"
        f"<body><main>{body}</main></body></html>"
    )


def _e(v: object) -> str:
    return html.escape(str(v if v is not None else ""))


def _pill(word: str) -> str:
    return f"<span class='pill {_e(word)}'>{_e(word)}</span>"


def create_app(service: APService | None = None) -> Starlette:
    svc = service or APService.from_paths()
    csrf = secrets.token_urlsafe(24)

    def row(p: Proposal) -> str:
        pl = p.payload
        what = (
            f"Bill {_e(pl.get('doc_number'))} from {_e(pl.get('vendor_name'))}"
            if p.kind == "create_bill"
            else f"Pay bill {_e(pl.get('bill_doc_number'))} on {_e(pl.get('pay_date'))}"
        )
        return (
            f"<tr><td><a href='/p/{_e(p.id)}'>{_e(p.id)}</a></td><td>{what}</td>"
            f"<td>${_e(p.amount)}</td><td>{_pill(p.recommendation)}</td>"
            f"<td>{_pill(p.status)}</td></tr>"
        )

    async def index(request: Request) -> Response:
        items = svc.proposals.list()
        pending = [p for p in items if p.status == "pending"]
        done = [p for p in items if p.status != "pending"]
        head = "<tr><th>Id</th><th>What</th><th>Amount</th><th>Agent says</th><th>Status</th></tr>"
        body = (
            "<h1>AP approvals</h1><p class='sub'>Sandbox only. The agent proposes; "
            f"a person decides. Ledger: <code>{_e(svc.ledger.name)}</code></p>"
            f"<h2>Waiting for you ({len(pending)})</h2><div class='card'><table>{head}"
            + "".join(row(p) for p in pending)
            + f"</table></div><h2>Decided ({len(done)})</h2><div class='card'><table>{head}"
            + "".join(row(p) for p in reversed(done))
            + "</table></div>"
        )
        return _page("AP approvals", body)

    async def detail(request: Request) -> Response:
        pid = request.path_params["pid"]
        try:
            p = svc.proposals.get(pid)
        except KeyError:
            return _page("Not found", "<p>No such proposal.</p>")
        error = request.query_params.get("error", "")
        pl = p.payload
        flags = (
            "".join(
                f"<div class='flag'>&#9888; <b>{_e(f['code'])}</b> {_e(f['message'])}</div>"
                for f in p.flags
            )
            or "<div>No flags. Invoice, PO and receiving log agree.</div>"
        )
        lines = ""
        if p.kind == "create_bill":
            lines = (
                "<table><tr><th>SKU</th><th>Item</th><th>Qty</th><th>Unit</th><th>Amount</th></tr>"
            )
            lines += (
                "".join(
                    f"<tr><td>{_e(ln['sku'])}</td><td>{_e(ln['description'])}</td><td>{_e(ln['qty'])}</td>"
                    f"<td>${_e(ln['unit_price'])}</td><td>${_e(ln['amount'])}</td></tr>"
                    for ln in pl["lines"]
                )
                + "</table>"
            )
            po = svc.purchase_order(pl["po_number"]) if pl.get("po_number") else None
            if po:
                lines += f"<h2>{_e(po['doc_number'])} and receiving log</h2><table>"
                lines += "<tr><th>SKU</th><th>Ordered</th><th>PO price</th><th>Received</th></tr>"
                got: dict[str, float] = {}
                for r in po["receipts"]:
                    got[r["sku"]] = got.get(r["sku"], 0) + float(r["qty_received"])
                lines += (
                    "".join(
                        f"<tr><td>{_e(ln['sku'])}</td><td>{_e(ln['qty'])}</td>"
                        f"<td>${_e(ln['unit_price'])}</td><td>{_e(got.get(ln['sku'], 0))}</td></tr>"
                        for ln in po["lines"]
                    )
                    + "</table>"
                )
        approvals = "".join(
            f"<div>Approved by <b>{_e(a['by'])}</b> at {_e(a['at'])}. {_e(a['note'])}</div>"
            for a in p.approvals
        )
        actions = ""
        if p.status == "pending":
            note_ph = "Override note (required: this has flags)" if p.flags else "Note (optional)"
            actions = (
                f"<h2>Your decision</h2><div class='card'>"
                f"<p>Needs {p.needed} approval(s); has {len(p.approvals)}.</p>"
                f"<form method='post' action='/p/{_e(p.id)}/approve'>"
                f"<input type='hidden' name='csrf' value='{csrf}'>"
                f"<input name='by' placeholder='Your name' required>"
                f"<input name='note' placeholder='{_e(note_ph)}'>"
                f"<button class='b-ok'>Approve</button></form>"
                f"<form method='post' action='/p/{_e(p.id)}/reject'>"
                f"<input type='hidden' name='csrf' value='{csrf}'>"
                f"<input name='by' placeholder='Your name' required>"
                f"<input name='reason' placeholder='Reason' required>"
                f"<button class='b-bad'>Reject</button></form></div>"
            )
        body = (
            "<p><a href='/'>&larr; All proposals</a></p>"
            + (f"<p class='err'>{_e(error)}</p>" if error else "")
            + f"<h1>{_e(p.id)} {_pill(p.status)}</h1>"
            f"<p class='sub'>{_e(p.kind)} for ${_e(p.amount)}. Agent recommends "
            f"{_pill(p.recommendation)}</p>"
            f"<div class='card'><h2 style='margin-top:0'>Checks</h2>{flags}</div>"
            f"<div class='card'><pre style='white-space:pre-wrap;margin:0'>"
            f"{_e(json.dumps({k: v for k, v in pl.items() if k != 'lines'}, indent=2))}"
            f"</pre>{lines}</div>"
            + (f"<div class='card'>{approvals}</div>" if approvals else "")
            + (
                f"<div class='card'>Result: <code>{_e(json.dumps(p.result))}</code></div>"
                if p.result
                else ""
            )
            + actions
            + f"<p class='sub'>Payload hash <code>{_e(p.payload_hash[:16])}</code></p>"
        )
        return _page(p.id, body)

    async def decide(request: Request) -> Response:
        pid = request.path_params["pid"]
        form = await request.form()
        if form.get("csrf") != csrf:
            return Response("bad form token", status_code=403)
        try:
            if request.path_params["action"] == "approve":
                svc.proposals.approve(pid, str(form.get("by", "")), str(form.get("note", "")))
            else:
                svc.proposals.reject(pid, str(form.get("by", "")), str(form.get("reason", "")))
        except ApprovalError as exc:
            from urllib.parse import quote

            return RedirectResponse(f"/p/{pid}?error={quote(str(exc))}", status_code=303)
        return RedirectResponse(f"/p/{pid}", status_code=303)

    return Starlette(
        routes=[
            Route("/", index),
            Route("/p/{pid}", detail),
            Route("/p/{pid}/{action:str}", decide, methods=["POST"]),
        ]
    )


def serve(port: int = 8787) -> None:  # pragma: no cover - wiring only
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="warning")
