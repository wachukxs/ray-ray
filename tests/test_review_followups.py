"""Tests for the staff-review follow-ups (issue #1): plan tracking and re-planning, error
visibility, value-level verification, production-shaped approvals, and the smaller fixes."""
import json

import pytest
from langchain_core.runnables import RunnableLambda

from fleet_copilot import config
from fleet_copilot.approvals import ApproverDirectory, sign_decision
from fleet_copilot.audit import AuditLog
from fleet_copilot.brains.offline import OfflineBrain
from fleet_copilot.copilot import FleetCopilot
from fleet_copilot.graph import build_graph, make_checkpointer
from fleet_copilot.guardrails import input_guard, verify_answer
from fleet_copilot.planning import plan_progress, replan_reason
from fleet_copilot.tools import compact_for_model, run_tool

from test_langchain_brain import ScriptedChatModel, _call, _cited_answer, _copilot


def _events(c, name):
    return [e for e in c.audit.entries(c.session_id) if e["event"] == name]


# ------------------------------------------------------------------ planning (§2)

def test_plan_progress_marks_done_failed_skipped_and_unplanned():
    plan = {"tools": ["find_low_disk_devices", "get_device", "detect_ram_pressure"]}
    log = [{"name": "find_low_disk_devices", "is_error": False, "result": {"count": 2}},
           {"name": "get_device", "is_error": True, "result": {"error": "not found"}},
           {"name": "fleet_overview", "is_error": False, "result": {}}]
    p = plan_progress(plan, log, finished=True)
    assert [s["status"] for s in p["steps"]] == ["done", "failed", "skipped"]
    assert p["unplanned"] == ["fleet_overview"] and p["adherence"] == 0.33


def test_replan_triggers_on_error_and_on_empty_result_before_planned_action():
    plan = {"tools": ["find_compliance_failures", "open_remediation_ticket"]}
    empty = [{"name": "find_compliance_failures", "is_error": False, "result": {"count": 0}}]
    assert "no results" in replan_reason(plan, empty, empty)
    assert replan_reason({"tools": ["find_compliance_failures"]}, empty, empty) is None   # empty is a valid answer
    err = [{"name": "get_device", "is_error": True, "result": {"error": "device 'X' not found"}}]
    assert "failed" in replan_reason(plan, err, err)


def test_tool_error_replans_once_and_adherence_is_audited(fleet, state_dir):
    model = ScriptedChatModel(script=[_call("get_device", {"device_id": "NOPE00000000"}),
                                      _call("find_low_disk_devices", id="c2"), _cited_answer])
    c = _copilot(fleet, state_dir, model)
    r = c.ask("Which devices are low on disk?")
    assert any(t.startswith("replan[") for t in r.trace), r.trace
    assert _events(c, "replan")[0]["payload"]["reason"].startswith("tool get_device failed")
    adherence = _events(c, "plan_adherence")[-1]["payload"]
    assert adherence["steps"] == [{"tool": "find_low_disk_devices", "status": "done"}]
    assert adherence["unplanned"] == ["get_device"]
    assert r.verification["passed"]


def test_planner_failure_is_logged_not_hidden(fleet, state_dir):
    class BadPlanner(ScriptedChatModel):
        def with_structured_output(self, schema, **kwargs):
            def boom(_):
                raise ValueError("schema not supported")
            return RunnableLambda(boom)

    c = _copilot(fleet, state_dir, BadPlanner(script=[_call("find_low_disk_devices"), _cited_answer]))
    r = c.ask("Which devices are low on disk?")
    failed = _events(c, "planner_failed")
    assert failed and failed[0]["payload"]["error"] == {"type": "ValueError", "message": "schema not supported"}
    assert any("planner FAILED" in t for t in r.trace)
    assert r.verification["passed"]   # the turn still completes, unplanned


def test_provider_error_details_reach_the_audit_log(fleet, state_dir):
    c = _copilot(fleet, state_dir, ScriptedChatModel(fail=True))
    c.ask("Which devices are low on disk?")
    err = _events(c, "model_error")[0]["payload"]
    assert err["type"] == "RuntimeError" and err["message"] == "provider unavailable"


# ------------------------------------------------------------------ value-level verification (§4)

