# Draft LinkedIn post (edit before posting)

I built an accounts payable agent that cannot pay a bill without a person.

It works inside QuickBooks Online through an MCP server. It reads the invoice, matches it against the purchase order and the receiving log, and drafts the bill. Then it stops. Approval lives in a separate program the agent cannot call.

The eval I care most about is the one I did not tune for. On invoice layouts the extractor had never seen, it read every field on only 4 of 30. But across all 90 synthetic invoices, it recommended zero bad ones for approval. When it cannot read something, it holds the invoice for a person.

The first live run against a QuickBooks sandbox found two gaps my mocked tests missed. Both were fixed the same day.

Code, evals and the live run log are public. Link in the comments.

#AIAgents #MCP #ForwardDeployedEngineering #Accounting

---

First comment: https://github.com/Icandi40/agentivx-ap-agent-mcp

Video upload: attach `out/ap-agent-carousel.srt` as the caption file (LinkedIn plays video muted by default).
