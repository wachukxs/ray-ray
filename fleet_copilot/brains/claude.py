"""Claude-backed brain (official `anthropic` SDK, manual tool-use loop driven by the graph).

* plan():   one structured-output call -> explicit Plan (intent, steps, tools), stored in state.
* act():    one Messages call with the tool catalog. Returns tool calls or the final answer.
            The graph executes the tools (tenant-scoped, audited) and feeds results back.
* revise(): verifier feedback is appended as a user turn; the model must fix citations.

Determinism note: current Claude models do not accept `temperature`; run-to-run variance is
bounded by the tool layer (all numbers come from deterministic tools), the verifier, and
the evidence policy. The offline brain is the fully deterministic baseline.
"""
from __future__ import annotations

import anthropic
from .. import config
from ..data import TenantScope
from ..tools import TOOLS, compact_for_model
from .prompts import (Plan, error_info, plan_to_state, planner_prompt, replan_prompt, revise_prompt,
                      system_prompt)

def _dump(block) -> dict:
    return block.model_dump(mode="json", exclude_none=True)


class ClaudeBrain:
    name = "claude"

    def __init__(self, model: str = config.CLAUDE_MODEL, effort: str = config.CLAUDE_EFFORT):
        self.client = anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.tools = [t.anthropic_schema() for t in TOOLS.values()]

    # ------------------------------------------------------------------ planning
    def plan(self, state: dict, scope: TenantScope) -> dict:
        return self._plan(scope, state["question"])

    def replan(self, state: dict, scope: TenantScope, reason: str) -> dict:
        return self._plan(scope, replan_prompt(state, reason))

    def _plan(self, scope: TenantScope, request: str) -> dict:
        try:
            resp = self.client.messages.parse(
                model=self.model,
                max_tokens=4000,
                output_config={"effort": "low"},
                system=planner_prompt(scope),
                messages=[{"role": "user", "content": request}],
                output_format=Plan,
            )
        except anthropic.APIError as e:
            return plan_to_state(None, self.model, error_info(e))
        if resp.stop_reason == "refusal" or resp.parsed_output is None:
            return plan_to_state(None, self.model, {"type": "NoPlan", "message": f"stop_reason={resp.stop_reason}"})
        return plan_to_state(resp.parsed_output, self.model)

    # ------------------------------------------------------------------ acting
    def initial_messages(self, state: dict) -> list[dict]:
        msgs: list[dict] = []
        for turn in state.get("history", [])[-4:]:
            msgs.append({"role": "user", "content": turn["question"]})
            msgs.append({"role": "assistant", "content": turn["answer"]})
        msgs.append({"role": "user", "content": state["question"]})
        return msgs

    def act(self, state: dict, scope: TenantScope) -> dict:
        messages = state.get("messages") or self.initial_messages(state)
        try:
            resp = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                output_config={"effort": self.effort},
                system=system_prompt(state, scope),
                tools=self.tools,
                messages=messages,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.RateLimitError as e:
            return {"answer": f"The model is rate-limited right now ({e.status_code}); please retry.", "messages": messages,
                    "error": error_info(e)}
        except anthropic.APIStatusError as e:
            return {"answer": f"Model call failed ({e.status_code}); no answer produced.", "messages": messages,
                    "error": error_info(e)}
        except anthropic.APIConnectionError as e:
            return {"answer": "Could not reach the model API; no answer produced.", "messages": messages,
                    "error": error_info(e)}

        content = [_dump(b) for b in resp.content]
        messages = messages + [{"role": "assistant", "content": content}]
        if resp.stop_reason == "refusal":
            return {"answer": "The model declined this request.", "messages": messages}
        calls = [{"id": b.id, "name": b.name, "args": b.input} for b in resp.content if b.type == "tool_use"]
        if calls:
            return {"calls": calls, "messages": messages}
        text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
        if resp.stop_reason == "max_tokens":
            text += "\n\n(Answer truncated at the output limit.)"
        return {"answer": text, "messages": messages}

    def tool_results_message(self, results: list[dict]) -> dict:
        return {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": r["id"], "is_error": r["is_error"],
             "content": compact_for_model(r["result"])}
            for r in results
        ]}

    def revise(self, state: dict, feedback: list[str]) -> dict:
        return {"role": "user", "content": revise_prompt(feedback)}
