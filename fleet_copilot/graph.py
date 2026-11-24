"""The agent graph (LangGraph).

    START -> guard --blocked--> refuse -> END
               |
               v
            planner -> agent <-> tools          (explicit loop, capped at MAX_AGENT_STEPS)
                         ^        |
                         |        +--tool error / empty result breaks the plan--> replan
                         +---------------------------------------------------------+
                         |
                         v
                      verifier --fail, retries left--> agent (with feedback)
                         |
                         +--pending proposals--> approval (interrupt: human decides) -> execute -> respond -> END
                         +------------------------------------------------------------> respond -> END

The brain (offline or Claude) only chooses tool calls and writes text. Everything that
enforces safety - tenant scoping, argument validation, evidence policy, approval,
grounding verification, auditing - lives in these nodes and in the tool layer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from . import config
from .actions import ActionProposal, ActionStore, evaluate_policy
from .approvals import ApproverDirectory, idempotency_key, verify_signature
from .audit import AuditLog
from .data import FleetData
from .guardrails import input_guard, verify_answer
from .planning import plan_progress, replan_reason
from .tools import Ledger, ToolContext, run_tool


class AgentState(TypedDict, total=False):
    tenant_id: str
    session_id: str
    thread_id: str
    question: str
    history: list[dict]
    guard: dict
    plan: dict
    plan_progress: dict
    replan_reason: str
    replans: int
    messages: list[dict]
    pending_calls: list[dict]
    tool_log: list[dict]
    ledger: dict
    proposals: list[dict]
    steps: int
    draft: str
    verification: dict
    verify_attempts: int
    decisions: dict
    executed: list[dict]
    final: str
    trace: list[str]


@dataclass
class Runtime:
    fleet: FleetData
    brain: Any
    audit: AuditLog
    store: ActionStore
    approvers: ApproverDirectory = field(default_factory=ApproverDirectory.load)


def _ledger(state: AgentState) -> Ledger:
    l = state.get("ledger") or {}
    return Ledger(records=dict(l.get("records", {})), query_seq=l.get("query_seq", 0))


def _ledger_dict(l: Ledger) -> dict:
    return {"records": l.records, "query_seq": l.query_seq}


def _expiry_reason(p: dict, data_as_of: str, now: datetime) -> str | None:
    if p.get("data_as_of") and p["data_as_of"] != data_as_of:
        return f"newer telemetry ({data_as_of}) than the evidence it was proposed on ({p['data_as_of']})"
    if p.get("created_at"):
        age_h = (now - datetime.fromisoformat(p["created_at"])).total_seconds() / 3600
        if age_h > config.PROPOSAL_TTL_HOURS:
            return f"approval came {age_h:.0f}h after the proposal; the window is {config.PROPOSAL_TTL_HOURS:g}h"
    return None


def make_checkpointer(db_path: str | None = None):
    """In-memory by default; SQLite when FLEET_CHECKPOINT_DB is set, so pending approvals survive a
    restart. Production would use the Postgres saver (langgraph-checkpoint-postgres) the same way."""
    db_path = db_path or config.CHECKPOINT_DB
    if not db_path:
        return InMemorySaver()
    try:
        import sqlite3
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ImportError as e:
        raise SystemExit('FLEET_CHECKPOINT_DB needs the durable extra: pip install -e ".[durable]"') from e
    return SqliteSaver(sqlite3.connect(db_path, check_same_thread=False))


def build_graph(rt: Runtime, checkpointer=None):
    def scope_of(state):
        return rt.fleet.scope(state["tenant_id"])

    def audit(state, event, payload=None, evidence=None, actor="agent"):
        rt.audit.record(event=event, tenant=state["tenant_id"], session_id=state["session_id"],
                        actor=actor, payload=payload, evidence=evidence)

    # ------------------------------------------------------------------ nodes
    def guard(state: AgentState) -> dict:
        audit(state, "question", {"question": state["question"], "brain": rt.brain.name}, actor="admin")
        g = input_guard(state["question"], scope_of(state), rt.fleet)
        if g.blocked:
            audit(state, "input_blocked", g.to_dict())
        return {"guard": g.to_dict(), "trace": [f"guard: {'BLOCKED ' + g.category if g.blocked else 'ok'}"],
                "steps": 0, "verify_attempts": 0, "replans": 0, "replan_reason": "", "plan_progress": {},
                "tool_log": [], "proposals": [], "executed": [],
                "ledger": {"records": {}, "query_seq": 0}, "messages": []}

    def refuse(state: AgentState) -> dict:
        final = state["guard"]["reason"]
        audit(state, "answer", {"final": final, "refused": True})
        return {"final": final}

    def planner(state: AgentState) -> dict:
        plan = rt.brain.plan(state, scope_of(state))
        audit(state, "plan", plan)
        trace = [f"plan[{plan.get('intent')}]: " + " -> ".join(plan["steps"])]
        if plan.get("error"):   # structured-output failure: visible, not hidden behind a normal-looking plan
            audit(state, "planner_failed", {"error": plan["error"], "planner": plan.get("planner")})
            trace.append(f"planner FAILED ({plan['error'].get('type')}); continuing unplanned")
        return {"plan": plan, "plan_progress": plan_progress(plan, []), "trace": state["trace"] + trace}

    def replan(state: AgentState) -> dict:
        reason = state["replan_reason"]
        new = rt.brain.replan(state, scope_of(state), reason)
        upd: dict = {"replans": state.get("replans", 0) + 1, "replan_reason": ""}
        if not new:
            upd["trace"] = state["trace"] + [f"replan: kept plan ({reason})"]
            return upd
        audit(state, "replan", {"reason": reason, "old": state.get("plan"), "new": new})
        if new.get("error"):
            audit(state, "planner_failed", {"error": new["error"], "planner": new.get("planner"), "during": "replan"})
            upd["trace"] = state["trace"] + [f"replan FAILED ({new['error'].get('type')}); kept plan"]
            return upd
        upd.update(plan=new, plan_progress=plan_progress(new, state.get("tool_log", [])),
                   trace=state["trace"] + [f"replan[{new.get('intent')}] because {reason}: " + " -> ".join(new["steps"])])
        return upd

    def agent(state: AgentState) -> dict:
        if state.get("steps", 0) >= config.MAX_AGENT_STEPS:
            return {"pending_calls": [], "draft": "I hit the step limit before finishing; the evidence gathered so "
                    "far is in the audit log, but I can't give a complete grounded answer.",
                    "trace": state["trace"] + ["agent: step budget exhausted"]}
        out = rt.brain.act(state, scope_of(state))
        upd: dict = {"steps": state.get("steps", 0) + 1}
        if out.get("error"):   # provider failure: keep the details for debugging live runs
            audit(state, "model_error", out["error"])
        if "messages" in out:
            upd["messages"] = out["messages"]
        if out.get("calls"):
            upd["pending_calls"] = out["calls"]
            upd["trace"] = state["trace"] + [f"agent: call {', '.join(c['name'] for c in out['calls'])}"]
        else:
            upd["pending_calls"] = []
            upd["draft"] = out["answer"]
            upd["trace"] = state["trace"] + ["agent: answer drafted"]
        return upd

    def tools(state: AgentState) -> dict:
        ctx = ToolContext(scope_of(state), state["session_id"], rt.audit, rt.store, _ledger(state),
                          [ActionProposal(**p) for p in state.get("proposals", [])])
        results, trace = [], []
        for c in state["pending_calls"]:
            result, is_err = run_tool(ctx, c["name"], c["args"])
            results.append({"id": c["id"], "name": c["name"], "args": c["args"], "result": result, "is_error": is_err})
            status = result.get("status") or (f"{result['count']} result(s)" if "count" in result else
                                              "error" if is_err else "ok")
            trace.append(f"tool {c['name']}: {status}")
        log = state.get("tool_log", []) + results
        plan = state.get("plan") or {}
        reason = replan_reason(plan, results, log) if state.get("replans", 0) < config.MAX_REPLANS else None
        upd = {"tool_log": log, "pending_calls": [],
               "ledger": _ledger_dict(ctx.ledger), "proposals": [p.to_dict() for p in ctx.proposals],
               "plan_progress": plan_progress(plan, log), "replan_reason": reason or "",
               "trace": state["trace"] + trace}
        msg = rt.brain.tool_results_message(results)   # one message, or one per tool call
        if msg:
            upd["messages"] = state.get("messages", []) + (msg if isinstance(msg, list) else [msg])
        return upd

    def verifier(state: AgentState) -> dict:
        v = verify_answer(state.get("draft", ""), _ledger(state), scope_of(state), rt.fleet, state.get("proposals"))
        audit(state, "verification", v.to_dict(), evidence=v.citations)
        progress = plan_progress(state.get("plan") or {}, state.get("tool_log", []), finished=True)
        audit(state, "plan_adherence", progress)
        upd: dict = {"verification": v.to_dict(), "verify_attempts": state.get("verify_attempts", 0) + 1,
                     "plan_progress": progress,
                     "trace": state["trace"] + [f"verifier: {'pass' if v.passed else 'FAIL ' + '; '.join(v.issues[:3])}"
                                                f" ({v.valid_citations}/{len(v.citations)} citations valid)"]}
        if not v.passed and upd["verify_attempts"] <= config.MAX_VERIFY_RETRIES:
            msg = rt.brain.revise(state, v.issues)
            if msg:
                upd["messages"] = state.get("messages", []) + [msg]
        return upd

    def approval(state: AgentState) -> dict:
        pending = [p for p in state["proposals"] if p["status"] == "pending_approval"]
        decision = interrupt({"type": "approval_required", "proposals": pending, "thread_id": state["thread_id"]})
        decisions = (decision or {}).get("decisions", {})
        approver = (decision or {}).get("approver", "unknown")
        sig_ok, signed = verify_signature(state["thread_id"], approver, decisions, (decision or {}).get("signature"))
        if not sig_ok:   # not from the authenticated approval channel: nothing is approved
            audit(state, "approval_signature_invalid", {"approver": approver, "decisions": decisions}, actor=approver)
        props = []
        for p in state["proposals"]:
            if p["status"] == "pending_approval":
                d = decisions.get(p["proposal_id"], "reject")   # silence is a rejection
                note = "rejected by approver" if d != "approve" else ""
                if d == "approve" and not sig_ok:
                    d, note = "reject", "decision signature missing or invalid"
                if d == "approve":
                    ok, why = rt.approvers.authorize(approver, state["tenant_id"], p["action"])
                    if not ok:
                        audit(state, "approval_denied", {"proposal_id": p["proposal_id"], "reason": why}, actor=approver)
                        d, note = "reject", why
                    else:
                        note = why
                status = "approved" if d == "approve" else "rejected"
                p = {**p, "status": status,
                     "decision": {"approver": approver, "signed": signed, "note": note,
                                  "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}}
                if status == "approved":
                    p["idempotency_key"] = idempotency_key(state["tenant_id"], state["thread_id"], p)
                audit(state, f"proposal_{status}", {"proposal_id": p["proposal_id"], "action": p["action"],
                                                    "signed": signed, "note": note,
                                                    "idempotency_key": p.get("idempotency_key")},
                      evidence=p["evidence"], actor=approver)
            props.append(p)
        return {"proposals": props, "decisions": {"approver": approver, **decisions}}

    def execute(state: AgentState) -> dict:
        scope = scope_of(state)
        retrieved = _ledger(state).snapshot_ids()
        approver = state.get("decisions", {}).get("approver", "unknown")
        executed, props = [], []
        now = datetime.now(timezone.utc)
        for p in state["proposals"]:
            if p["status"] == "approved":
                stale = _expiry_reason(p, scope.as_of, now)
                if stale:   # evidence or approval window no longer valid: re-propose instead of acting
                    audit(state, "proposal_expired", {"proposal_id": p["proposal_id"], "reason": stale})
                    props.append({**p, "status": "expired", "decision": {**p.get("decision", {}), "note": stale}})
                    continue
                policy = evaluate_policy(p["action"], p["args"], scope, retrieved, rt.store)  # re-check at execution
                if policy.allowed:
                    rec = rt.store.execute(scope.company_id, ActionProposal(**p), approver)
                    audit(state, "action_deduplicated" if rec.get("duplicate") else "action_executed", rec,
                          evidence=p["evidence"], actor=approver)
                    executed.append(rec)
                    p = {**p, "status": "executed"}
                else:
                    audit(state, "action_blocked_at_execution", {"proposal_id": p["proposal_id"],
                                                                 "reasons": policy.reasons})
                    p = {**p, "status": "blocked", "policy": policy.to_dict()}
            props.append(p)
        return {"executed": executed, "proposals": props}

    def respond(state: AgentState) -> dict:
        v = state.get("verification") or {}
        draft = state.get("draft", "")
        props = state.get("proposals", [])
        if not v.get("passed"):
            # Never show an ungrounded answer as if it were fact; never pass its proposals to execution.
            draft = ("I couldn't produce an answer that passes evidence verification, so I'm not going to "
                     "guess. Issues: " + "; ".join(v.get("issues", [])[:3]))
            props = [{**p, "status": "blocked"} if p["status"] == "pending_approval" else p for p in props]
        lines = [draft]
        decided = [p for p in props if p["status"] in ("executed", "rejected", "blocked", "expired")]
        if state.get("decisions") and decided:
            lines.append("\nAction outcomes:")
            for p in decided:
                ex = next((e for e in state.get("executed", []) if e["proposal_id"] == p["proposal_id"]), None)
                note = p.get("decision", {}).get("note", "")
                lines.append(f"- {p['proposal_id']} {p['action']}: {p['status'].upper()}"
                             + (f" as {ex['action_id']}" if ex else "")
                             + (f" ({note})" if p["status"] in ("rejected", "expired") and note else ""))
        final = "\n".join(lines)
        audit(state, "answer", {"final": final, "verified": bool(v.get("passed"))}, evidence=v.get("citations"))
        return {"final": final, "proposals": props}

    # ------------------------------------------------------------------ routing
    def after_guard(state):
        return "refuse" if state["guard"]["blocked"] else "planner"

    def after_agent(state):
        return "tools" if state.get("pending_calls") else "verifier"

    def after_tools(state):
        return "replan" if state.get("replan_reason") else "agent"

    def after_verifier(state):
        v = state["verification"]
        if not v["passed"] and state["verify_attempts"] <= config.MAX_VERIFY_RETRIES and rt.brain.name != "offline":
            return "agent"
        if v["passed"] and any(p["status"] == "pending_approval" for p in state.get("proposals", [])):
            return "approval"
        return "respond"

    g = StateGraph(AgentState)
    for name, fn in [("guard", guard), ("refuse", refuse), ("planner", planner), ("agent", agent),
                     ("tools", tools), ("replan", replan), ("verifier", verifier), ("approval", approval),
                     ("execute", execute), ("respond", respond)]:
        g.add_node(name, fn)
    g.add_edge(START, "guard")
    g.add_conditional_edges("guard", after_guard, ["refuse", "planner"])
    g.add_edge("refuse", END)
    g.add_edge("planner", "agent")
    g.add_conditional_edges("agent", after_agent, ["tools", "verifier"])
    g.add_conditional_edges("tools", after_tools, ["replan", "agent"])
    g.add_edge("replan", "agent")
    g.add_conditional_edges("verifier", after_verifier, ["agent", "approval", "respond"])
    g.add_edge("approval", "execute")
    g.add_edge("execute", "respond")
    g.add_edge("respond", END)
    return g.compile(checkpointer=checkpointer or make_checkpointer())
