import asyncio
import re
from pathlib import Path

from conftest import CLEAN_INVOICE
from starlette.testclient import TestClient

from ap_agent.review_app import create_app
from ap_agent.server import build_server

ROOT = Path(__file__).resolve().parents[1]


def _tools(svc):
    return {t.name: t for t in asyncio.run(build_server(svc).list_tools())}


def test_no_tool_can_approve(svc):
    names = set(_tools(svc))
    assert not {n for n in names if "approve" in n and n != "post_approved_proposal"}
    assert not {n for n in names if "reject" in n}
    assert {
        "list_vendors",
        "list_open_bills",
        "list_purchase_orders",
        "propose_bill",
        "propose_payment",
        "post_approved_proposal",
    } <= names


def test_read_tools_are_marked_read_only(svc):
    tools = _tools(svc)
    for name in ["list_vendors", "list_open_bills", "list_purchase_orders", "check_invoice"]:
        assert tools[name].annotations.read_only_hint is True
    assert tools["propose_bill"].annotations.read_only_hint is False


def test_mcp_post_without_approval_is_refused(svc):
    server = build_server(svc)
    res = asyncio.run(server.call_tool("propose_bill", {"text": CLEAN_INVOICE}))
    pid = res.structured_content["proposal_id"]
    res = asyncio.run(server.call_tool("post_approved_proposal", {"proposal_id": pid}))
    assert "refused" in res.structured_content
    assert svc.proposals.get(pid).status == "pending"


def test_review_page_approves_with_form_token(svc):
    pid = svc.propose_bill(text=CLEAN_INVOICE)["proposal_id"]
    client = TestClient(create_app(svc))
    page = client.get(f"/p/{pid}").text
    token = re.search(r"name='csrf' value='([^']+)'", page).group(1)

    bad = client.post(f"/p/{pid}/approve", data={"csrf": "wrong", "by": "Jordan"})
    assert bad.status_code == 403

    client.post(f"/p/{pid}/approve", data={"csrf": token, "by": "ap-agent"})
    assert svc.proposals.get(pid).status == "pending"  # agent identity refused

    client.post(f"/p/{pid}/approve", data={"csrf": token, "by": "Jordan Reviewer"})
    assert svc.proposals.get(pid).status == "approved"
    assert "Jordan Reviewer" in client.get(f"/p/{pid}").text
    assert pid in client.get("/").text


def test_no_em_or_en_dashes_in_project_text():
    """House style: no em dashes or en dashes anywhere a person will read."""
    offenders = []
    for path in ROOT.rglob("*"):
        if any(
            part in {".venv", ".git", "dataset", "__pycache__", ".ruff_cache"}
            for part in path.parts
        ):
            continue
        if path.is_file() and path.suffix in {".md", ".py", ".json", ".toml", ".txt", ".html"}:
            text = path.read_text(encoding="utf-8", errors="ignore")
            if chr(0x2014) in text or chr(0x2013) in text:
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
