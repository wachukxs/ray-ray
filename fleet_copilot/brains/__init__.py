"""Brains decide *what to do next*; the graph decides *what is allowed*.

Both brains implement:
    plan(state, scope) -> {"intent", "steps", ...}
    act(state, scope) -> {"calls": [{"id","name","args"}]} | {"answer": str}
    revise(state, feedback) -> None          # react to verifier feedback
    tool_results_message(results) -> dict|None
"""
from __future__ import annotations


def make_brain(mode: str):
    if mode == "offline":
        from .offline import OfflineBrain
        return OfflineBrain()
    if mode == "claude":
        from .claude import ClaudeBrain
        return ClaudeBrain()
    raise ValueError(f"unknown mode {mode!r} (expected 'offline' or 'claude')")
