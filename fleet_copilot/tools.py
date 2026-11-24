"""Tool catalog: typed, tenant-scoped, evidence-returning.

Design rules
* Inputs are Pydantic models with `extra="forbid"`. There is **no tenant/company
  argument** on any tool: the tenant comes from the session (`ToolContext.scope`). A model
  that tries to pass `company_id` gets a validation error.
* Every read/insight tool returns `{query_id, definition, count, results}` and registers
  (a) each supporting snapshot and (b) the query itself in the session's evidence ledger.
  Answers cite `[snap:...]` for device facts and `[query:qN]` for aggregates and
  negative results ("no devices match").
* Action tools only create proposals (see actions.py). They never execute.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import analytics, config
from .actions import ACTIONS, ActionProposal, ActionStore, evaluate_policy
from .audit import AuditLog
from .data import NotFoundInTenant, TenantScope


# --------------------------------------------------------------------------- context

_NUM_RE = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?")


def harvest_numbers(obj, out: set[float] | None = None) -> set[float]:
    """Every number a tool result states: numeric fields and numbers inside its strings
    (finding text, definitions). The verifier checks quoted figures against this set."""
    out = set() if out is None else out
    if isinstance(obj, bool) or obj is None:
        return out
    if isinstance(obj, (int, float)):
        out.add(float(obj))
    elif isinstance(obj, str):
        if not obj.startswith(("snap:", "query:")):
            out.update(float(m) for m in _NUM_RE.findall(obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k not in ("evidence", "evidence_id", "collected_at", "as_of", "first", "last"):
                harvest_numbers(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            harvest_numbers(v, out)
    return out


@dataclass
class Ledger:
    """Everything the agent has *seen* this turn. The verifier only accepts citations from here."""
    records: dict[str, dict] = field(default_factory=dict)
    query_seq: int = 0

    def add_snapshot(self, summary: dict) -> None:
        self.records.setdefault(summary["evidence_id"], {"kind": "snapshot", **summary,
                                                         "values": sorted(harvest_numbers(summary))})

    def attach_values(self, qid: str | None, result: dict) -> None:
        """Record the numbers a query's result stated, so figures citing [query:qN] can be checked."""
        if qid in self.records:
            self.records[qid]["values"] = sorted(harvest_numbers(result))

    def add_query(self, tool: str, args: dict, count: int, definition: str, devices: list[str] | None = None) -> str:
        self.query_seq += 1
        qid = f"query:q{self.query_seq}"
        self.records[qid] = {"kind": "query", "tool": tool, "args": args, "count": count,
                             "definition": definition, "devices": sorted(set(devices or []))}
        return qid

    def snapshot_ids(self) -> set[str]:
        return {k for k, v in self.records.items() if v["kind"] == "snapshot"}

    def devices(self) -> set[str]:
        return {v["device_id"] for v in self.records.values() if v["kind"] == "snapshot"}

    def employees(self) -> set[str]:
        return {v["employee_id"] for v in self.records.values() if v["kind"] == "snapshot"}


@dataclass
class ToolContext:
    scope: TenantScope
    session_id: str
    audit: AuditLog
    store: ActionStore
    ledger: Ledger = field(default_factory=Ledger)
    proposals: list[ActionProposal] = field(default_factory=list)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- inputs

class NoArgs(Strict):
    pass


class ListDevicesIn(Strict):
    model: str | None = Field(None, description="Filter by model name substring, e.g. 'MacBook'.")
    os_name: Literal["macOS", "Windows"] | None = None
    employee_id: str | None = None
    laptops_only: bool = Field(False, description="Only devices that report a battery.")


class DeviceIn(Strict):
    device_id: str = Field(..., description="Device ID (serial), e.g. 'MT7PJB7N5LRE'.")


class HistoryIn(Strict):
    device_id: str
    metric: Literal["ram_used_pct", "disk_free_pct", "battery_cycle_count",
                    "battery_full_charge_capacity", "compliance"]


class LowDiskIn(Strict):
    free_pct_below: float = Field(config.LOW_DISK_FREE_PCT, ge=0, le=100,
                                  description="Free-space percentage threshold.")


