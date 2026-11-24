"""State-changing actions: proposal objects, the evidence policy, and the (mock) executor.

Lifecycle:  propose -> policy check -> human approval (graph interrupt) -> policy re-check -> execute

* Action tools never execute. They create an `ActionProposal` with justification and
  cited evidence. A proposal that fails the evidence policy is refused on the spot.
* The policy is server-side and data-driven: it re-runs the analytics detectors against
  the tenant's telemetry, so the LLM cannot talk its way into an action. Citing evidence
  is necessary (cited snapshots must have been retrieved this session and belong to the
  target) but never sufficient.
* Execution happens only in the graph's `execute_actions` node, only for proposals a
  human approved, and re-checks the policy first (time-of-check/time-of-use).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import analytics, config
from .data import NotFoundInTenant, TenantScope

ACTIONS = {
    "create_upgrade_order": "Order a hardware upgrade (ram | storage | battery) for a device.",
    "open_remediation_ticket": "Open an IT remediation ticket for a failing compliance check on a device.",
    "flag_device_for_replacement": "Flag a device for replacement in the next refresh cycle.",
    "notify_employee": "Send an employee a message about an issue on their device.",
}
UPGRADE_COMPONENTS = {"ram", "storage", "battery"}


@dataclass
class ActionProposal:
    proposal_id: str
    action: str
    args: dict
    justification: str
    evidence: list[str]
    target_device: str | None = None
    target_employee: str | None = None
    policy: dict = field(default_factory=dict)
    status: str = "pending_approval"   # pending_approval | approved | rejected | executed | blocked | expired
    created_at: str = ""               # wall clock, for the approval TTL
    data_as_of: str = ""               # telemetry as-of the proposal was made on; newer data => stale
    idempotency_key: str | None = None # set on approval; carried to the downstream system
    decision: dict = field(default_factory=dict)   # approver, signed, authorization reason

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PolicyResult:
    allowed: bool
    reasons: list[str]
    criteria_evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_gb(spec: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(tb|gb)", spec.lower())
    if not m:
        return None
    return float(m.group(1)) * (1024 if m.group(2) == "tb" else 1)


def _is_soldered(scope: TenantScope, device_id: str) -> bool:
    """Apple-silicon Macs have soldered RAM and storage: not field-upgradable."""
    return scope.device(device_id).latest.processor.lower().startswith("apple")


def evaluate_policy(action: str, args: dict, scope: TenantScope, retrieved: set[str],
                    store: "ActionStore | None" = None) -> PolicyResult:
    """Decide whether `action(args)` is justified by this tenant's telemetry."""
    reasons: list[str] = []
    if action not in ACTIONS:
        return PolicyResult(False, [f"unknown action {action!r}"])

    # 1. Target must exist inside the tenant (same error whether foreign or nonexistent).
    try:
        if action == "notify_employee":
            target_devices = [h.device_id for h in scope.devices_of(args.get("employee_id", ""))]
        else:
            target_devices = [scope.device(args.get("device_id", "")).device_id]
    except NotFoundInTenant as e:
        return PolicyResult(False, [str(e)])

    # 2. Cited evidence must be real, retrieved during this session, and about the target.
    cited = list(args.get("evidence_ids") or [])
    if not cited:
        return PolicyResult(False, ["no evidence cited: actions require cited telemetry snapshots"])
    for ev in cited:
        if ev not in retrieved:
            reasons.append(f"evidence {ev} was not retrieved by a tool in this session")
            continue
        try:
            snap = scope.snapshot(ev)
        except NotFoundInTenant:
            reasons.append(f"evidence {ev} is not visible in this tenant")
            continue
        if snap.device_id not in target_devices:
            reasons.append(f"evidence {ev} is about {snap.device_id}, not the action target")
    if reasons:
        return PolicyResult(False, reasons)

    # 3. Data criterion, recomputed from telemetry.
    device_id = target_devices[0]
    if action == "notify_employee":
        msg = str(args.get("message", ""))
        if not msg.strip():
            return PolicyResult(False, ["message is empty"])
        if len(msg) > config.MAX_NOTIFY_MESSAGE_CHARS:
            return PolicyResult(False, [f"message longer than {config.MAX_NOTIFY_MESSAGE_CHARS} chars"])
        active = []
        cited_devices = {scope.snapshot(ev).device_id for ev in cited}
        # Justify with findings on the devices the caller actually cited first.
        for d in sorted(target_devices, key=lambda d: d not in cited_devices):
            for kind, fs in analytics.device_findings(scope, d).items():
                active += [(kind, f) for f in fs]
        if not active:
            return PolicyResult(False, ["employee has no device with an active finding; nothing to notify about"])
        return PolicyResult(True, [f"{len(active)} active finding(s) on employee's device(s), e.g. "
                                   f"{active[0][1].device_id}: {active[0][1].finding}"],
                            active[0][1].evidence)

    f = analytics.device_findings(scope, device_id)

    if action == "create_upgrade_order":
        comp = str(args.get("component", "")).lower()
        if comp not in UPGRADE_COMPONENTS:
            return PolicyResult(False, [f"component must be one of {sorted(UPGRADE_COMPONENTS)}"])
        latest = scope.device(device_id).latest
        if comp in ("ram", "storage") and _is_soldered(scope, device_id):
            return PolicyResult(False, [f"{latest.model_name} ({latest.processor}) has soldered {comp}; "
                                        "it cannot be upgraded - consider flag_device_for_replacement"])
        if comp == "ram":
            hit = [x for x in f["ram"] if x.status == "consistent"]
            if not hit:
                return PolicyResult(False, ["device is not consistently RAM-constrained "
                                            f"(needs >{config.RAM_PRESSURE_USED_PCT}% used in "
                                            f">={int(config.RAM_PERSISTENT_SHARE*100)}% of snapshots)"])
            want = _parse_gb(str(args.get("spec", "")))
            if want is None or want <= latest.ram_gb:
                return PolicyResult(False, [f"spec must be larger than current {latest.ram_gb} GB (e.g. '32 GB')"])
            return PolicyResult(True, [hit[0].finding], hit[0].evidence)
        if comp == "storage":
            hit = [x for x in f["storage"] if x.status == "consistent"]
            if not hit:
                return PolicyResult(False, ["device is not consistently low on storage"])
            want = _parse_gb(str(args.get("spec", "")))
            if want is None or want <= latest.disk_size_gb:
                return PolicyResult(False, [f"spec must be larger than current {latest.disk_size_gb} GB (e.g. '1 TB')"])
            return PolicyResult(True, [hit[0].finding], hit[0].evidence)
        hit = [x for x in f["battery"] if x.status == "end_of_life"]
        if not hit:
            return PolicyResult(False, ["battery is not at end of life"])
        return PolicyResult(True, [hit[0].finding], hit[0].evidence)

    if action == "open_remediation_ticket":
        check_id = str(args.get("check_id", ""))
        if check_id == "unapproved_software":
            hit = f["unapproved_software"]
        else:
            hit = [x for x in f["compliance"] if x.metrics["check_id"] == check_id]
        if not hit:
            return PolicyResult(False, [f"check {check_id!r} is not currently failing on {device_id}"])
        if store and store.open_ticket_exists(scope.company_id, device_id, check_id):
            return PolicyResult(False, [f"an open remediation ticket already exists for {device_id}/{check_id}"])
        return PolicyResult(True, [hit[0].finding], hit[0].evidence)

    if action == "flag_device_for_replacement":
        grounds = [x for x in f["battery"] if x.status == "end_of_life"]
        if _is_soldered(scope, device_id):
            grounds += [x for x in f["ram"] + f["storage"] if x.status == "consistent"]
        if not grounds:
            return PolicyResult(False, ["no replacement criterion met (battery end-of-life, or consistent "
                                        "RAM/storage pressure on non-upgradable hardware)"])
        return PolicyResult(True, [g.finding for g in grounds], [e for g in grounds for e in g.evidence])

    return PolicyResult(False, ["unhandled action"])


