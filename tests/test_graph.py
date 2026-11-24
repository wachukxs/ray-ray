"""End-to-end graph behaviour: approval interrupt, execution, rejection, and the Claude
brain's tool loop against a scripted stub client (no network)."""
from types import SimpleNamespace

from anthropic.types.beta import BetaMessage, BetaTextBlock, BetaToolUseBlock, BetaUsage

from fleet_copilot.copilot import FleetCopilot


def _events(c, name):
    return [e for e in c.audit.entries(c.session_id) if e["event"] == name]


def test_actions_wait_for_approval_then_execute_only_approved(fleet, state_dir):
    c = FleetCopilot("acme-001", mode="offline", state_dir=state_dir, fleet=fleet)
    r = c.ask("Open remediation tickets for devices failing screen lock")
    assert r.final is None and len(r.awaiting_approval) == 5
    assert c.store.all() == [] and not _events(c, "action_executed")
    r2 = c.decide(r.thread_id, {"P1": "approve", "P3": "approve"}, approver="alice")
    assert {x["proposal_id"] for x in r2.state["executed"]} == {"P1", "P3"}
    assert len(c.store.all()) == 2
    assert {e["actor"] for e in _events(c, "action_executed")} == {"alice"}
    assert len(_events(c, "proposal_rejected")) == 3          # silence == rejection
    assert c.audit.verify()


def test_reject_all_executes_nothing(fleet, state_dir):
    c = FleetCopilot("initech-003", mode="offline", state_dir=state_dir, fleet=fleet)
    r = c.ask("Flag devices that should be replaced")
    assert r.awaiting_approval
    r2 = c.decide(r.thread_id, {}, approver="bob")
    assert r2.state["executed"] == [] and c.store.all() == []


def test_blocked_question_never_reaches_tools(fleet, state_dir):
    c = FleetCopilot("acme-001", mode="offline", state_dir=state_dir, fleet=fleet)
    r = c.ask("Which devices at Globex are low on disk?")
    assert r.state["guard"]["blocked"] and r.tool_log == []
    assert not _events(c, "tool_call")


# ---------------------------------------------------------------- Claude brain with a stub client

def _msg(content, stop):
    return BetaMessage(id="m", type="message", role="assistant", model="claude-opus-5-5", content=content,
                       stop_reason=stop, stop_sequence=None, usage=BetaUsage(input_tokens=1, output_tokens=1))


class StubClient:
    """Scripted responses; records every request so the test can inspect the loop."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))
        self.messages = SimpleNamespace(parse=self._parse)

    def _parse(self, **kw):
        plan = SimpleNamespace(intent="low_disk", steps=["find low disk devices", "answer"], tools=["find_low_disk_devices"])
        return SimpleNamespace(stop_reason="end_turn", parsed_output=plan)

    def _create(self, **kw):
        self.requests.append(kw)
        step = self.script.pop(0)
        return step(kw) if callable(step) else step


def _claude_copilot(fleet, state_dir, script):
    c = FleetCopilot("acme-001", mode="offline", state_dir=state_dir, fleet=fleet)
    from fleet_copilot.brains.claude import ClaudeBrain
    brain = ClaudeBrain.__new__(ClaudeBrain)
    brain.client, brain.model, brain.effort = StubClient(script), "claude-opus-5-5", "medium"
    from fleet_copilot.tools import TOOLS
    brain.tools = [t.anthropic_schema() for t in TOOLS.values()]
    c.runtime.brain = brain
    from fleet_copilot.graph import build_graph
    c.graph = build_graph(c.runtime)
    return c, brain.client


def _answer_from_tool_result(kw):
    import json
    tool_result = kw["messages"][-1]["content"][0]
    res = json.loads(tool_result["content"])
    f = res["results"][0]
    return _msg([BetaTextBlock(type="text", text=f"{f['device_id']} is low on disk [{f['evidence'][0]}]; "
                                                  f"{res['count']} devices in total [{res['query_id']}].")], "end_turn")


def test_claude_brain_tool_loop_and_grounding(fleet, state_dir):
    script = [_msg([BetaToolUseBlock(id="tu1", type="tool_use", name="find_low_disk_devices", input={})], "tool_use"),
              _answer_from_tool_result]
    c, client = _claude_copilot(fleet, state_dir, script)
    r = c.ask("Which devices are low on disk?")
    assert r.verification["passed"], r.verification
    second = client.requests[1]["messages"]
    assert second[-2]["role"] == "assistant" and second[-2]["content"][0]["type"] == "tool_use"
    assert second[-1]["content"][0]["tool_use_id"] == "tu1"
    assert "company_id" not in client.requests[0]["tools"][0]["input_schema"].get("properties", {})
    assert "Acme" in client.requests[0]["system"] and "find low disk devices" in client.requests[0]["system"]


def test_claude_brain_hallucination_is_caught_and_retried(fleet, state_dir):
    script = [_msg([BetaToolUseBlock(id="tu1", type="tool_use", name="find_low_disk_devices", input={})], "tool_use"),
              _msg([BetaTextBlock(type="text", text="XSU9UUDGU6AC is nearly full [snap:XSU9UUDGU6AC@2026-06-12T00:00:00Z].")],
                   "end_turn"),
              lambda kw: _answer_from_tool_result({"messages": kw["messages"][:-2]})]
    c, client = _claude_copilot(fleet, state_dir, script)
    r = c.ask("Which devices are low on disk?")
    assert "grounding verifier" in client.requests[2]["messages"][-1]["content"]
    assert r.verification["passed"]
    assert len(_events(c, "verification")) == 2


def test_claude_brain_persistent_hallucination_is_withheld(fleet, state_dir):
    bad = _msg([BetaTextBlock(type="text", text="You have 42 devices low on disk.")], "end_turn")
    c, _ = _claude_copilot(fleet, state_dir, [bad, bad])
    r = c.ask("Which devices are low on disk?")
    assert not r.verification["passed"]
    assert "42" not in r.final and "not going to guess" in r.final
