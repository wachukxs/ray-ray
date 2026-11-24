"""Plan bookkeeping: adherence tracking and re-plan triggers.

The planner names the tools it expects to use. As the turn runs, each planned tool is marked

    done     called and returned a result
    failed   called, but every call errored
    pending  not called yet (while the turn is running)
    skipped  never called (once the turn has an answer)

Tools the agent called that the plan did not mention are reported as `unplanned`. The graph
records this in state, shows it to LLM brains in the system prompt, and writes the final
adherence to the audit log, so "the agent planned X and did Y" is visible, not assumed.

A re-plan is triggered when a tool errors, or when a read returns nothing while the plan still
has action steps that would need its evidence (the plan's premise no longer holds).
"""
from __future__ import annotations

from .tools import TOOLS


def _calls(tool_log: list[dict], name: str) -> list[dict]:
    return [e for e in tool_log if e["name"] == name]


def plan_progress(plan: dict, tool_log: list[dict], finished: bool = False) -> dict:
    steps = []
    for name in plan.get("tools", []):
        calls = _calls(tool_log, name)
        if any(not c["is_error"] for c in calls):
            status = "done"
        elif calls:
            status = "failed"
        else:
            status = "skipped" if finished else "pending"
        steps.append({"tool": name, "status": status})
    planned = set(plan.get("tools", []))
    unplanned = sorted({e["name"] for e in tool_log} - planned)
    counts = {s: sum(x["status"] == s for x in steps) for s in ("done", "failed", "pending", "skipped")}
    return {"steps": steps, "unplanned": unplanned, **counts,
            "adherence": round(counts["done"] / len(steps), 2) if steps else None}


def replan_reason(plan: dict, new_results: list[dict], tool_log: list[dict]) -> str | None:
    """Why the current plan should be revisited after this batch of tool results, if at all."""
    errors = [r for r in new_results if r["is_error"]]
    if errors:
        e = errors[0]
        return f"tool {e['name']} failed: {e['result'].get('error', 'error')}"
    planned_actions = [t for t in plan.get("tools", []) if TOOLS.get(t) and TOOLS[t].kind == "action"]
    pending_actions = [t for t in planned_actions if not _calls(tool_log, t)]
    for r in new_results:
        if TOOLS.get(r["name"]) and TOOLS[r["name"]].kind != "action" and r["result"].get("count") == 0 and pending_actions:
            return (f"{r['name']} returned no results, but the plan still expects to call "
                    f"{', '.join(pending_actions)}; there is no evidence to act on")
    return None


def render_progress(plan: dict, progress: dict | None) -> str:
    steps = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(plan.get("steps", []))) or "(none)"
    if not progress or not progress.get("steps"):
        return steps
    status = ", ".join(f"{s['tool']}={s['status']}" for s in progress["steps"])
    extra = f"; unplanned so far: {', '.join(progress['unplanned'])}" if progress.get("unplanned") else ""
    return f"{steps}\nPlanned tool status: {status}{extra}"