class ComplianceIn(Strict):
    severity: Literal["high", "medium", "low"] | None = None
    check_id: str | None = Field(None, description="e.g. disk_encryption, screen_lock, os_up_to_date")
    laptops_only: bool = False


class OsIn(Strict):
    os_name: Literal["macOS", "Windows"]
    version: str = Field(..., description="Baseline version; devices strictly older are returned, e.g. '15'.")


class SoftwareIn(Strict):
    name: str = Field(..., min_length=2)


class BatteryIn(Strict):
    include_ok: bool = False


class UpgradeIn(Strict):
    device_id: str
    component: Literal["ram", "storage", "battery"]
    spec: str = Field(..., description="Target spec, e.g. '32 GB', '1 TB', 'OEM replacement battery'.")
    justification: str
    evidence_ids: list[str] = Field(..., min_length=1, description="snap:... IDs supporting the action.")


class TicketIn(Strict):
    device_id: str
    check_id: str = Field(..., description="Failing compliance check_id, or 'unapproved_software'.")
    note: str
    evidence_ids: list[str] = Field(..., min_length=1)


class ReplaceIn(Strict):
    device_id: str
    reason: str
    evidence_ids: list[str] = Field(..., min_length=1)


class NotifyIn(Strict):
    employee_id: str
    message: str = Field(..., max_length=config.MAX_NOTIFY_MESSAGE_CHARS)
    evidence_ids: list[str] = Field(..., min_length=1)


# --------------------------------------------------------------------------- helpers

def _findings_result(ctx: ToolContext, tool: str, args: dict, findings: list, definition: str) -> dict:
    for f in findings:
        for ev in f.evidence:
            ctx.ledger.add_snapshot(ctx.scope.snapshot(ev).summary())
    qid = ctx.ledger.add_query(tool, args, len(findings), definition, [f.device_id for f in findings])
    return {"query_id": qid, "as_of": ctx.scope.as_of, "definition": definition,
            "count": len(findings), "results": [f.to_dict() for f in findings]}


# --------------------------------------------------------------------------- read tools

def fleet_overview(ctx: ToolContext, a: NoArgs) -> dict:
    devs = ctx.scope.devices()
    for h in devs:
        ctx.ledger.add_snapshot(h.latest.summary())
    qid = ctx.ledger.add_query("fleet_overview", {}, len(devs), "latest snapshot per device", [h.device_id for h in devs])
    return {
        "query_id": qid, "as_of": ctx.scope.as_of, "company": ctx.scope.company_name,
        "device_count": len(devs), "employee_count": len(ctx.scope.employee_ids()),
        "laptops": sum(h.is_laptop for h in devs),
        "models": dict(Counter(h.latest.model_name for h in devs)),
        "os": dict(Counter(f"{h.latest.os_name} {h.latest.os_version}" for h in devs)),
        "snapshots_per_device": len(devs[0].snapshots) if devs else 0,
        "devices": [{"device_id": h.device_id, "employee_id": h.latest.employee_id, "model": h.latest.model_name,
                     "evidence_id": h.latest.evidence_id} for h in devs],
    }


def list_devices(ctx: ToolContext, a: ListDevicesIn) -> dict:
    out = []
    for h in ctx.scope.devices():
        s = h.latest
        if a.model and a.model.lower() not in s.model_name.lower():
            continue
        if a.os_name and s.os_name != a.os_name:
            continue
        if a.employee_id and s.employee_id != a.employee_id:
            continue
        if a.laptops_only and not h.is_laptop:
            continue
        ctx.ledger.add_snapshot(s.summary())
        out.append(s.summary())
    qid = ctx.ledger.add_query("list_devices", a.model_dump(exclude_none=True), len(out), "latest snapshot per device",
                               [o["device_id"] for o in out])
    return {"query_id": qid, "as_of": ctx.scope.as_of, "count": len(out), "results": out}


