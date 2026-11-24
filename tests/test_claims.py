"""Structured answers: the model submits claims through final_answer; the server checks each claim
against its own evidence, feeds precise errors back, and renders the prose."""
import json

from anthropic.types.beta import BetaToolUseBlock
from langchain_core.messages import AIMessage, ToolMessage

from fleet_copilot import config
from fleet_copilot.claims import check_claims, final_answer_schema, render
from fleet_copilot.tools import run_tool

from test_graph import _claude_copilot, _msg
from test_langchain_brain import ScriptedChatModel, _call, _copilot, _last_tool_result


def _final(args, id="fa1"):
    return AIMessage(content="", tool_calls=[{"name": "final_answer", "args": args, "id": id}])


def _lowdisk_answer(messages, value_offset=0.0, cite="snap"):
    res = _last_tool_result(messages)
    f = res["results"][0]
    snap = f["evidence"][-1]
    pct = f["metrics"]["disk_free_pct"] + value_offset
    return _final({
        "lead": f"{res['count']} devices are low on disk space.",
        "claims": [
            {"statement": f"{f['device_id']} has {pct}% free disk", "value": pct, "unit": "%",
             "device_id": f["device_id"], "evidence_ids": [snap if cite == "snap" else "snap:NOPE@x"]},
            {"statement": f"{res['count']} devices are below the 10% threshold", "value": res["count"], "unit": "count",
             "evidence_ids": [res["query_id"]]},
        ],
        "caveats": ["Threshold: latest snapshot free disk below 10%"],
    })


def test_final_answer_is_checked_and_rendered(fleet, state_dir):
    model = ScriptedChatModel(script=[_call("find_low_disk_devices"), _lowdisk_answer])
    c = _copilot(fleet, state_dir, model)
    r = c.ask("Which devices are low on disk?")
    v = r.verification
    assert v["passed"], v
    assert v["answer_format"] == "claims" and v["claims"] == v["claims_valid"] == 2
    assert r.final.startswith("2 devices are low on disk space.")
    assert "- MT7PJB7N5LRE has 2.0% free disk [snap:MT7PJB7N5LRE@" in r.final
    assert "[query:q1]" in r.final and "Notes: Threshold" in r.final


def test_value_not_in_its_evidence_is_fed_back_and_fixed(fleet, state_dir):
    def wrong_attribution(messages):
        res = _last_tool_result(messages)
        f, g = res["results"][0], res["results"][1]
        # g's figure cited with f's snapshot: the number exists this turn, but not in the cited record
        pct = g["metrics"]["disk_free_pct"]
        return _final({"lead": "Low disk devices.", "claims": [
            {"statement": f"The second device has {pct}% free", "value": pct, "unit": "%",
             "evidence_ids": [f["evidence"][-1]]}]})

    def fixed(messages):
        fb = messages[-1]
        assert isinstance(fb, ToolMessage) and fb.tool_call_id == "fa1"
        assert "it appears in query:q1" in fb.content
        return _lowdisk_answer([m for m in messages if isinstance(m, ToolMessage)][:1])

    model = ScriptedChatModel(script=[_call("find_low_disk_devices"), wrong_attribution, fixed])
    c = _copilot(fleet, state_dir, model)
    r = c.ask("Which devices are low on disk?")
    assert any("FAIL" in t and "claims valid" in t for t in r.trace), r.trace
    assert r.verification["passed"], r.verification


def test_fabricated_value_is_withheld(fleet, state_dir):
    bad = lambda m: _lowdisk_answer(m, value_offset=7.0)
    model = ScriptedChatModel(script=[_call("find_low_disk_devices"), bad, lambda m: bad(
        [x for x in m if isinstance(x, ToolMessage)][:1])])
    c = _copilot(fleet, state_dir, model)
    r = c.ask("Which devices are low on disk?")
    assert not r.verification["passed"]
    assert "not going to guess" in r.final
    assert any("does not match any value" in i for i in r.verification["issues"])


def test_final_answer_mixed_with_data_calls_is_rejected_then_loop_continues(fleet, state_dir):
    mixed = AIMessage(content="", tool_calls=[{"name": "find_low_disk_devices", "args": {}, "id": "c1"},
                                              {"name": "final_answer", "args": {"lead": "x", "claims": []}, "id": "f0"}])
    model = ScriptedChatModel(script=[mixed, _lowdisk_answer])
    c = _copilot(fleet, state_dir, model)
    r = c.ask("Which devices are low on disk?")
    assert r.verification["passed"], r.verification
    errs = [json.loads(m.content) for m in model.seen[1] if isinstance(m, ToolMessage) and m.tool_call_id == "f0"]
    assert "alone" in errs[0]["error"]