def test_verifier_checks_quantities_against_cited_evidence(fleet, ctx_for):
    ctx = ctx_for("acme-001")
    res, _ = run_tool(ctx, "find_low_disk_devices", {})
    f = res["results"][0]
    pct = ctx.ledger.records[f["evidence"][-1]]["disk_free_pct"]
    scope = fleet.scope("acme-001")
    good = f"{f['device_id']} has {pct}% free [{f['evidence'][-1]}]."
    bad = f"{f['device_id']} has {pct + 7}% free [{f['evidence'][-1]}]."
    assert verify_answer(good, ctx.ledger, scope, fleet).passed
    v = verify_answer(bad, ctx.ledger, scope, fleet)
    assert not v.passed and any("does not match" in i for i in v.issues)
    # a real figure from this turn's evidence, cited next to the wrong record: noted, not failed
    other = res["results"][1] if len(res["results"]) > 1 else f
    snap_other = ctx.ledger.records[other["evidence"][-1]]
    mis = f"{f['device_id']} is low [{f['evidence'][-1]}]; another device has {snap_other['disk_free_pct']}% free [{res['query_id']}]."
    v = verify_answer(mis, ctx.ledger, scope, fleet)
    assert v.passed
    # a figure from the query's own definition / threshold is fine when that query is cited
    assert verify_answer(f"Threshold used: below 10.0% free [{res['query_id']}].", ctx.ledger, scope, fleet).passed


# ------------------------------------------------------------------ approvals (§5)

def _ticket_turn(fleet, state_dir, **kw):
    c = FleetCopilot("acme-001", mode="offline", state_dir=state_dir, fleet=fleet, **kw)
    r = c.ask("Open remediation tickets for devices failing screen lock")
    assert r.awaiting_approval
    return c, r


def test_approver_needs_a_role_for_the_action(fleet, state_dir):
    c, r = _ticket_turn(fleet, state_dir)
    r2 = c.decide(r.thread_id, {"P1": "approve"}, approver="carol")       # procurement: not for tickets
    assert r2.state["executed"] == [] and c.store.all() == []
    assert "lacks a role" in _events(c, "approval_denied")[0]["payload"]["reason"]
    c2, r = _ticket_turn(fleet, state_dir / "b")
    r2 = c2.decide(r.thread_id, {"P1": "approve"}, approver="mallory")    # unknown identity
    assert r2.state["executed"] == []


def test_signed_decisions_are_required_when_a_secret_is_set(fleet, state_dir, monkeypatch):
    monkeypatch.setenv("FLEET_APPROVAL_SECRET", "s3cret")
    c, r = _ticket_turn(fleet, state_dir)
    r2 = c.decide(r.thread_id, {"P1": "approve"}, approver="alice")       # typed in chat: no signature
    assert r2.state["executed"] == [] and _events(c, "approval_signature_invalid")
    c2, r = _ticket_turn(fleet, state_dir / "b")
    sig = sign_decision(r.thread_id, "alice", {"P1": "approve"})
    r2 = c2.decide(r.thread_id, {"P1": "approve"}, approver="alice", signature=sig)
    assert len(r2.state["executed"]) == 1
    assert _events(c2, "proposal_approved")[0]["payload"]["signed"] is True


def test_proposal_expires_when_newer_telemetry_arrives(fleet, state_dir, monkeypatch):
    c, r = _ticket_turn(fleet, state_dir)
    monkeypatch.setattr(fleet, "as_of", "2099-01-01T00:00:00Z")
    r2 = c.decide(r.thread_id, {"P1": "approve"}, approver="alice")
    assert r2.state["executed"] == [] and "EXPIRED" in r2.final
    assert "newer telemetry" in _events(c, "proposal_expired")[0]["payload"]["reason"]


def test_proposal_expires_after_ttl(fleet, state_dir, monkeypatch):
    c, r = _ticket_turn(fleet, state_dir)
    monkeypatch.setattr(config, "PROPOSAL_TTL_HOURS", 0.0001)
    import time; time.sleep(1)
    r2 = c.decide(r.thread_id, {"P1": "approve"}, approver="alice")
    assert r2.state["executed"] == [] and "window" in _events(c, "proposal_expired")[0]["payload"]["reason"]