def get_device(ctx: ToolContext, a: DeviceIn) -> dict:
    h = ctx.scope.device(a.device_id)
    findings = analytics.device_findings(ctx.scope, a.device_id)
    flat = [f for fs in findings.values() for f in fs]
    res = _findings_result(ctx, "get_device", a.model_dump(), flat, "all active findings for the device")
    ctx.ledger.add_snapshot(h.latest.summary())
    res["latest"] = h.latest.summary()
    res["window"] = {"first": h.first.collected_at, "last": h.latest.collected_at, "snapshots": len(h.snapshots),
                     "mean_ram_used_pct": h.mean("ram_used_pct"), "mean_disk_free_pct": h.mean("disk_free_pct"),
                     "is_laptop": h.is_laptop}
    return res


def get_device_history(ctx: ToolContext, a: HistoryIn) -> dict:
    h = ctx.scope.device(a.device_id)
    series = []
    for s in h.snapshots:
        if a.metric == "compliance":
            v = {c.check_id: c.status for c in s.compliance}
        elif a.metric.startswith("battery"):
            v = getattr(s, a.metric) if s.battery_present else None
        else:
            v = getattr(s, a.metric)
        series.append({"evidence_id": s.evidence_id, "collected_at": s.collected_at, "value": v})
        ctx.ledger.add_snapshot(s.summary())
    qid = ctx.ledger.add_query("get_device_history", a.model_dump(), len(series), f"{a.metric} per snapshot", [a.device_id])
    return {"query_id": qid, "device_id": a.device_id, "metric": a.metric, "series": series}


def find_low_disk_devices(ctx: ToolContext, a: LowDiskIn) -> dict:
    return _findings_result(ctx, "find_low_disk_devices", a.model_dump(), analytics.low_disk(ctx.scope, a.free_pct_below),
                            f"latest snapshot free disk < {a.free_pct_below}%")


def find_compliance_failures(ctx: ToolContext, a: ComplianceIn) -> dict:
    fs = analytics.compliance_failures(ctx.scope, a.severity, a.check_id, a.laptops_only)
    checks = sorted({c.check_id + "(" + c.severity + ")" for h in ctx.scope.devices() for c in h.latest.compliance})
    res = _findings_result(ctx, "find_compliance_failures", a.model_dump(exclude_none=True), fs,
                           "status != pass on each device's latest snapshot; laptops = devices reporting a battery")
    res["checks_in_scope"] = checks
    res["devices_scanned"] = len(ctx.scope.devices())
    return res


def find_os_older_than(ctx: ToolContext, a: OsIn) -> dict:
    return _findings_result(ctx, "find_os_older_than", a.model_dump(), analytics.os_older_than(ctx.scope, a.os_name, a.version),
                            f"{a.os_name} devices whose latest OS version < {a.version} (numeric compare)")


def search_installed_software(ctx: ToolContext, a: SoftwareIn) -> dict:
    return _findings_result(ctx, "search_installed_software", a.model_dump(), analytics.software_search(ctx.scope, a.name),
                            "case-insensitive substring match on latest installed-software inventory")


def find_unapproved_software(ctx: ToolContext, a: NoArgs) -> dict:
    res = _findings_result(ctx, "find_unapproved_software", {}, analytics.unapproved_software(ctx.scope),
                           "software not in the approved list")
    res["approved_list"] = sorted(config.APPROVED_SOFTWARE)
    return res


# --------------------------------------------------------------------------- insight tools

def detect_storage_pressure(ctx: ToolContext, a: NoArgs) -> dict:
    return _findings_result(ctx, "detect_storage_pressure", {}, analytics.storage_pressure(ctx.scope),
                            f"share of 30-day snapshots with free disk < {config.LOW_DISK_FREE_PCT}%; "
                            f"consistent if >= {config.STORAGE_PERSISTENT_SHARE:.0%}; linear trend for days-to-full")


def detect_ram_pressure(ctx: ToolContext, a: NoArgs) -> dict:
    return _findings_result(ctx, "detect_ram_pressure", {}, analytics.ram_pressure(ctx.scope),
                            f"share of snapshots with RAM used > {config.RAM_PRESSURE_USED_PCT}%; consistent if >= "
                            f"{config.RAM_PERSISTENT_SHARE:.0%}, watch if >= {config.RAM_WATCH_SHARE:.0%}")


