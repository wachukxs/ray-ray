"""LangChain brain through the full graph, against a scripted chat model (no network, no keys).

The scripted model is a real BaseChatModel subclass, so bind_tools / invoke / message types go
through LangChain exactly as they would for OpenAI, Gemini or Anthropic.
"""
import json
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda

from fleet_copilot import config
from fleet_copilot.brains import make_brain
from fleet_copilot.brains.lc import LangChainBrain
from fleet_copilot.brains.prompts import Plan
from fleet_copilot.copilot import FleetCopilot
from fleet_copilot.graph import build_graph


class ScriptedChatModel(BaseChatModel):
    """Returns scripted replies in order. A reply may be a callable(messages) -> AIMessage."""
    script: list = []
    seen: list = []
    bound_tools: list = []
    fail: bool = False

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        if self.fail:
            raise RuntimeError("provider unavailable")
        self.seen.append(list(messages))
        step = self.script.pop(0)
        msg = step(messages) if callable(step) else step
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def bind_tools(self, tools, **kwargs):
        self.bound_tools.extend(tools)
        return self

    def with_structured_output(self, schema, **kwargs):
        return RunnableLambda(lambda _: Plan(intent="scripted", steps=["use tools", "answer"], tools=["find_low_disk_devices"]))


def _copilot(fleet, state_dir, model, tenant="acme-001"):
    c = FleetCopilot(tenant, mode="offline", state_dir=state_dir, fleet=fleet)
    c.runtime.brain = LangChainBrain(model_name="scripted:test", model=model)
    c.graph = build_graph(c.runtime)
    return c


def _call(name, args=None, id="c1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args or {}, "id": id}])


def _last_tool_result(messages) -> dict:
    return json.loads([m for m in messages if isinstance(m, ToolMessage)][-1].content)


def _cited_answer(messages):
    res = _last_tool_result(messages)
    f = res["results"][0]
    return AIMessage(content=f"{f['device_id']} is low on disk [{f['evidence'][0]}]; "
                             f"{res['count']} devices in total [{res['query_id']}].")


def test_tool_loop_grounded_answer(fleet, state_dir):
    model = ScriptedChatModel(script=[_call("find_low_disk_devices"), _cited_answer])
    c = _copilot(fleet, state_dir, model)
    r = c.ask("Which devices are low on disk?")
    assert r.verification["passed"], r.verification
    assert r.state["plan"]["planner"] == "scripted:test"
    second_call = model.seen[1]
    assert isinstance(second_call[-1], ToolMessage) and second_call[-1].tool_call_id == "c1"
    assert "Acme" in second_call[0].content and "use tools" in second_call[0].content   # system prompt + plan


def test_tools_are_bound_without_tenant_argument(fleet, state_dir):
    model = ScriptedChatModel(script=[AIMessage(content="No tools needed.")])
    _copilot(fleet, state_dir, model)
    names = {t["function"]["name"] for t in model.bound_tools}
    assert len(names) == 17
    for t in model.bound_tools:
        assert "company_id" not in t["function"]["parameters"].get("properties", {})


def test_missing_tool_call_ids_are_assigned_and_paired(fleet, state_dir):
    model = ScriptedChatModel(script=[_call("find_low_disk_devices", id=None), _cited_answer])
    c = _copilot(fleet, state_dir, model)
    r = c.ask("Which devices are low on disk?")
    assert r.verification["passed"]
    tool_msg = [m for m in model.seen[1] if isinstance(m, ToolMessage)][0]
    ai_msg = [m for m in model.seen[1] if isinstance(m, AIMessage)][-1]
    assert tool_msg.tool_call_id and tool_msg.tool_call_id == ai_msg.tool_calls[0]["id"]


def test_hallucination_is_caught_and_retried(fleet, state_dir):
    model = ScriptedChatModel(script=[
        _call("find_low_disk_devices"),
        AIMessage(content="XSU9UUDGU6AC is nearly full [snap:XSU9UUDGU6AC@2026-06-12T00:00:00Z]."),
        _cited_answer,
    ])
    c = _copilot(fleet, state_dir, model)
    r = c.ask("Which devices are low on disk?")
    assert "grounding verifier" in model.seen[2][-1].content
    assert r.verification["passed"]


def test_provider_error_gives_safe_answer(fleet, state_dir):
    c = _copilot(fleet, state_dir, ScriptedChatModel(fail=True))
    r = c.ask("Which devices are low on disk?")
    assert "Model call failed (RuntimeError)" in r.final
    assert r.tool_log == []


def test_actions_pause_for_approval_with_langchain_messages_in_state(fleet, state_dir):
    def ticket(messages):
        f = _last_tool_result(messages)["results"][0]
        return _call("open_remediation_ticket", {"device_id": f["device_id"], "check_id": "screen_lock",
                                                 "note": "fix screen lock", "evidence_ids": f["evidence"]}, id="c2")

    def summary(messages):
        res = json.loads([m for m in messages if isinstance(m, ToolMessage)][0].content)
        return AIMessage(content=f"Proposed a ticket for {res['results'][0]['device_id']} "
                                 f"[{res['results'][0]['evidence'][0]}], awaiting approval.")

    model = ScriptedChatModel(script=[_call("find_compliance_failures", {"check_id": "screen_lock"}), ticket, summary])
    c = _copilot(fleet, state_dir, model)
    r = c.ask("Open a remediation ticket for screen lock")
    assert r.final is None and len(r.awaiting_approval) == 1 and c.store.all() == []
    r2 = c.decide(r.thread_id, {"P1": "approve"}, approver="alice")
    assert len(r2.state["executed"]) == 1 and len(c.store.all()) == 1


def test_langchain_mode_requires_a_model(monkeypatch):
    monkeypatch.setattr(config, "LANGCHAIN_MODEL", None)
    with pytest.raises(ValueError, match="FLEET_COPILOT_LC_MODEL"):
        make_brain("langchain")
