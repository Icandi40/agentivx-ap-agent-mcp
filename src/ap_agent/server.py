"""MCP server for the AP agent.

Read tools query the system of record. Write tools never write: they file a
proposal that a person approves in the review page (`ap-agent review`) or the CLI.
`post_approved_proposal` executes a proposal only after that approval, and refuses
otherwise. There is deliberately no approve tool.

Run with `ap-agent-mcp` (stdio). Point any MCP client, such as Claude Desktop or
Claude Code, at it.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from ap_agent.service import APService

INSTRUCTIONS = """\
You are an accounts payable assistant working inside the company's books (a sandbox).
For each invoice: call check_invoice, explain the outcome and any flags in plain words,
then call propose_bill. You cannot approve anything. Tell the person the proposal id and
that it is waiting in the review page. After they approve it, call post_approved_proposal.
Never ask for or repeat bank account numbers, card numbers, or passwords.
"""

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
PROPOSE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)
EXECUTE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True)


def build_server(service: APService | None = None) -> MCPServer:
    svc = service or APService.from_paths()
    mcp = MCPServer(name="ap-agent", instructions=INSTRUCTIONS, version="0.1.0")

    @mcp.tool(annotations=READ)
    def list_vendors() -> list[dict[str, Any]]:
        """List active vendors in the system of record."""
        return svc.vendors()

    @mcp.tool(annotations=READ)
    def list_open_bills(vendor_id: str | None = None) -> list[dict[str, Any]]:
        """List bills with an unpaid balance, optionally for one vendor."""
        return svc.open_bills(vendor_id)

    @mcp.tool(annotations=READ)
    def list_purchase_orders(status: str = "Open") -> list[dict[str, Any]]:
        """List purchase orders. status is Open or Closed; pass an empty string for all."""
        return svc.purchase_orders(status or None)

    @mcp.tool(annotations=READ)
    def get_purchase_order(po_number: str) -> dict[str, Any]:
        """One purchase order with its lines and what the receiving log says arrived."""
        return svc.purchase_order(po_number) or {"error": f"{po_number} not found"}

    @mcp.tool(annotations=READ)
    def list_inbox() -> list[str]:
        """Invoice files waiting in the inbox folder."""
        return sorted(p.name for p in svc.inbox.iterdir() if p.is_file())

    @mcp.tool(annotations=READ)
    def check_invoice(text: str | None = None, filename: str | None = None) -> dict[str, Any]:
        """Extract fields from an invoice (pasted text, or a file in the inbox) and run the
        three-way match against the PO and receiving log. Writes nothing to the ledger."""
        return svc.check_invoice(text=text, filename=filename)

    @mcp.tool(annotations=PROPOSE)
    def propose_bill(text: str | None = None, filename: str | None = None) -> dict[str, Any]:
        """Draft a bill from an invoice and queue it for human approval. Does NOT post it."""
        return svc.propose_bill(text=text, filename=filename)

    @mcp.tool(annotations=PROPOSE)
    def propose_payment(bill_id: str, amount: str, pay_date: str) -> dict[str, Any]:
        """Queue a scheduled payment of an open bill for human approval. Does NOT pay.
        pay_date is YYYY-MM-DD. Uses the sandbox company's own bank account reference."""
        return svc.propose_payment(bill_id=bill_id, amount=amount, pay_date=pay_date)

    @mcp.tool(annotations=READ)
    def get_proposal(proposal_id: str) -> dict[str, Any]:
        """Status of a proposal: pending, approved, rejected, executed or failed."""
        return svc.proposal(proposal_id)

    @mcp.tool(annotations=EXECUTE)
    def post_approved_proposal(proposal_id: str) -> dict[str, Any]:
        """Write an APPROVED proposal to the system of record. Refuses unless enough people
        approved it and the payload is unchanged since they did."""
        try:
            return svc.post_approved(proposal_id)
        except Exception as exc:  # the refusal reason is the useful answer
            return {"id": proposal_id, "refused": str(exc)}

    @mcp.tool(annotations=READ)
    def get_audit_trail(proposal_id: str) -> list[dict[str, Any]]:
        """Every audit log entry for a proposal and the invoice behind it."""
        return svc.audit_trail(proposal_id)

    return mcp


def main() -> None:  # pragma: no cover - wiring only
    build_server().run("stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