def detect_battery_eol(ctx: ToolContext, a: BatteryIn) -> dict:
    return _findings_result(ctx, "detect_battery_eol", a.model_dump(), analytics.battery_health(ctx.scope, a.include_ok),
                            f"end_of_life if latest condition is a service state or cycles >= {config.BATTERY_EOL_CYCLES}; "
                            f"approaching if cycles >= {config.BATTERY_APPROACHING_CYCLES} or capacity drop >= "
                            f"{config.BATTERY_CAPACITY_DROP_PCT}% (design capacity is not in telemetry)")


def detect_compliance_drift(ctx: ToolContext, a: NoArgs) -> dict:
    d = analytics.compliance_drift(ctx.scope)
    flat = d["regressions"] + d["improvements"] + d["persistent_failures"]
    res = _findings_result(ctx, "detect_compliance_drift", {}, flat,
                           "per device and check: first vs latest status over the window, with the transition snapshot")
    res["regressions"] = [f.to_dict() for f in d["regressions"]]
    res["improvements"] = [f.to_dict() for f in d["improvements"]]
    res["persistent_failures"] = [f.to_dict() for f in d["persistent_failures"]]
    res["fleet_series"] = d["fleet_series"]
    del res["results"]
    return res


# --------------------------------------------------------------------------- action tools

def _propose(ctx: ToolContext, action: str, a: Strict, justification: str) -> dict:
    args = a.model_dump()
    policy = evaluate_policy(action, args, ctx.scope, ctx.ledger.snapshot_ids(), ctx.store)
    pid = f"P{len(ctx.proposals) + 1}"
    p = ActionProposal(pid, action, args, justification, list(args["evidence_ids"]),
                       target_device=args.get("device_id"), target_employee=args.get("employee_id"),
                       policy=policy.to_dict(), status="pending_approval" if policy.allowed else "blocked",
                       created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       data_as_of=ctx.scope.as_of)
    ctx.audit.record(event="action_proposed" if policy.allowed else "action_refused", tenant=ctx.scope.company_id,
                     session_id=ctx.session_id, payload={"proposal": p.to_dict()}, evidence=p.evidence)
    if not policy.allowed:
        return {"status": "REFUSED", "action": action, "reasons": policy.reasons,
                "note": "Refused by evidence policy. Do not retry with different wording; report this to the admin."}
    ctx.proposals.append(p)
    return {"status": "PENDING_HUMAN_APPROVAL", "proposal_id": pid, "action": action,
            "policy_reasons": policy.reasons,
            "note": "Not executed. An administrator must approve it before anything happens."}


def create_upgrade_order(ctx: ToolContext, a: UpgradeIn) -> dict:
    return _propose(ctx, "create_upgrade_order", a, a.justification)


def open_remediation_ticket(ctx: ToolContext, a: TicketIn) -> dict:
    return _propose(ctx, "open_remediation_ticket", a, a.note)


def flag_device_for_replacement(ctx: ToolContext, a: ReplaceIn) -> dict:
    return _propose(ctx, "flag_device_for_replacement", a, a.reason)


def notify_employee(ctx: ToolContext, a: NotifyIn) -> dict:
    return _propose(ctx, "notify_employee", a, a.message)


# --------------------------------------------------------------------------- registry

@dataclass(frozen=True)
class ToolSpec:
    name: str
    kind: Literal["read", "insight", "action"]
    description: str
    input_model: type[Strict]
    fn: Callable[[ToolContext, Any], dict]

    def anthropic_schema(self) -> dict:
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        return {"name": self.name, "description": self.description, "input_schema": schema}


