"""Provider-agnostic brain built on LangChain chat models.

Select any provider LangChain supports with one setting, e.g.
    FLEET_COPILOT_MODE=langchain
    FLEET_COPILOT_LC_MODEL=openai:<model>        (needs OPENAI_API_KEY + langchain-openai)
    FLEET_COPILOT_LC_MODEL=google_genai:<model>  (needs GOOGLE_API_KEY + langchain-google-genai)
    FLEET_COPILOT_LC_MODEL=anthropic:<model>     (needs ANTHROPIC_API_KEY + langchain-anthropic)

Same contract and the same prompts as the Claude brain; the graph, tools, evidence policy,
approval interrupt and verifier are untouched, so every provider is held to the same controls
and can be compared on the same eval suite.

Messages are kept in graph state as plain dicts (`messages_to_dict`) so the LangGraph
checkpointer can serialise them for every provider.
"""
from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (AIMessage, HumanMessage, SystemMessage, ToolMessage,
                                     messages_from_dict, messages_to_dict)
from langchain_core.utils.function_calling import convert_to_openai_tool

from .. import config
from ..data import TenantScope
from ..tools import TOOLS, compact_for_model
from .prompts import (Plan, error_info, plan_to_state, planner_prompt, replan_prompt, revise_prompt,
                      system_prompt)


def _init_model(model_name: str | None) -> BaseChatModel:
    if not model_name:
        raise ValueError("langchain mode needs FLEET_COPILOT_LC_MODEL, e.g. 'openai:<model>', "
                         "'google_genai:<model>' or 'anthropic:<model>'")
    from langchain.chat_models import init_chat_model
    kwargs = {}
    if config.LANGCHAIN_TEMPERATURE is not None:   # optional: some current models reject temperature
        kwargs["temperature"] = config.LANGCHAIN_TEMPERATURE
    return init_chat_model(model_name, **kwargs)


class LangChainBrain:
    name = "langchain"

    def __init__(self, model_name: str | None = None, model: BaseChatModel | None = None):
        model_name = model_name or config.LANGCHAIN_MODEL
        self.llm = model if model is not None else _init_model(model_name)
        self.model_name = model_name or type(self.llm).__name__
        self.tools = [convert_to_openai_tool(t.anthropic_schema()) for t in TOOLS.values()]
        self.llm_with_tools = self.llm.bind_tools(self.tools)

    # ------------------------------------------------------------------ planning
    def plan(self, state: dict, scope: TenantScope) -> dict:
        return self._plan(scope, state["question"])

    def replan(self, state: dict, scope: TenantScope, reason: str) -> dict:
        return self._plan(scope, replan_prompt(state, reason))

    def _plan(self, scope: TenantScope, request: str) -> dict:
        try:
            p = self.llm.with_structured_output(Plan).invoke([SystemMessage(planner_prompt(scope)), HumanMessage(request)])
            if isinstance(p, dict):
                p = Plan(**p)
            if p is None:
                return plan_to_state(None, self.model_name, {"type": "EmptyStructuredOutput",
                                                             "message": "model returned no parsable plan"})
        except Exception as e:   # provider errors differ by package; planning is best-effort, but never silent
            return plan_to_state(None, self.model_name, error_info(e))
        return plan_to_state(p, self.model_name)

    # ------------------------------------------------------------------ acting
    def _initial(self, state: dict) -> list[dict]:
        msgs = []
        for turn in state.get("history", [])[-4:]:
            msgs += [HumanMessage(turn["question"]), AIMessage(turn["answer"])]
        msgs.append(HumanMessage(state["question"]))
        return messages_to_dict(msgs)

    def act(self, state: dict, scope: TenantScope) -> dict:
        stored = state.get("messages") or self._initial(state)
        try:
            resp = self.llm_with_tools.invoke([SystemMessage(system_prompt(state, scope)), *messages_from_dict(stored)])
        except Exception as e:  # each provider package raises its own error types
            return {"answer": f"Model call failed ({type(e).__name__}); no answer produced.", "messages": stored,
                    "error": error_info(e)}

        # Some providers omit tool-call ids; assign stable ones so tool results can be paired.
        if resp.tool_calls:
            resp.tool_calls = [{**tc, "id": tc.get("id") or f"call_{len(stored)}_{i}"}
                               for i, tc in enumerate(resp.tool_calls)]
        stored = stored + messages_to_dict([resp])
        calls = [{"id": tc["id"], "name": tc["name"], "args": tc["args"]} for tc in resp.tool_calls]
        if calls:
            return {"calls": calls, "messages": stored}
        text = resp.text.strip()
        meta = resp.response_metadata or {}
        if (meta.get("finish_reason") or meta.get("stop_reason")) in ("length", "max_tokens", "MAX_TOKENS"):
            text += "\n\n(Answer truncated at the output limit.)"
        return {"answer": text, "messages": stored}

    def tool_results_message(self, results: list[dict]) -> list[dict]:
        return messages_to_dict([
            ToolMessage(content=compact_for_model(r["result"]), tool_call_id=r["id"],
                        status="error" if r["is_error"] else "success")
            for r in results
        ])

    def revise(self, state: dict, feedback: list[str]) -> dict:
        return messages_to_dict([HumanMessage(revise_prompt(feedback))])[0]
