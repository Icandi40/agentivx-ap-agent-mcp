"""Process X-Ray: a before and after cycle-time estimate for AP intake.

The step times and waits come from a JSON file of SAMPLE ASSUMPTIONS, not from any
client. The only measured inputs are the agent's hold rate and processing time,
and those come from the synthetic eval set. The output says so in its first line.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def estimate(baseline: dict[str, Any], hold_rate: float, agent_seconds: float) -> dict[str, Any]:
    vol = baseline["monthly_invoices"]
    before_touch = sum(s["touch_min"] for s in baseline["before"]["steps"])
    after_touch = 0.0
    for s in baseline["after"]["steps"]:
        share = hold_rate if s["applies_to"] == "held" else 1 - hold_rate
        after_touch += s["touch_min"] * share
    after_touch += agent_seconds / 60  # machine time, shown for completeness
    before_wait = sum(baseline["before"]["wait_days"].values())
    after_wait = sum(baseline["after"]["wait_days"].values())
    return {
        "label": baseline["label"],
        "monthly_invoices": vol,
        "hold_rate": hold_rate,
        "before_touch_min": before_touch,
        "after_touch_min": after_touch,
        "before_hours_month": before_touch * vol / 60,
        "after_hours_month": after_touch * vol / 60,
        "before_cycle_days": before_wait,
        "after_cycle_days": after_wait,
    }


def render_markdown(est: dict[str, Any], baseline: dict[str, Any]) -> str:
    rows = "\n".join(
        f"| Before | {s['step']} | {s['touch_min']} min |" for s in baseline["before"]["steps"]
    )
    rows += "\n" + "\n".join(
        f"| After | {s['step']} ({'held' if s['applies_to'] == 'held' else 'clean'} invoices) "
        f"| {s['touch_min']} min |"
        for s in baseline["after"]["steps"]
    )
    return f"""**{est["label"]}**

Inputs measured by the eval script: hold rate {est["hold_rate"]:.0%} of invoices, plus
the agent's processing time. Every other number below is a sample assumption from
`data/sample/xray_baseline.json`.

| When | Step | Touch time |
|---|---|---|
{rows}

| Estimate (sample) | Before | After |
|---|---|---|
| Hands-on minutes per invoice | {est["before_touch_min"]:.1f} | {est["after_touch_min"]:.1f} |
| Hands-on hours per month at {est["monthly_invoices"]} invoices | {est["before_hours_month"]:.0f} | {est["after_hours_month"]:.0f} |
| Waiting days from receipt to posted bill | {est["before_cycle_days"]:.1f} | {est["after_cycle_days"]:.1f} |

How to use this with a real client: run a Process X-Ray on two to four weeks of their
actual invoices first, replace the JSON values with what was observed, then rerun.
"""


def load_baseline(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