TOOLS: dict[str, ToolSpec] = {t.name: t for t in [
    ToolSpec("fleet_overview", "read", "Company fleet summary: device/employee counts, models, OS mix, device list.", NoArgs, fleet_overview),
    ToolSpec("list_devices", "read", "List devices (latest snapshot) with optional filters.", ListDevicesIn, list_devices),
    ToolSpec("get_device", "read", "Latest state, 30-day stats and all active findings for one device.", DeviceIn, get_device),
    ToolSpec("get_device_history", "read", "Per-snapshot time series of one metric for one device.", HistoryIn, get_device_history),
    ToolSpec("find_low_disk_devices", "read", "Devices whose latest free disk is below a threshold (default 10%).", LowDiskIn, find_low_disk_devices),
    ToolSpec("find_compliance_failures", "read", "Devices failing compliance checks on their latest snapshot; filter by severity/check/laptops.", ComplianceIn, find_compliance_failures),
    ToolSpec("find_os_older_than", "read", "Devices whose OS version is strictly older than a baseline.", OsIn, find_os_older_than),
    ToolSpec("search_installed_software", "read", "Devices with a given application installed.", SoftwareIn, search_installed_software),
    ToolSpec("find_unapproved_software", "read", "Devices with software outside the approved list.", NoArgs, find_unapproved_software),
    ToolSpec("detect_storage_pressure", "insight", "Trend: devices consistently low on storage over 30 days, with fill rate.", NoArgs, detect_storage_pressure),
    ToolSpec("detect_ram_pressure", "insight", "Trend: devices consistently RAM-constrained (not one-off spikes).", NoArgs, detect_ram_pressure),
    ToolSpec("detect_battery_eol", "insight", "Trend: batteries at or approaching end of life.", BatteryIn, detect_battery_eol),
    ToolSpec("detect_compliance_drift", "insight", "Trend: compliance checks that regressed pass->fail (or improved) over the window.", NoArgs, detect_compliance_drift),
    ToolSpec("create_upgrade_order", "action", "PROPOSE a hardware upgrade. Requires cited evidence; needs human approval.", UpgradeIn, create_upgrade_order),
    ToolSpec("open_remediation_ticket", "action", "PROPOSE a remediation ticket for a failing check. Requires cited evidence; needs human approval.", TicketIn, open_remediation_ticket),
    ToolSpec("flag_device_for_replacement", "action", "PROPOSE flagging a device for replacement. Requires cited evidence; needs human approval.", ReplaceIn, flag_device_for_replacement),
    ToolSpec("notify_employee", "action", "PROPOSE a message to an employee about their device. Requires cited evidence; needs human approval.", NotifyIn, notify_employee),
]}
assert set(ACTIONS) == {n for n, t in TOOLS.items() if t.kind == "action"}


def compact_for_model(result: dict, max_items: int | None = None, max_chars: int | None = None) -> str:
    """Serialise a tool result for an LLM, bounded in size.

    Long lists (`results`, `devices`, `series`, drift lists) are cut to `max_items` with an
    explicit `truncated` note, so the model knows it saw a page, not everything. The evidence
    ledger still holds every record, so citations and the evidence policy are unaffected.
    """
    import json
    max_items = max_items or config.MAX_RESULTS_TO_MODEL
    max_chars = max_chars or config.MAX_RESULT_CHARS_TO_MODEL
    out, truncated = dict(result), {}
    for key in ("results", "devices", "series", "regressions", "improvements", "persistent_failures"):
        v = out.get(key)
        if isinstance(v, list) and len(v) > max_items:
            out[key] = v[:max_items]
            truncated[key] = {"shown": max_items, "total": len(v)}
    if truncated:
        out["truncated"] = {**truncated, "note": "Only the first items are shown. Counts are complete. "
                                                 "Narrow the query with filters to see others."}
    text = json.dumps(out, default=str)
    if len(text) > max_chars:
        text = text[:max_chars] + '..." [output cut at size limit; narrow the query]'
    return text


def run_tool(ctx: ToolContext, name: str, raw_args: dict) -> tuple[dict, bool]:
    """Validate, execute and audit one tool call. Returns (result, is_error)."""
    spec = TOOLS.get(name)
    if spec is None:
        result, err = {"error": f"unknown tool {name!r}"}, True
    else:
        try:
            result, err = spec.fn(ctx, spec.input_model.model_validate(raw_args or {})), False
            ctx.ledger.attach_values(result.get("query_id"), result)
        except ValidationError as e:
            result, err = {"error": "invalid arguments", "details": e.errors(include_url=False, include_input=False)}, True
        except NotFoundInTenant as e:
            result, err = {"error": str(e)}, True
    ctx.audit.record(event="tool_call", tenant=ctx.scope.company_id, session_id=ctx.session_id,
                     payload={"tool": name, "args": raw_args, "is_error": err,
                              "status": result.get("status"), "count": result.get("count"),
                              "error": result.get("error")})
    return result, err