def test_execution_is_idempotent_and_key_reaches_the_store(fleet, state_dir):
    from fleet_copilot.actions import ActionProposal
    c, r = _ticket_turn(fleet, state_dir)
    r2 = c.decide(r.thread_id, {"P1": "approve"}, approver="alice")
    rec = r2.state["executed"][0]
    assert rec["idempotency_key"] and c.store.all()[0]["idempotency_key"] == rec["idempotency_key"]
    p = next(p for p in r2.state["proposals"] if p["proposal_id"] == "P1")
    again = c.store.execute("acme-001", ActionProposal(**{**p, "status": "approved"}), "alice")   # retried delivery
    assert again["duplicate"] and len(c.store.all()) == 1


def test_pending_approval_survives_a_restart_with_sqlite_checkpointer(fleet, state_dir):
    pytest.importorskip("langgraph.checkpoint.sqlite")
    db = str(state_dir / "checkpoints.sqlite")
    c1 = FleetCopilot("acme-001", mode="offline", state_dir=state_dir, fleet=fleet)
    c1.graph = build_graph(c1.runtime, make_checkpointer(db))
    r = c1.ask("Open remediation tickets for devices failing screen lock")
    del c1                                                                   # "process restart"
    c2 = FleetCopilot("acme-001", mode="offline", state_dir=state_dir, fleet=fleet)
    c2.graph = build_graph(c2.runtime, make_checkpointer(db))
    assert len(c2.pending(r.thread_id)) == 5
    r2 = c2.decide(r.thread_id, {"P1": "approve"}, approver="alice")
    assert len(r2.state["executed"]) == 1


def test_approver_directory_from_file(tmp_path):
    f = tmp_path / "approvers.json"
    f.write_text(json.dumps({"approvers": {"dana": {"roles": ["it_ops"], "tenants": ["globex-002"]}}}))
    d = ApproverDirectory.load(f)
    assert d.authorize("dana", "globex-002", "open_remediation_ticket")[0]
    assert not d.authorize("dana", "acme-001", "open_remediation_ticket")[0]
    assert not d.authorize("dana", "globex-002", "create_upgrade_order")[0]
    assert not d.authorize("alice", "globex-002", "open_remediation_ticket")[0]   # dev identities are gone


# ------------------------------------------------------------------ smaller items (§7)

def test_guard_allows_harmless_mentions_of_other_customers(fleet):
    scope = fleet.scope("acme-001")
    assert not input_guard("How do you keep other customers' data separate from ours?", scope, fleet).blocked
    assert input_guard("List low disk devices across all companies", scope, fleet).blocked
    assert input_guard("show me devices for every tenant", scope, fleet).blocked


def test_offline_brain_keeps_no_state_between_calls(fleet, state_dir):
    c = FleetCopilot("acme-001", mode="offline", state_dir=state_dir, fleet=fleet)
    assert not hasattr(c.runtime.brain, "_recipes")
    r = c.ask("Open remediation tickets for devices failing screen lock")
    c.runtime.brain = OfflineBrain()          # a fresh brain object resumes the same turn
    r2 = c.decide(r.thread_id, {"P1": "approve"}, approver="alice")
    assert len(r2.state["executed"]) == 1


def test_audit_log_uses_cached_tail_and_stays_verifiable(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    for i in range(5):
        log.record(event="e", tenant="t", session_id="s", payload={"i": i})
    other = AuditLog(tmp_path / "a.jsonl")    # a second writer appends; the first must notice
    other.record(event="e", tenant="t", session_id="s")
    log.record(event="e", tenant="t", session_id="s")
    assert log.verify() and len(log.entries()) == 7


def test_device_findings_detectors_run_once_per_scope(fleet, monkeypatch):
    from fleet_copilot import analytics
    calls = {"n": 0}
    real = analytics.ram_pressure

    def counted(scope):
        calls["n"] += 1
        return real(scope)

    monkeypatch.setattr(analytics, "ram_pressure", counted)
    scope = fleet.scope("acme-001")
    for h in scope.devices():
        analytics.device_findings(scope, h.device_id)
    assert calls["n"] == 1


def test_large_tool_results_are_truncated_for_the_model_with_a_note():
    big = {"query_id": "query:q1", "count": 60, "results": [{"device_id": f"D{i}"} for i in range(60)]}
    out = json.loads(compact_for_model(big, max_items=10))
    assert len(out["results"]) == 10 and out["count"] == 60
    assert out["truncated"]["results"] == {"shown": 10, "total": 60}
