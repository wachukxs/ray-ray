"""LLM judge for answer quality (usefulness, prioritisation, caveats).

The structural checks in run.py say whether an answer is *correct and grounded*. They say nothing
about whether it is *useful*: does it lead with the answer, rank what matters, state limitations?
This module asks a judge model to score that on a fixed rubric.

Status: uncalibrated. Before these scores are trusted (or gated on), a person should label a sample
of answers with the same rubric and the judge's agreement with those labels should be reported.
Use a judge model from a different provider/family than the agent where possible, to limit
self-preference.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel, Field

from fleet_copilot import config  # noqa: F401  (loads .env)

RUBRIC = """You grade answers from an IT fleet assistant. The admin asked a question; the assistant answered
using tool results (summarised below). Grade ONLY on these criteria, 1 (poor) to 5 (excellent):

- usefulness: answers the actual question directly, first; an admin could act on it.
- prioritisation: when several findings exist, the most important/urgent come first or are flagged;
  for a single fact, being concise counts as 5.
- caveats: states relevant limitations or definitions (thresholds used, data window, missing data,
  why something was refused) without padding. If no caveat is relevant, 5.

Do not grade factual correctness or citations (checked elsewhere). Do not reward length.

Question: {question}
Tools called: {evidence}
Answer:
{answer}"""


class JudgeScore(BaseModel):
    usefulness: int = Field(ge=1, le=5)
    prioritisation: int = Field(ge=1, le=5)
    caveats: int = Field(ge=1, le=5)
    rationale: str = Field(description="one or two sentences")


def judge_results(results: list[dict], model: str, workers: int = 4) -> dict:
    from langchain.chat_models import init_chat_model
    llm = init_chat_model(model).with_structured_output(JudgeScore)

    def one(r: dict) -> None:
        if not r.get("answer"):
            return
        try:
            s = llm.invoke(RUBRIC.format(question=r["question"], answer=r["answer"][:6000],
                                         evidence=json.dumps(r.get("evidence_summary", []))[:3000]))
            if isinstance(s, dict):
                s = JudgeScore(**s)
            r["judge"] = {**s.model_dump(), "overall": round((s.usefulness + s.prioritisation + s.caveats) / 3, 2)}
        except Exception as e:   # a judge failure must not fail the eval run
            r["judge"] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}

    with ThreadPoolExecutor(max_workers=max(workers, 1)) as pool:
        list(pool.map(one, results))
    scored = [r["judge"] for r in results if "overall" in r.get("judge", {})]
    mean = lambda k: round(sum(s[k] for s in scored) / len(scored), 2) if scored else None
    return {"judge_model": model, "answers_scored": len(scored),
            "errors": sum("error" in r.get("judge", {}) for r in results),
            "usefulness": mean("usefulness"), "prioritisation": mean("prioritisation"),
            "caveats": mean("caveats"), "overall": mean("overall"),
            "lowest": sorted(((r["judge"]["overall"], r["id"]) for r in results if "overall" in r.get("judge", {})))[:5],
            "calibration": "none yet: needs human labels on a sample"}
