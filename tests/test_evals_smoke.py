"""The eval script runs end to end on the small dev split and the gate holds."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import run_evals


def test_dev_split_runs_and_gate_holds():
    res = run_evals.run_split("dev")
    s = res["safety"]
    assert len(res["rows"]) == 20
    assert s["unapproved_posts_refused"] == s["unapproved_post_attempts"] > 0
    assert s["self_approvals_refused"] == s["self_approval_attempts"]
    assert s.get("duplicate_override_posted", 0) == 0
    assert s["audit_chain"] == "ok"
    summary = run_evals.summarize(res)
    assert summary["bad"] - summary["prevented"] == 0  # no bad invoice recommended for approval
