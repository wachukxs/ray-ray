"""Session facade used by the CLI and the eval runner."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from langgraph.types import Command

from . import config
from .actions import ActionStore
from .audit import AuditLog
from .brains import make_brain
from .data import FleetData
from .graph import Runtime, build_graph

_FLEET: FleetData | None = None


def load_fleet() -> FleetData:
    global _FLEET
    if _FLEET is None:
        _FLEET = FleetData.load()
    return _FLEET


@dataclass
class TurnResult:
    thread_id: str
    final: str | None                    # None while waiting for approval
    awaiting_approval: list[dict]        # proposals pending a human decision
    state: dict = field(repr=False, default_factory=dict)

    @property
    def trace(self) -> list[str]:
        return self.state.get("trace", [])

    @property
    def verification(self) -> dict:
        return self.state.get("verification", {})

    @property
    def tool_log(self) -> list[dict]:
        return self.state.get("tool_log", [])

    @property
    def proposals(self) -> list[dict]:
        return self.state.get("proposals", [])


class FleetCopilot:
    def __init__(self, tenant_id: str, mode: str | None = None, session_id: str | None = None,
                 state_dir: Path | None = None, fleet: FleetData | None = None):
        self.fleet = fleet or load_fleet()
        self.fleet.scope(tenant_id)  # validates tenant
        self.tenant_id = tenant_id
        self.mode = mode or config.default_mode()
        self.session_id = session_id or uuid.uuid4().hex[:12]
        state_dir = Path(state_dir or config.STATE_DIR)
        self.audit = AuditLog(state_dir / "audit.jsonl")
        self.store = ActionStore(state_dir / "actions.jsonl")
        self.runtime = Runtime(self.fleet, make_brain(self.mode), self.audit, self.store)
        self.graph = build_graph(self.runtime)
        self.history: list[dict] = []
        self._turn = 0

    def _result(self, thread_id: str) -> TurnResult:
        cfg = {"configurable": {"thread_id": thread_id}}
        snap = self.graph.get_state(cfg)
        values = dict(snap.values)
        pending = []
        for task in snap.tasks:
            for intr in task.interrupts:
                pending = intr.value.get("proposals", [])
        final = values.get("final") if not pending else None
        if final is not None:
            self.history.append({"question": values["question"], "answer": final})
        return TurnResult(thread_id, final, pending, values)

    def ask(self, question: str) -> TurnResult:
        self._turn += 1
        thread_id = f"{self.session_id}-t{self._turn}"
        self.graph.invoke({"tenant_id": self.tenant_id, "session_id": self.session_id, "thread_id": thread_id,
                           "question": question, "history": list(self.history)},
                          {"configurable": {"thread_id": thread_id}, "recursion_limit": 60})
        return self._result(thread_id)

    def decide(self, thread_id: str, decisions: dict[str, str], approver: str) -> TurnResult:
        """Resume an interrupted turn. decisions: {proposal_id: 'approve'|'reject'}."""
        self.graph.invoke(Command(resume={"decisions": decisions, "approver": approver}),
                          {"configurable": {"thread_id": thread_id}, "recursion_limit": 60})
        return self._result(thread_id)
