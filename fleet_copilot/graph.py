"""The agent graph (LangGraph).

    START -> guard --blocked--> refuse -> END
               |
               v
            planner -> agent <-> tools          (explicit loop, capped at MAX_AGENT_STEPS)
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

from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from . import config
from .actions import ActionProposal, ActionStore, evaluate_policy
from .audit import AuditLog
from .data import FleetData
from .guardrails import input_guard, verify_answer
from .tools import Ledger, ToolContext, run_tool


class AgentState(TypedDict, total=False):
    tenant_id: str
    session_id: str
    thread_id: str
    question: str
    history: list[dict]
    guard: dict
    plan: dict
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


def _ledger(state: AgentState) -> Ledger:
    l = state.get("ledger") or {}
    return Ledger(records=dict(l.get("records", {})), query_seq=l.get("query_seq", 0))


def _ledger_dict(l: Ledger) -> dict:
    return {"records": l.records, "query_seq": l.query_seq}


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
                "steps": 0, "verify_attempts": 0, "tool_log": [], "proposals": [], "executed": [],
                "ledger": {"records": {}, "query_seq": 0}, "messages": []}

    def refuse(state: AgentState) -> dict:
        final = state["guard"]["reason"]
        audit(state, "answer", {"final": final, "refused": True})
        return {"final": final}

    def planner(state: AgentState) -> dict:
        plan = rt.brain.plan(state, scope_of(state))
        audit(state, "plan", plan)
        return {"plan": plan, "trace": state["trace"] + [f"plan[{plan.get('intent')}]: " + " -> ".join(plan["steps"])]}

    def agent(state: AgentState) -> dict:
        if state.get("steps", 0) >= config.MAX_AGENT_STEPS:
            return {"pending_calls": [], "draft": "I hit the step limit before finishing; the evidence gathered so "
                    "far is in the audit log, but I can't give a complete grounded answer.",
                    "trace": state["trace"] + ["agent: step budget exhausted"]}
        out = rt.brain.act(state, scope_of(state))
        upd: dict = {"steps": state.get("steps", 0) + 1}
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
        upd = {"tool_log": state.get("tool_log", []) + results, "pending_calls": [],
               "ledger": _ledger_dict(ctx.ledger), "proposals": [p.to_dict() for p in ctx.proposals],
               "trace": state["trace"] + trace}
        msg = rt.brain.tool_results_message(results)
        if msg:
            upd["messages"] = state.get("messages", []) + [msg]
        return upd

    def verifier(state: AgentState) -> dict:
        v = verify_answer(state.get("draft", ""), _ledger(state), scope_of(state), rt.fleet)
        audit(state, "verification", v.to_dict(), evidence=v.citations)
        upd: dict = {"verification": v.to_dict(), "verify_attempts": state.get("verify_attempts", 0) + 1,
                     "trace": state["trace"] + [f"verifier: {'pass' if v.passed else 'FAIL ' + '; '.join(v.issues[:3])}"
                                                f" ({v.valid_citations}/{len(v.citations)} citations valid)"]}
        if not v.passed and upd["verify_attempts"] <= config.MAX_VERIFY_RETRIES:
            msg = rt.brain.revise(state, v.issues)
            if msg:
                upd["messages"] = state.get("messages", []) + [msg]
        return upd

    def approval(state: AgentState) -> dict:
        pending = [p for p in state["proposals"] if p["status"] == "pending_approval"]
        decision = interrupt({"type": "approval_required", "proposals": pending})
        decisions = (decision or {}).get("decisions", {})
        approver = (decision or {}).get("approver", "unknown")
        props = []
        for p in state["proposals"]:
            if p["status"] == "pending_approval":
                d = decisions.get(p["proposal_id"], "reject")   # silence is a rejection
                p = {**p, "status": "approved" if d == "approve" else "rejected"}
                audit(state, f"proposal_{p['status']}", {"proposal_id": p["proposal_id"], "action": p["action"]},
                      evidence=p["evidence"], actor=approver)
            props.append(p)
        return {"proposals": props, "decisions": {"approver": approver, **decisions}}

    def execute(state: AgentState) -> dict:
        scope = scope_of(state)
        retrieved = _ledger(state).snapshot_ids()
        approver = state.get("decisions", {}).get("approver", "unknown")
        executed, props = [], []
        for p in state["proposals"]:
            if p["status"] == "approved":
                policy = evaluate_policy(p["action"], p["args"], scope, retrieved, rt.store)  # re-check at execution
                if policy.allowed:
                    rec = rt.store.execute(scope.company_id, ActionProposal(**p), approver)
                    audit(state, "action_executed", rec, evidence=p["evidence"], actor=approver)
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
        decided = [p for p in props if p["status"] in ("executed", "rejected", "blocked")]
        if state.get("decisions") and decided:
            lines.append("\nAction outcomes:")
            for p in decided:
                ex = next((e for e in state.get("executed", []) if e["proposal_id"] == p["proposal_id"]), None)
                lines.append(f"- {p['proposal_id']} {p['action']}: {p['status'].upper()}"
                             + (f" as {ex['action_id']}" if ex else ""))
        final = "\n".join(lines)
        audit(state, "answer", {"final": final, "verified": bool(v.get("passed"))}, evidence=v.get("citations"))
        return {"final": final, "proposals": props}

    # ------------------------------------------------------------------ routing
    def after_guard(state):
        return "refuse" if state["guard"]["blocked"] else "planner"

    def after_agent(state):
        return "tools" if state.get("pending_calls") else "verifier"

    def after_verifier(state):
        v = state["verification"]
        if not v["passed"] and state["verify_attempts"] <= config.MAX_VERIFY_RETRIES and rt.brain.name != "offline":
            return "agent"
        if v["passed"] and any(p["status"] == "pending_approval" for p in state.get("proposals", [])):
            return "approval"
        return "respond"

    g = StateGraph(AgentState)
    for name, fn in [("guard", guard), ("refuse", refuse), ("planner", planner), ("agent", agent),
                     ("tools", tools), ("verifier", verifier), ("approval", approval),
                     ("execute", execute), ("respond", respond)]:
        g.add_node(name, fn)
    g.add_edge(START, "guard")
    g.add_conditional_edges("guard", after_guard, ["refuse", "planner"])
    g.add_edge("refuse", END)
    g.add_edge("planner", "agent")
    g.add_conditional_edges("agent", after_agent, ["tools", "verifier"])
    g.add_edge("tools", "agent")
    g.add_conditional_edges("verifier", after_verifier, ["agent", "approval", "respond"])
    g.add_edge("approval", "execute")
    g.add_edge("execute", "respond")
    g.add_edge("respond", END)
    return g.compile(checkpointer=checkpointer or InMemorySaver())
