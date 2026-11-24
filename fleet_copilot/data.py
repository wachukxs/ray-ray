"""Data layer: load NDJSON telemetry into typed snapshots and expose tenant-scoped views.

Tenant isolation is enforced here, by construction: the only way tools reach data is
through a `TenantScope`, which physically holds just one company's snapshots. There is
no unscoped query method for tools to call by mistake, and a lookup of another
tenant's device fails exactly like a lookup of a device that does not exist (no
existence oracle).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from statistics import mean

from . import config


class NotFoundInTenant(LookupError):
    """Raised for IDs not visible to the current tenant (whether or not they exist elsewhere)."""


@dataclass(frozen=True)
class ComplianceResult:
    check_id: str
    status: str
    severity: str


@dataclass(frozen=True)
class Snapshot:
    device_id: str
    company_id: str
    employee_id: str
    collected_at: str  # ISO-8601 UTC, lexically sortable
    os_platform: str
    os_name: str
    os_version: str
    hostname: str
    model_name: str
    processor: str
    total_memory_label: str
    ram_total_bytes: int
    ram_used_bytes: int
    disk_size_bytes: int
    disk_available_bytes: int
    disk_encrypted: bool | None
    battery_present: bool
    battery_condition: str | None
    battery_cycle_count: int | None
    battery_full_charge_capacity: int | None
    battery_percentage: int | None
    software: tuple[tuple[str, str], ...]  # (name, version)
    compliance: tuple[ComplianceResult, ...]

    @property
    def evidence_id(self) -> str:
        """Stable citation key for this snapshot."""
        return f"snap:{self.device_id}@{self.collected_at}"

    @property
    def ram_used_pct(self) -> float:
        return round(100.0 * self.ram_used_bytes / self.ram_total_bytes, 1)

    @property
    def disk_free_pct(self) -> float:
        return round(100.0 * self.disk_available_bytes / self.disk_size_bytes, 1)

    @property
    def disk_free_gb(self) -> float:
        return round(self.disk_available_bytes / 1024**3, 1)

    @property
    def disk_size_gb(self) -> int:
        return round(self.disk_size_bytes / 1024**3)

    @property
    def ram_gb(self) -> int:
        return round(self.ram_total_bytes / 1024**3)

    def check(self, check_id: str) -> ComplianceResult | None:
        return next((c for c in self.compliance if c.check_id == check_id), None)

    def summary(self) -> dict:
        """Compact, citation-friendly view of a snapshot (what the ledger stores)."""
        return {
            "evidence_id": self.evidence_id,
            "device_id": self.device_id,
            "employee_id": self.employee_id,
            "collected_at": self.collected_at,
            "model": self.model_name,
            "os": f"{self.os_name} {self.os_version}",
            "ram_gb": self.ram_gb,
            "ram_used_pct": self.ram_used_pct,
            "disk_size_gb": self.disk_size_gb,
            "disk_free_pct": self.disk_free_pct,
            "battery": None if not self.battery_present else {
                "condition": self.battery_condition,
                "cycle_count": self.battery_cycle_count,
                "full_charge_capacity": self.battery_full_charge_capacity,
            },
            "failing_checks": [f"{c.check_id}({c.severity})" for c in self.compliance if c.status != "pass"],
        }


def _parse(raw: dict) -> Snapshot:
    os_ = raw.get("os") or {}
    ident = raw.get("device_identity") or {}
    mem = raw.get("memory") or {}
    vols = raw.get("disk_volumes") or []
    # The system volume is the one mounted at / (macOS) or C:\ (Windows); fall back to the first.
    vol = next((v for v in vols if v.get("mount_point") in ("/", "C:\\")), vols[0] if vols else {})
    bat = raw.get("battery") or None
    return Snapshot(
        device_id=raw["device_id"],
        company_id=raw["company_id"],
        employee_id=raw["employee_id"],
        collected_at=raw["collected_at"],
        os_platform=os_.get("platform", ""),
        os_name=os_.get("product_name", ""),
        os_version=str(os_.get("product_version", "")),
        hostname=os_.get("hostname", ""),
        model_name=ident.get("model_name", ""),
        processor=ident.get("processor", ""),
        total_memory_label=ident.get("total_memory", ""),
        ram_total_bytes=int(mem.get("total_memory_bytes") or mem.get("ram_bytes") or 0),
        ram_used_bytes=int(mem.get("used_memory_bytes") or 0),
        disk_size_bytes=int(vol.get("size_bytes") or 0),
        disk_available_bytes=int(vol.get("available_bytes") or 0),
        disk_encrypted=vol.get("encrypted"),
        battery_present=bool(bat and bat.get("battery_present")),
        battery_condition=bat.get("condition") if bat else None,
        battery_cycle_count=bat.get("cycle_count") if bat else None,
        battery_full_charge_capacity=bat.get("full_charge_capacity") if bat else None,
        battery_percentage=bat.get("percentage") if bat else None,
        software=tuple((s.get("name", ""), str(s.get("version", ""))) for s in raw.get("installed_software") or []),
        compliance=tuple(
            ComplianceResult(c.get("check_id", ""), c.get("status", ""), c.get("severity", ""))
            for c in raw.get("compliance_results") or []
        ),
    )


def parse_version(v: str) -> tuple[int, ...]:
    """'14.5' -> (14, 5); '11 23H2' -> (11, 23); tolerant of suffixes."""
    nums = re.findall(r"\d+", v)
    return tuple(int(n) for n in nums) or (0,)


@dataclass
class DeviceHistory:
    """All snapshots of one device, oldest first."""
    snapshots: list[Snapshot] = field(default_factory=list)

    @property
    def device_id(self) -> str:
        return self.snapshots[0].device_id

    @property
    def latest(self) -> Snapshot:
        return self.snapshots[-1]

    @property
    def first(self) -> Snapshot:
        return self.snapshots[0]

    @cached_property
    def is_laptop(self) -> bool:
        """A device that ever reported a battery. Gaps in battery telemetry do not unmake a laptop."""
        return any(s.battery_present for s in self.snapshots)

    @property
    def battery_snapshots(self) -> list[Snapshot]:
        return [s for s in self.snapshots if s.battery_present]

    def mean(self, attr: str) -> float:
        return round(mean(getattr(s, attr) for s in self.snapshots), 1)


class FleetData:
    """Whole-dataset store. Never handed to tools — only `scope()` views are."""

    def __init__(self, snapshots: list[Snapshot]):
        self._by_company: dict[str, dict[str, DeviceHistory]] = {}
        for s in sorted(snapshots, key=lambda s: (s.company_id, s.device_id, s.collected_at)):
            self._by_company.setdefault(s.company_id, {}).setdefault(s.device_id, DeviceHistory()).snapshots.append(s)
        self.as_of = max(s.collected_at for s in snapshots)

    @classmethod
    def load(cls, path: Path | str = config.DATA_PATH) -> "FleetData":
        with open(path, encoding="utf-8") as f:
            return cls([_parse(json.loads(line)) for line in f if line.strip()])

    @property
    def company_ids(self) -> list[str]:
        return sorted(self._by_company)

    def scope(self, company_id: str) -> "TenantScope":
        if company_id not in self._by_company:
            raise NotFoundInTenant(f"unknown tenant {company_id!r}")
        return TenantScope(company_id, self._by_company[company_id], self.as_of)

    # Used only by guardrails to *detect* foreign identifiers in text — never to return data.
    def foreign_identifiers(self, company_id: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for cid, devices in self._by_company.items():
            if cid == company_id:
                continue
            out[cid] = cid
            for d, h in devices.items():
                out[d] = cid
                out[h.first.employee_id] = cid
                for s in h.snapshots:
                    out[s.hostname] = cid
        return out


class TenantScope:
    """Read-only view over exactly one tenant's devices."""

    def __init__(self, company_id: str, devices: dict[str, DeviceHistory], as_of: str):
        self.company_id = company_id
        self.company_name = config.COMPANY_NAMES.get(company_id, company_id)
        self._devices = devices
        self.as_of = as_of

    def devices(self) -> list[DeviceHistory]:
        return [self._devices[k] for k in sorted(self._devices)]

    def device(self, device_id: str) -> DeviceHistory:
        h = self._devices.get(device_id)
        if h is None:
            raise NotFoundInTenant(f"device {device_id!r} not found in {self.company_name}")
        return h

    def employee_ids(self) -> list[str]:
        return sorted({h.first.employee_id for h in self._devices.values()})

    def devices_of(self, employee_id: str) -> list[DeviceHistory]:
        out = [h for h in self.devices() if h.latest.employee_id == employee_id]
        if not out:
            raise NotFoundInTenant(f"employee {employee_id!r} not found in {self.company_name}")
        return out

    def snapshot(self, evidence_id: str) -> Snapshot:
        m = re.fullmatch(r"snap:([^@]+)@(.+)", evidence_id)
        if not m:
            raise NotFoundInTenant(f"malformed evidence id {evidence_id!r}")
        h = self.device(m.group(1))
        for s in h.snapshots:
            if s.collected_at == m.group(2):
                return s
        raise NotFoundInTenant(f"snapshot {evidence_id!r} not found")
