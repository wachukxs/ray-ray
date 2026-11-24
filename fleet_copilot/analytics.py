"""Deterministic detectors over a tenant's telemetry.

These pure functions are the *only* place health judgements are computed. Read tools
call them to answer questions; the action evidence policy calls them again, server-side,
to decide whether an action is justified. The LLM never gets to define "constrained".

Every finding carries `evidence`: the snapshot IDs that support it (first/last of a
window, the worst reading, the exact pass->fail transition, ...).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field

from . import config
from .data import DeviceHistory, Snapshot, TenantScope, parse_version


@dataclass
class Finding:
    device_id: str
    employee_id: str
    model: str
    status: str                  # e.g. "consistent", "watch", "end_of_life", "regressed"
    finding: str                 # one-line human statement
    explanation: str             # why it matters / how it was derived
    metrics: dict = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _slope_per_day(points: list[tuple[int, float]]) -> float:
    """Least-squares slope of value vs. day index."""
    n = len(points)
    if n < 2:
        return 0.0
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    den = sum((x - mx) ** 2 for x, _ in points)
    return 0.0 if den == 0 else sum((x - mx) * (y - my) for x, y in points) / den


def _unique(ids: list[str]) -> list[str]:
    return list(dict.fromkeys(ids))


# --- point-in-time ----------------------------------------------------------------------

def low_disk(scope: TenantScope, free_pct_below: float = config.LOW_DISK_FREE_PCT) -> list[Finding]:
    out = []
    for h in scope.devices():
        s = h.latest
        if s.disk_free_pct < free_pct_below:
            out.append(Finding(
                h.device_id, s.employee_id, s.model_name, "low",
                f"{s.disk_free_pct}% free ({s.disk_free_gb} GB of {s.disk_size_gb} GB) on latest snapshot",
                f"Latest free space is below the {free_pct_below}% threshold.",
                {"disk_free_pct": s.disk_free_pct, "disk_free_gb": s.disk_free_gb, "disk_size_gb": s.disk_size_gb},
                [s.evidence_id],
            ))
    return sorted(out, key=lambda f: f.metrics["disk_free_pct"])


def compliance_failures(scope: TenantScope, severity: str | None = None, check_id: str | None = None,
                        laptops_only: bool = False) -> list[Finding]:
    out = []
    for h in scope.devices():
        if laptops_only and not h.is_laptop:
            continue
        s = h.latest
        for c in s.compliance:
            if c.status == "pass":
                continue
            if severity and c.severity != severity.lower():
                continue
            if check_id and c.check_id != check_id:
                continue
            out.append(Finding(
                h.device_id, s.employee_id, s.model_name, "failing",
                f"failing {c.check_id} (severity {c.severity}) on latest snapshot",
                "Status taken from the device's most recent compliance results.",
                {"check_id": c.check_id, "severity": c.severity, "is_laptop": h.is_laptop},
                [s.evidence_id],
            ))
    return out


def os_older_than(scope: TenantScope, os_name: str, version: str) -> list[Finding]:
    target = parse_version(version)
    out = []
    for h in scope.devices():
        s = h.latest
        if s.os_name.lower() != os_name.lower():
            continue
        if parse_version(s.os_version) < target:
            out.append(Finding(
                h.device_id, s.employee_id, s.model_name, "older",
                f"runs {s.os_name} {s.os_version}",
                f"{s.os_name} {s.os_version} is older than {os_name} {version} (numeric version compare).",
                {"os_version": s.os_version},
                [s.evidence_id],
            ))
    return out


def software_search(scope: TenantScope, name: str) -> list[Finding]:
    out = []
    for h in scope.devices():
        s = h.latest
        hits = [(n, v) for n, v in s.software if name.lower() in n.lower()]
        if hits:
            out.append(Finding(
                h.device_id, s.employee_id, s.model_name, "installed",
                "has " + ", ".join(f"{n} {v}" for n, v in hits) + " installed",
                "From the latest installed-software inventory.",
                {"matches": [n for n, _ in hits]},
                [s.evidence_id],
            ))
    return out


def unapproved_software(scope: TenantScope) -> list[Finding]:
    out = []
    for h in scope.devices():
        s = h.latest
        extra = sorted({n for n, _ in s.software} - config.APPROVED_SOFTWARE)
        if extra:
            out.append(Finding(
                h.device_id, s.employee_id, s.model_name, "unapproved",
                "has unapproved software: " + ", ".join(extra),
                "Not on the approved software list (config.APPROVED_SOFTWARE).",
                {"unapproved": extra},
                [s.evidence_id],
            ))
    return out


# --- time-series ------------------------------------------------------------------------

def storage_pressure(scope: TenantScope) -> list[Finding]:
    out = []
    for h in scope.devices():
        snaps = h.snapshots
        low = [s for s in snaps if s.disk_free_pct < config.LOW_DISK_FREE_PCT]
        share = len(low) / len(snaps)
        if share == 0:
            continue
        slope = _slope_per_day([(i, s.disk_free_pct) for i, s in enumerate(snaps)])
        latest = h.latest
        days_to_full = round(latest.disk_free_pct / -slope) if slope < -0.01 else None
        status = "consistent" if share >= config.STORAGE_PERSISTENT_SHARE else "intermittent"
        worst = min(snaps, key=lambda s: s.disk_free_pct)
        out.append(Finding(
            h.device_id, latest.employee_id, latest.model_name, status,
            f"below {config.LOW_DISK_FREE_PCT}% free disk in {len(low)}/{len(snaps)} snapshots "
            f"(now {latest.disk_free_pct}% free, was {h.first.disk_free_pct}%)",
            f"Free space trend {slope:+.2f} pts/day"
            + (f"; at this rate the disk fills in ~{days_to_full} days." if days_to_full is not None else "."),
            {"low_share": round(share, 2), "latest_free_pct": latest.disk_free_pct,
             "first_free_pct": h.first.disk_free_pct, "trend_pct_per_day": round(slope, 2),
             "days_to_full": days_to_full, "disk_size_gb": latest.disk_size_gb},
            _unique([h.first.evidence_id, low[0].evidence_id, worst.evidence_id, latest.evidence_id]),
        ))
    return sorted(out, key=lambda f: (-f.metrics["low_share"], f.metrics["latest_free_pct"]))


def ram_pressure(scope: TenantScope) -> list[Finding]:
    out = []
    for h in scope.devices():
        snaps = h.snapshots
        hot = [s for s in snaps if s.ram_used_pct > config.RAM_PRESSURE_USED_PCT]
        share = len(hot) / len(snaps)
        if share < config.RAM_WATCH_SHARE:
            continue
        status = "consistent" if share >= config.RAM_PERSISTENT_SHARE else "watch"
        peak = max(snaps, key=lambda s: s.ram_used_pct)
        out.append(Finding(
            h.device_id, h.latest.employee_id, h.latest.model_name, status,
            f"RAM above {config.RAM_PRESSURE_USED_PCT}% used in {len(hot)}/{len(snaps)} snapshots "
            f"(mean {h.mean('ram_used_pct')}%, peak {peak.ram_used_pct}%) on {h.latest.ram_gb} GB",
            "Sustained, not a one-off spike: share of snapshots over the threshold across the whole window."
            + (" Candidate for a RAM upgrade or replacement." if status == "consistent" else " Watchlist only."),
            {"pressure_share": round(share, 2), "mean_used_pct": h.mean("ram_used_pct"),
             "peak_used_pct": peak.ram_used_pct, "ram_gb": h.latest.ram_gb},
            _unique([h.first.evidence_id, hot[0].evidence_id, peak.evidence_id, h.latest.evidence_id]),
        ))
    return sorted(out, key=lambda f: -f.metrics["pressure_share"])


def battery_health(scope: TenantScope, include_ok: bool = False) -> list[Finding]:
    out = []
    for h in scope.devices():
        bs = h.battery_snapshots
        if not bs:
            continue
        first, last = bs[0], bs[-1]
        drop = 0.0
        if first.battery_full_charge_capacity and last.battery_full_charge_capacity:
            drop = round(100 * (first.battery_full_charge_capacity - last.battery_full_charge_capacity)
                         / first.battery_full_charge_capacity, 1)
        service = [s for s in bs if s.battery_condition in config.BATTERY_SERVICE_CONDITIONS]
        cycles = last.battery_cycle_count or 0
        reasons = []
        if last.battery_condition in config.BATTERY_SERVICE_CONDITIONS:
            reasons.append(f"condition '{last.battery_condition}' on latest reading")
        if cycles >= config.BATTERY_EOL_CYCLES:
            reasons.append(f"{cycles} cycles (>= {config.BATTERY_EOL_CYCLES} rated)")
        if reasons:
            status = "end_of_life"
        elif cycles >= config.BATTERY_APPROACHING_CYCLES or drop >= config.BATTERY_CAPACITY_DROP_PCT or service:
            status = "approaching"
            if cycles >= config.BATTERY_APPROACHING_CYCLES:
                reasons.append(f"{cycles} cycles (>= {config.BATTERY_APPROACHING_CYCLES})")
            if drop >= config.BATTERY_CAPACITY_DROP_PCT:
                reasons.append(f"full-charge capacity down {drop}% in 30 days")
            if service:
                reasons.append(f"service condition reported in {len(service)} snapshots")
        else:
            status = "ok"
            if not include_ok:
                continue
        gaps = len(h.snapshots) - len(bs)
        if drop >= config.BATTERY_CAPACITY_DROP_PCT and status == "end_of_life":
            reasons.append(f"full-charge capacity down {drop}% in 30 days")
        evidence = [first.evidence_id, last.evidence_id]
        if service:
            evidence.insert(1, service[0].evidence_id)
        out.append(Finding(
            h.device_id, last.employee_id, last.model_name, status,
            f"battery {status.replace('_', ' ')}: " + ("; ".join(reasons) if reasons else "healthy"),
            f"Cycles {first.battery_cycle_count}->{cycles}, capacity {first.battery_full_charge_capacity}"
            f"->{last.battery_full_charge_capacity} mAh, condition now '{last.battery_condition}'."
            + (f" Note: {gaps} snapshots had no battery telemetry (treated as missing data, not removal)." if gaps else ""),
            {"cycle_count": cycles, "capacity_drop_pct": drop, "condition": last.battery_condition,
             "service_snapshots": len(service), "missing_battery_snapshots": gaps},
            _unique(evidence),
        ))
    order = {"end_of_life": 0, "approaching": 1, "ok": 2}
    return sorted(out, key=lambda f: (order[f.status], -f.metrics["cycle_count"]))


def compliance_drift(scope: TenantScope) -> dict:
    """Per-device pass->fail transitions plus a fleet-level daily failure series per check."""
    regressions, improvements, persistent = [], [], []
    daily: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for h in scope.devices():
        checks = sorted({c.check_id for s in h.snapshots for c in s.compliance})
        for cid in checks:
            series = [(s, s.check(cid)) for s in h.snapshots if s.check(cid)]
            for s, c in series:
                if c.status != "pass":
                    daily[cid][s.collected_at[:10]].add(h.device_id)
            first_s, first_c = series[0]
            last_s, last_c = series[-1]
            if first_c.status == "pass" and last_c.status != "pass":
                # last pass before the most recent failing streak
                idx = max(i for i, (_, c) in enumerate(series) if c.status == "pass")
                pass_s, fail_s = series[idx][0], series[idx + 1][0]
                regressions.append(Finding(
                    h.device_id, last_s.employee_id, last_s.model_name, "regressed",
                    f"{cid} ({last_c.severity}) went pass -> fail on {fail_s.collected_at[:10]} and is still failing",
                    f"Passed through {pass_s.collected_at[:10]}, failing since {fail_s.collected_at[:10]} "
                    f"({len(series) - idx - 1} consecutive failing snapshots).",
                    {"check_id": cid, "severity": last_c.severity, "failing_since": fail_s.collected_at,
                     "failing_snapshots": len(series) - idx - 1},
                    [pass_s.evidence_id, fail_s.evidence_id, last_s.evidence_id],
                ))
            elif first_c.status != "pass" and last_c.status == "pass":
                improvements.append(Finding(
                    h.device_id, last_s.employee_id, last_s.model_name, "improved",
                    f"{cid} went fail -> pass", "Remediated during the window.",
                    {"check_id": cid}, [first_s.evidence_id, last_s.evidence_id]))
            elif all(c.status != "pass" for _, c in series):
                persistent.append(Finding(
                    h.device_id, last_s.employee_id, last_s.model_name, "persistent_fail",
                    f"{cid} ({last_c.severity}) failing in all {len(series)} snapshots",
                    "Never passed during the 30-day window.",
                    {"check_id": cid, "severity": last_c.severity}, [first_s.evidence_id, last_s.evidence_id]))
    series_out = {}
    all_days = sorted({d for per in daily.values() for d in per})
    for cid, per_day in sorted(daily.items()):
        counts = [len(per_day.get(d, ())) for d in all_days]
        series_out[cid] = {"first_day": all_days[0], "last_day": all_days[-1],
                           "failing_devices_first_day": counts[0], "failing_devices_last_day": counts[-1],
                           "peak": max(counts)}
    return {
        "regressions": sorted(regressions, key=lambda f: f.metrics["failing_since"]),
        "improvements": improvements,
        "persistent_failures": persistent,
        "fleet_series": series_out,
        "device_count": len(scope.devices()),
    }


def device_findings(scope: TenantScope, device_id: str) -> dict[str, list[Finding]]:
    """All active findings for one device - used by the evidence policy.

    The fleet-wide detectors run once per scope object and are reused for every device and
    every policy check in it (a batch of N proposals used to re-run all seven detectors N times).
    """
    def mine(fs):
        return [f for f in fs if f.device_id == device_id]
    scope.device(device_id)  # raises if foreign
    every = scope.memo("device_findings:all", lambda: {
        "low_disk": low_disk(scope),
        "storage": storage_pressure(scope),
        "ram": ram_pressure(scope),
        "battery": battery_health(scope),
        "compliance": compliance_failures(scope),
        "regressions": compliance_drift(scope)["regressions"],
        "unapproved_software": unapproved_software(scope),
    })
    return {kind: mine(fs) for kind, fs in every.items()}


def latest_snapshot(h: DeviceHistory) -> Snapshot:
    return h.latest