def test_invalid_final_answer_arguments_get_feedback(fleet, state_dir):
    model = ScriptedChatModel(script=[_call("find_low_disk_devices"), _final({"lead": "x"}),
                                      lambda m: _lowdisk_answer([x for x in m if isinstance(x, ToolMessage)][:1])])
    c = _copilot(fleet, state_dir, model)
    r = c.ask("Which devices are low on disk?")
    assert "final_answer arguments are invalid" in model.seen[2][-1].content
    assert r.verification["passed"]


def test_claim_checks_device_coverage_and_foreign_ids(fleet, ctx_for):
    ctx = ctx_for("acme-001")
    res, _ = run_tool(ctx, "find_low_disk_devices", {})
    scope = fleet.scope("acme-001")
    other = next(h.device_id for h in scope.devices() if h.device_id not in res_devices(res))
    foreign = next(iter(fleet.foreign_identifiers("acme-001")))
    ans = {"lead": "x", "claims": [
        {"statement": "a", "device_id": other, "evidence_ids": [res["query_id"]]},            # not covered
        {"statement": "b", "device_id": foreign, "evidence_ids": [res["query_id"]]},          # other tenant
        {"statement": "c", "evidence_ids": ["query:q99"]},                                     # not retrieved
    ]}
    issues, stats = check_claims(ans, ctx.ledger, scope, fleet)
    assert stats == {"claims": 3, "claims_valid": 0}
    assert any("does not cover" in i for i in issues) and any("another tenant" in i for i in issues)
    assert any("q99 was not retrieved" in i for i in issues)


def res_devices(res):
    return {f["device_id"] for f in res["results"]}


def test_proposal_claims_can_cite_the_proposal(fleet, ctx_for):
    ctx = ctx_for("acme-001")
    r0, _ = run_tool(ctx, "detect_ram_pressure", {})
    f = next(x for x in r0["results"] if x["status"] == "consistent")
    run_tool(ctx, "create_upgrade_order", {"device_id": f["device_id"], "component": "ram", "spec": "32 GB",
                                           "justification": "x", "evidence_ids": f["evidence"]})
    proposals = [p.to_dict() for p in ctx.proposals]
    ans = {"lead": "Proposed one RAM upgrade.", "claims": [
        {"statement": f"Proposed upgrading {f['device_id']} to 32 GB", "value": 32, "unit": "GB",
         "device_id": f["device_id"], "evidence_ids": ["proposal:P1"]}]}
    issues, _ = check_claims(ans, ctx.ledger, fleet.scope("acme-001"), fleet, proposals)
    assert issues == []
    assert "(P1)" in render(ans)


def test_claude_brain_final_answer_and_tool_result_feedback(fleet, state_dir):
    def final(kw):
        res = json.loads(kw["messages"][-1]["content"][0]["content"])
        return _msg([BetaToolUseBlock(id="fa1", type="tool_use", name="final_answer", input=_claims_from(res))],
                    "tool_use")


    script = [_msg([BetaToolUseBlock(id="tu1", type="tool_use", name="find_low_disk_devices", input={})], "tool_use"),
              final]
    c, client = _claude_copilot(fleet, state_dir, script)
    r = c.ask("Which devices are low on disk?")
    assert r.verification["passed"] and r.verification["answer_format"] == "claims", r.verification


def _claims_from(res):
    f = res["results"][0]
    return {"lead": "Low disk devices.", "claims": [
        {"statement": f"{f['device_id']} has {f['metrics']['disk_free_pct']}% free", "value": f["metrics"]["disk_free_pct"],
         "unit": "%", "device_id": f["device_id"], "evidence_ids": [f["evidence"][-1]]}]}


def test_schema_has_no_refs_and_prose_mode_drops_the_tool(fleet, state_dir, monkeypatch):
    assert "$ref" not in json.dumps(final_answer_schema())
    monkeypatch.setattr(config, "ANSWER_FORMAT", "prose")
    model = ScriptedChatModel(script=[])
    model.bound_tools = []
    _copilot(fleet, state_dir, model)
    assert "final_answer" not in {t["function"]["name"] for t in model.bound_tools}