class ActionStore:
    """Mock system of record for executed actions (tickets, orders, flags, notifications)."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path or config.STATE_DIR / "actions.jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def all(self) -> list[dict]:
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]

    def by_idempotency_key(self, key: str) -> dict | None:
        return next((r for r in self.all() if r.get("idempotency_key") == key), None)

    def open_ticket_exists(self, tenant: str, device_id: str, check_id: str) -> bool:
        return any(r["tenant"] == tenant and r["action"] == "open_remediation_ticket"
                   and r["args"].get("device_id") == device_id and r["args"].get("check_id") == check_id
                   for r in self.all())

    def execute(self, tenant: str, proposal: ActionProposal, approver: str) -> dict:
        """Idempotent on `proposal.idempotency_key`: a retried execution returns the original record
        (flagged `duplicate`) instead of writing a second ticket/order. A real integration sends the
        same key to the downstream API (e.g. an Idempotency-Key header)."""
        if proposal.idempotency_key:
            prior = self.by_idempotency_key(proposal.idempotency_key)
            if prior:
                return {**prior, "duplicate": True}
        key = json.dumps([tenant, proposal.action, proposal.args], sort_keys=True)
        record = {
            "idempotency_key": proposal.idempotency_key,
            "action_id": "ACT-" + hashlib.sha256(key.encode()).hexdigest()[:10].upper(),
            "tenant": tenant,
            "action": proposal.action,
            "args": {k: v for k, v in proposal.args.items() if k != "evidence_ids"},
            "evidence": proposal.evidence,
            "justification": proposal.justification,
            "approved_by": approver,
            "executed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "proposal_id": proposal.proposal_id,
        }
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        return record
