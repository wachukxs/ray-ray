"""Input guard and output verifier.

Input guard (before planning): deterministic checks for cross-tenant references and
attempts to talk the agent out of its controls. It runs before any model sees the text.

Verifier (after the agent answers): checks that the answer is grounded:
  * every citation exists in this turn's evidence ledger,
  * every device / employee the answer names was actually returned by a tool, and is
    covered by a citation (a snapshot of that device, or a query whose results include it),
  * factual answers carry at least one citation,
  * no identifier from another tenant appears anywhere in the output.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from . import config
from .data import FleetData, TenantScope
from .tools import Ledger

CITATION_RE = re.compile(r"\[((?:snap|query):[^\]]+)\]")
DEVICE_RE = re.compile(r"\b[0-9A-Z]{12}\b")
EMPLOYEE_RE = re.compile(r"\bemp-[a-z]+-\d+\b")

BYPASS_PATTERNS = [
    (r"\bignore (all |any )?(previous|prior|above|your|the) (instructions|rules|guardrails|policies)", "prompt_injection"),
    (r"\b(skip|bypass|without|no need for|don'?t need|do not need)\b.{0,30}\b(approval|approve|confirmation|review|sign-?off)", "approval_bypass"),
    (r"\bauto-?(approve|execute)\b", "approval_bypass"),
    (r"\b(pre-?approved|already approved|i approve in advance)\b", "approval_bypass"),
    (r"\b(you are now|act as|pretend (to be|you are))\b.{0,40}\b(admin|administrator|root|system|developer|another (company|tenant))", "role_escalation"),
    (r"\b(disable|turn off|delete|erase|clear|wipe)\b.{0,20}\b(guardrails?|audit|logs?|policy|policies)\b", "control_tampering"),
    (r"\b(all|every|other|another) (companies|company|tenants?|customers?|organi[sz]ations?)\b", "cross_tenant"),
    (r"\bsystem prompt\b", "prompt_injection"),
]


@dataclass
class GuardResult:
    blocked: bool
    category: str | None = None
    reason: str | None = None
    matches: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def input_guard(text: str, scope: TenantScope, fleet: FleetData) -> GuardResult:
    low = text.lower()
    # Other tenants by name or ID (device serials, employee IDs, hostnames, company IDs).
    foreign = fleet.foreign_identifiers(scope.company_id)
    hits = []
    for cid, name in config.COMPANY_NAMES.items():
        if cid != scope.company_id and re.search(rf"\b{re.escape(name.lower())}\b", low):
            hits.append(name)
    for token in set(re.findall(r"[\w.-]+", text)):
        if token in foreign or token.upper() in foreign:
            hits.append(token)
    if hits:
        return GuardResult(True, "cross_tenant",
                           f"Your session is scoped to {scope.company_name}. I can't access or act on data belonging "
                           f"to another organization.", sorted(set(hits)))
    for pattern, category in BYPASS_PATTERNS:
        m = re.search(pattern, low)
        if m:
            msg = {
                "approval_bypass": "Every state-changing action requires explicit human approval; that can't be skipped "
                                   "or granted in advance from chat.",
                "cross_tenant": f"Your session is scoped to {scope.company_name}; I can't query other organizations.",
                "control_tampering": "Guardrails and the audit log can't be changed from the copilot.",
            }.get(category, "That request tries to override the copilot's operating rules, so I won't follow it.")
            return GuardResult(True, category, msg, [m.group(0)])
    return GuardResult(False)


@dataclass
class Verification:
    passed: bool
    issues: list[str]
    citations: list[str]
    valid_citations: int
    devices_mentioned: list[str]
    foreign_leaks: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def extract_citations(text: str) -> list[str]:
    out = []
    for group in CITATION_RE.findall(text):
        out += [c.strip() for c in re.split(r"[,;]\s*", group) if c.strip()]
    return out


def verify_answer(answer: str, ledger: Ledger, scope: TenantScope, fleet: FleetData) -> Verification:
    issues: list[str] = []
    cites = extract_citations(answer)
    valid = [c for c in cites if c in ledger.records]
    for c in cites:
        if c not in ledger.records:
            issues.append(f"citation {c} does not match any evidence retrieved this turn")

    # Cross-tenant leak check over the raw text (identifiers of any other tenant).
    foreign = fleet.foreign_identifiers(scope.company_id)
    leaks = sorted({t for t in re.findall(r"[\w.-]+", answer) if t in foreign})
    for t in leaks:
        issues.append(f"answer contains identifier {t} from another tenant")

    own_devices = {h.device_id for h in scope.devices()}
    mentioned = sorted({d for d in DEVICE_RE.findall(answer) if d in own_devices})
    seen = ledger.devices()
    cited_snap_devices = {ledger.records[c]["device_id"] for c in valid if ledger.records[c]["kind"] == "snapshot"}
    cited_query_devices = {d for c in valid if ledger.records[c]["kind"] == "query" for d in ledger.records[c]["devices"]}
    for d in mentioned:
        if d not in seen:
            issues.append(f"device {d} is mentioned but no tool returned it this turn")
        elif d not in cited_snap_devices | cited_query_devices:
            issues.append(f"device {d} is mentioned without a citation covering it")
    for e in sorted(set(EMPLOYEE_RE.findall(answer))):
        if e not in ledger.employees() and e not in foreign:
            issues.append(f"employee {e} is mentioned but no tool returned it this turn")

    has_facts = bool(mentioned) or bool(re.search(r"\d", CITATION_RE.sub("", answer)))
    if has_facts and not valid:
        issues.append("answer states facts but cites no evidence")
    return Verification(not issues, issues, cites, len(valid), mentioned, leaks)
