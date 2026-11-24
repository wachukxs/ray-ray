"""Structured answers: the model states claims, the server checks them, then renders the prose.

LLM brains finish a turn by calling the `final_answer` tool instead of writing free text:

    {"lead": "Two devices are low on disk.",
     "claims": [{"statement": "MT7PJB7N5LRE has 2.0% free disk", "value": 2.0, "unit": "%",
                 "device_id": "MT7PJB7N5LRE", "evidence_ids": ["snap:MT7PJB7N5LRE@..."]},
                {"statement": "2 devices are below the 10% threshold", "value": 2, "unit": "count",
                 "evidence_ids": ["query:q1"]}],
     "caveats": ["Threshold: latest snapshot free disk < 10%."]}

Each claim is checked against *its own* evidence, which is stricter than checking prose:

* every evidence ID must have been retrieved this turn (or be `proposal:Pn` for a proposal made
  this turn);
* `device_id` must be a device of this tenant that the claim's evidence covers (a snapshot of it,
  a query that returned it, or a proposal that targets it);
* `value` (with its unit) and every quantity written in `statement` must match a number held by
  the claim's evidence. If the number exists elsewhere in the turn, the feedback names the record
  to cite instead, so a revision can fix the attribution rather than guess.

Only then is the prose rendered (lead, one bullet per claim with its citations, caveats), and the
rendered text still goes through the prose verifier (leaks, coverage, uncited facts).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from .data import FleetData, TenantScope
from .guardrails import CITATION_RE, DEVICE_RE, QUANTITY_RE, _matches
from .tools import Ledger, harvest_numbers

FINAL_ANSWER_TOOL = "final_answer"
PROPOSAL_PREFIX = "proposal:"


class Claim(BaseModel):
    statement: str = Field(description="One factual sentence in plain words. No [citations] in the text.")
    value: float | None = Field(None, description="The number the statement asserts, if any (e.g. 2.0 for '2.0% free').")
    unit: Literal["%", "GB", "TB", "cycles", "count"] | None = Field(
        None, description="Unit of `value`. 'count' for numbers of devices/results.")
    device_id: str | None = Field(None, description="The device the claim is about, if it is about one device.")
    evidence_ids: list[str] = Field(..., min_length=1, description=(
        "IDs returned by tools this turn that support THIS claim: snap:..., query:qN, or proposal:Pn. "
        "The value must appear in these records."))


class StructuredAnswer(BaseModel):
    lead: str = Field(description="One or two sentences that answer the question directly. No numbers or device IDs "
                                  "unless a claim below states and cites them.")
    claims: list[Claim] = Field(description="Every fact the answer relies on, one per item. For 'nothing matched' "
                                            "answers, one claim with value 0, unit 'count', citing the query.")
    caveats: list[str] = Field(default_factory=list, description="Definitions, thresholds, limitations, refusals.")


def final_answer_schema() -> dict:
    """JSON schema for the tool input, with $defs inlined (some providers do not resolve $ref)."""
    schema = StructuredAnswer.model_json_schema()
    defs = schema.pop("$defs", {})

    def inline(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return inline(defs[node["$ref"].split("/")[-1]])
            return {k: inline(v) for k, v in node.items() if k != "title"}
        if isinstance(node, list):
            return [inline(v) for v in node]
        return node

    return inline(schema)


FINAL_ANSWER_DESCRIPTION = (
    "Submit your final answer as structured claims, after all data tool calls have returned. Call it alone. "
    "Every fact goes in `claims` with the evidence IDs that support that specific fact; the server checks each "
    "value against those records and renders the prose for the admin.")


def parse_final_answer(args: dict) -> tuple[dict | None, str | None]:
    try:
        return StructuredAnswer.model_validate(args or {}).model_dump(), None
    except ValidationError as e:
        errs = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors(include_url=False)[:5])
        return None, f"final_answer arguments are invalid: {errs}"


# --------------------------------------------------------------------------- checking

def _records_for(ev_ids: list[str], ledger: Ledger, proposals: dict[str, dict]) -> list[dict]:
    out = []
    for ev in ev_ids:
        if ev in ledger.records:
            out.append(ledger.records[ev])
        elif ev.startswith(PROPOSAL_PREFIX) and ev[len(PROPOSAL_PREFIX):] in proposals:
            p = proposals[ev[len(PROPOSAL_PREFIX):]]
            out.append({"kind": "proposal", "device_id": p.get("target_device"), "employee_id": p.get("target_employee"),
                        "values": sorted(harvest_numbers([p.get("args", {}), p.get("policy", {})]))})
    return out


def _covers(rec: dict, device: str) -> bool:
    if rec["kind"] in ("snapshot", "proposal"):
        return rec.get("device_id") == device
    return device in rec.get("devices", [])


def _where(value: float, unit: str, ledger: Ledger) -> list[str]:
    hits = [k for k, r in ledger.records.items() if _matches(value, unit, r.get("values", []))]
    return sorted(hits, key=lambda k: not k.startswith("query:"))[:3]   # aggregates first: usually the source


def _check_number(i: int, value: float, unit: str, text: str, own: list[dict], ledger: Ledger,
                  cited: list[str]) -> str | None:
    unit = unit.lower()
    if unit == "count":
        unit = "n"
    if _matches(value, unit, [v for r in own for v in r.get("values", [])]):
        return None
    elsewhere = _where(value, unit, ledger)
    if elsewhere:
        return (f"claim {i}: {text} is not in its evidence {cited}; it appears in {', '.join(elsewhere)} "
                f"- cite that record for this claim")
    return f"claim {i}: {text} does not match any value in the evidence retrieved this turn"


def check_claims(answer: dict, ledger: Ledger, scope: TenantScope, fleet: FleetData,
                 proposals: list[dict] | None = None) -> tuple[list[str], dict]:
    props = {p["proposal_id"]: p for p in proposals or []}
    own_devices = {h.device_id for h in scope.devices()}
    foreign = fleet.foreign_identifiers(scope.company_id)
    issues: list[str] = []
    valid = 0
    for i, c in enumerate(answer.get("claims", []), 1):
        before = len(issues)
        ev = c.get("evidence_ids") or []
        unknown = [e for e in ev if e not in ledger.records and not (
            e.startswith(PROPOSAL_PREFIX) and e[len(PROPOSAL_PREFIX):] in props)]
        for e in unknown:
            issues.append(f"claim {i}: evidence {e} was not retrieved this turn")
        own = _records_for([e for e in ev if e not in unknown], ledger, props)
        dev = c.get("device_id")
        if dev:
            if dev in foreign:
                issues.append(f"claim {i}: device {dev} belongs to another tenant")
            elif dev not in own_devices:
                issues.append(f"claim {i}: {dev} is not a device in this fleet")
            elif own and not any(_covers(r, dev) for r in own):
                issues.append(f"claim {i}: its evidence does not cover device {dev}")
        for d in set(DEVICE_RE.findall(c.get("statement", ""))) - {dev}:
            if d in foreign:
                issues.append(f"claim {i}: statement names {d} from another tenant")
            elif d in own_devices and own and not any(_covers(r, d) for r in own):
                issues.append(f"claim {i}: statement names {d} but its evidence does not cover it")
        if c.get("value") is not None and c.get("unit") and own:
            msg = _check_number(i, float(c["value"]), c["unit"], f"value {c['value']:g} {c['unit']}", own, ledger, ev)
            if msg:
                issues.append(msg)
        if own:
            for m in QUANTITY_RE.finditer(CITATION_RE.sub("", c.get("statement", ""))):
                q = float(m.group(1).replace(",", "") + (m.group(2) or ""))
                unit = "cycles" if m.group(3).lower().startswith("cycle") else m.group(3)
                msg = _check_number(i, q, unit, f"'{m.group(0).strip()}'", own, ledger, ev)
                if msg:
                    issues.append(msg)
        valid += len(issues) == before
    n = len(answer.get("claims", []))
    if n == 0:
        issues.append("final_answer has no claims: state each fact as a claim with its evidence IDs "
                      "(for 'nothing matched', one claim with value 0 citing the query)")
    return issues, {"claims": n, "claims_valid": valid}


# --------------------------------------------------------------------------- rendering

def render(answer: dict) -> str:
    lines = [answer.get("lead", "").strip()]
    for c in answer.get("claims", []):
        text = c["statement"].strip().rstrip(".")
        cites = "".join(f"[{e}]" for e in c.get("evidence_ids", []) if not e.startswith(PROPOSAL_PREFIX)
                        and f"[{e}]" not in text)
        props = ", ".join(e[len(PROPOSAL_PREFIX):] for e in c.get("evidence_ids", []) if e.startswith(PROPOSAL_PREFIX))
        lines.append(f"- {text}{' (' + props + ')' if props else ''} {cites}".rstrip())
    caveats = [x.strip() for x in answer.get("caveats", []) if x.strip()]
    if caveats:
        lines.append("Notes: " + " ".join(x if x.endswith(".") else x + "." for x in caveats))
    return "\n".join(l for l in lines if l)
