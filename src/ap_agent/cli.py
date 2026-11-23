"""Command line for people: run the review page, decide proposals, run the demo pipeline.

ap-agent review                       open the approval page on http://127.0.0.1:8787
ap-agent pending                      list proposals waiting for a decision
ap-agent approve PR-ABC --by "Name" [--note "..."]
ap-agent reject  PR-ABC --by "Name" --reason "..."
ap-agent process FILE                 run the agent flow on one invoice file
ap-agent post PR-ABC                  post an approved proposal to the ledger
ap-agent audit [PR-ABC]               show the audit trail, verify the hash chain
ap-agent reset                        restore the offline sample company
ap-agent qbo-connect                  sign in to a QuickBooks Online SANDBOX company
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys

from ap_agent.proposals import ApprovalError
from ap_agent.service import APService, default_paths


def _print(obj: object) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="ap-agent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("review")
    r.add_argument("--port", type=int, default=8787)
    sub.add_parser("pending")
    a = sub.add_parser("approve")
    a.add_argument("id")
    a.add_argument("--by", required=True)
    a.add_argument("--note", default="")
    j = sub.add_parser("reject")
    j.add_argument("id")
    j.add_argument("--by", required=True)
    j.add_argument("--reason", required=True)
    pr = sub.add_parser("process")
    pr.add_argument("file")
    po = sub.add_parser("post")
    po.add_argument("id")
    au = sub.add_parser("audit")
    au.add_argument("id", nargs="?")
    sub.add_parser("reset")
    sub.add_parser("qbo-connect")
    args = ap.parse_args(argv)

    if args.cmd == "qbo-connect":
        from ap_agent.ledger.qbo_auth import connect

        connect()
        return 0
    if args.cmd == "reset":
        paths = default_paths()
        # Only this state folder's own files. Subfolders (such as a saved live sandbox run
        # kept under .ap-agent-state/) are left alone.
        removed = []
        for f in (paths.ledger, paths.proposals, paths.audit):
            for target in (f, f.with_name(f.name + ".lock"), f.with_suffix(".tmp")):
                if target.is_file():
                    target.unlink()
                    removed.append(target.name)
        if paths.inbox.is_dir():
            shutil.rmtree(paths.inbox)
            removed.append("inbox/")
        print(f"Cleared {', '.join(removed) or 'nothing'} in {paths.home}.")
        print("The sample company reloads on next use. Other folders there were not touched.")
        return 0
    if args.cmd == "review":
        from ap_agent.review_app import serve

        print(f"Review page on http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
        serve(args.port)
        return 0

    svc = APService.from_paths()
    try:
        if args.cmd == "pending":
            for p in svc.proposals.list("pending"):
                flags = ",".join(f["code"] for f in p.flags) or "none"
                print(
                    f"{p.id}  {p.kind:16} ${p.amount:>10}  agent:{p.recommendation:7} flags:{flags}"
                )
        elif args.cmd == "approve":
            p = svc.proposals.approve(args.id, args.by, args.note)
            print(f"{p.id} now {p.status} ({len(p.approvals)}/{p.needed} approvals)")
        elif args.cmd == "reject":
            p = svc.proposals.reject(args.id, args.by, args.reason)
            print(f"{p.id} rejected")
        elif args.cmd == "process":
            import pathlib

            src = pathlib.Path(args.file)
            dest = svc.inbox / src.name
            if src.resolve() != dest.resolve():
                shutil.copyfile(src, dest)
            out = svc.propose_bill(filename=src.name)
            m = out["match"]
            print(f"Outcome: {m['outcome']}  Agent recommends: {m['recommendation']}")
            for f in m["flags"]:
                print(f"  flag {f['code']}: {f['message']}")
            print(
                f"Proposal: {out['proposal_id'] or 'none'}  ({out.get('reason', 'waiting for approval')})"
            )
        elif args.cmd == "post":
            _print(svc.post_approved(args.id))
        elif args.cmd == "audit":
            _ok, msg = svc.audit.verify()
            entries = svc.audit_trail(args.id) if args.id else svc.audit.entries()[-25:]
            for e in entries:
                print(
                    f"{e['ts']}  {e['event']:20} {e['actor']:14} {json.dumps(e['details'])[:110]}"
                )
            print(f"Hash chain: {msg}")
    except (ApprovalError, KeyError, ValueError, FileNotFoundError) as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
