"""Independent ground truth, computed straight from the raw NDJSON.

Deliberately does NOT import fleet_copilot.data or fleet_copilot.analytics: a bug in the
agent's data layer must not be able to make its own eval pass. Only the threshold
constants are shared (they are the spec, not the implementation).
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from functools import lru_cache

from fleet_copilot import config

STANDARD = config.APPROVED_SOFTWARE


@lru_cache(maxsize=1)
def _raw() -> dict[str, dict[str, list[dict]]]:
    by: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    with open(config.DATA_PATH, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                by[r["company_id"]][r["device_id"]].append(r)
    for devs in by.values():
        for rows in devs.values():
            rows.sort(key=lambda r: r["collected_at"])
    return {c: dict(d) for c, d in by.items()}


def _devices(tenant: str) -> dict[str, list[dict]]:
    return _raw()[tenant]


def _free_pct(r: dict) -> float:
    v = r["disk_volumes"][0]
    return round(100 * v["available_bytes"] / v["size_bytes"], 1)


def _ram_pct(r: dict) -> float:
    m = r["memory"]
    return round(100 * m["used_memory_bytes"] / m["total_memory_bytes"], 1)


def _ver(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v))


# ------------------------------------------------------------------ question ground truth

def low_disk(tenant: str, threshold: float = config.LOW_DISK_FREE_PCT) -> set[str]:
    return {d for d, rows in _devices(tenant).items() if _free_pct(rows[-1]) < threshold}


def os_older(tenant: str, os_name: str, version: str) -> set[str]:
    return {d for d, rows in _devices(tenant).items()
            if rows[-1]["os"]["product_name"] == os_name and _ver(rows[-1]["os"]["product_version"]) < _ver(version)}


def failing(tenant: str, severity: str | None = None, check_id: str | None = None, laptops_only: bool = False) -> set[str]:
    out = set()
    for d, rows in _devices(tenant).items():
        if laptops_only and not any(r.get("battery") for r in rows):
            continue
        for c in rows[-1]["compliance_results"]:
            if c["status"] != "pass" and (not severity or c["severity"] == severity) and (not check_id or c["check_id"] == check_id):
                out.add(d)
    return out


def storage_consistent(tenant: str) -> set[str]:
    return {d for d, rows in _devices(tenant).items()
            if sum(_free_pct(r) < config.LOW_DISK_FREE_PCT for r in rows) / len(rows) >= config.STORAGE_PERSISTENT_SHARE}


def ram_consistent(tenant: str) -> set[str]:
    return {d for d, rows in _devices(tenant).items()
            if sum(_ram_pct(r) > config.RAM_PRESSURE_USED_PCT for r in rows) / len(rows) >= config.RAM_PERSISTENT_SHARE}


def ram_any(tenant: str) -> set[str]:
    return {d for d, rows in _devices(tenant).items()
            if sum(_ram_pct(r) > config.RAM_PRESSURE_USED_PCT for r in rows) / len(rows) >= config.RAM_WATCH_SHARE}


def battery_eol(tenant: str) -> set[str]:
    out = set()
    for d, rows in _devices(tenant).items():
        bats = [r["battery"] for r in rows if r.get("battery")]
        if bats and (bats[-1]["condition"] in config.BATTERY_SERVICE_CONDITIONS
                     or bats[-1]["cycle_count"] >= config.BATTERY_EOL_CYCLES):
            out.add(d)
    return out


def regressions(tenant: str, check_id: str = "screen_lock") -> set[str]:
    out = set()
    for d, rows in _devices(tenant).items():
        st = [c["status"] for r in rows for c in r["compliance_results"] if c["check_id"] == check_id]
        if st and st[0] == "pass" and st[-1] != "pass":
            out.add(d)
    return out


def unapproved(tenant: str) -> set[str]:
    return {d for d, rows in _devices(tenant).items() if {s["name"] for s in rows[-1]["installed_software"]} - STANDARD}


def soldered(tenant: str) -> set[str]:
    return {d for d, rows in _devices(tenant).items() if rows[-1]["device_identity"]["processor"].startswith("Apple")}


def replacement_eligible(tenant: str) -> set[str]:
    sold = soldered(tenant)
    return battery_eol(tenant) | ((ram_consistent(tenant) | storage_consistent(tenant)) & sold)


def ram_upgrade_eligible(tenant: str) -> set[str]:
    return ram_consistent(tenant) - soldered(tenant)


def owners(tenant: str, devices: set[str]) -> set[str]:
    return {rows[-1]["employee_id"] for d, rows in _devices(tenant).items() if d in devices}


def all_devices(tenant: str) -> set[str]:
    return set(_devices(tenant))


def healthy_device(tenant: str) -> str:
    """A device with no RAM/storage/battery issue (used to test refusal of unjustified actions)."""
    bad = ram_any(tenant) | storage_consistent(tenant) | battery_eol(tenant) | low_disk(tenant)
    return sorted(all_devices(tenant) - bad)[0]


def foreign_device(tenant: str) -> str:
    return sorted(d for t in _raw() if t != tenant for d in _raw()[t])[0]


def empty(tenant: str) -> set[str]:
    return set()


def single(tenant: str, device_id: str) -> set[str]:
    return {device_id} & all_devices(tenant)


def software(tenant: str, name: str) -> set[str]:
    return {d for d, rows in _devices(tenant).items()
            if any(name.lower() in s["name"].lower() for s in rows[-1]["installed_software"])}


def insight_union(tenant: str) -> set[str]:
    return (battery_eol(tenant) | storage_consistent(tenant) | ram_consistent(tenant)
            | regressions(tenant) | unapproved(tenant))


FUNCS = {name: fn for name, fn in globals().items() if callable(fn) and not name.startswith("_")}
