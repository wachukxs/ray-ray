"""Deterministic offline brain.

Same contract as the Claude brain (plan -> choose tool calls step by step -> compose a cited
answer) but driven by an intent router and recipes instead of a model. It exists so that:
  * the whole graph (guard, tools, policy, HITL approval, verifier, audit) runs and is
    evaluated with no API key and fully deterministic output;
  * the eval suite has a reproducible baseline to compare the LLM brain against.

A recipe is a small state machine: `next_calls(round, tool_log)` looks at what has been
retrieved so far and returns the next batch of tool calls (or None when done);
`compose(tool_log)` writes the answer, citing only IDs returned by tools.

The brain itself is stateless. The recipe is re-derived from the question on every call and
the round comes from the graph's own step counter, so everything a resumed turn needs lives in
the checkpointed graph state (nothing on the brain object).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from ..data import TenantScope

DEVICE_ID_RE = re.compile(r"\b[0-9A-Z]{12}\b")
EMP_RE = re.compile(r"\bemp-[a-z]+-\d+\b")


def _cite(ids: list[str], limit: int = 2) -> str:
    return "".join(f"[{i}]" for i in ids[:limit])


def _fmt_findings(res: dict, empty: str, header: str) -> str:
    q = res["query_id"]
    if res["count"] == 0:
        return f"{empty} (definition: {res['definition']}; as of {res['as_of'][:10]}) [{q}]"
    lines = [f"{header} - {res['count']} device(s) [{q}] (definition: {res['definition']}):"]
    for f in res["results"]:
        lines.append(f"- {f['device_id']} ({f['model']}, {f['employee_id']}): {f['finding']}. "
                     f"{f['explanation']} {_cite(f['evidence'], 2)}")
    return "\n".join(lines)


@dataclass
class Recipe:
    intent: str
    steps: list[str]
    first: list[dict]
    follow: Callable[[list[dict]], list[dict] | None] | None = None
    composer: Callable[[list[dict]], str] | None = None

    def next_calls(self, round_: int, log: list[dict]) -> list[dict] | None:
        """round_ = number of agent steps already taken this turn (graph state `steps`)."""
        if round_ == 0:
            return self.first or None
        if self.follow and round_ == 1:
            return self.follow(log) or None
        return None


def _call(name: str, **args) -> dict:
    return {"name": name, "args": args}


def _results(log: list[dict], name: str) -> list[dict]:
    return [e["result"] for e in log if e["name"] == name and not e["is_error"]]


def _action_summary(log: list[dict]) -> str:
    acts = [e for e in log if e["name"] in ACTION_TOOLS]
    if not acts:
        return "No actions were proposed: nothing in the retrieved evidence met the criteria."
    lines = ["Proposed actions (nothing has been executed; each needs your approval):"]
    for e in acts:
        r, a = e["result"], e["args"]
        target = a.get("device_id") or a.get("employee_id")
        ev = _cite(a.get("evidence_ids", []), 2)
        if r.get("status") == "PENDING_HUMAN_APPROVAL":
            detail = a.get("spec") or a.get("check_id")
            call = f"{e['name']}({target}" + (f", {detail})" if detail else ")")
            lines.append(f"- {r['proposal_id']}: {call} - justified by: {'; '.join(r['policy_reasons'])} {ev}")
            if e["name"] == "notify_employee":
                lines.append(f"    message: \"{a['message']}\"")
        else:
            reason = "; ".join(r.get("reasons", [])) or r.get("error", "invalid request")
            lines.append(f"- REFUSED {e['name']}({target}): {reason}")
    return "\n".join(lines)


ACTION_TOOLS = {"create_upgrade_order", "open_remediation_ticket", "flag_device_for_replacement", "notify_employee"}

CHECK_WORDS = {
    "screen_lock": ["screen lock", "screen-lock", "screenlock", "lock screen", "screen_lock"],
    "os_up_to_date": ["os update", "out of date os", "os_up_to_date", "up to date", "up-to-date", "outdated os", "os patch"],
    "disk_encryption": ["encryption", "encrypted", "filevault", "bitlocker", "disk_encryption"],
}


def _check_in(q: str) -> str | None:
    for cid, words in CHECK_WORDS.items():
        if any(w in q for w in words):
            return cid
    return None


def _evidence_of(f: dict) -> list[str]:
    return f["evidence"][-2:] if len(f["evidence"]) > 1 else f["evidence"]


class OfflineBrain:
    name = "offline"

    # ------------------------------------------------------------------ planning
    def plan(self, state: dict, scope: TenantScope) -> dict:
        recipe = self._route(state["question"], scope)
        tools = list(dict.fromkeys(c["name"] for c in recipe.first))
        return {"intent": recipe.intent, "steps": recipe.steps, "tools": tools, "planner": "offline-router"}

    def replan(self, state: dict, scope: TenantScope, reason: str) -> dict | None:
        """A deterministic router has nothing new to plan with; keep the current plan."""
        return None

    def _route(self, question: str, scope: TenantScope) -> Recipe:
        q = question.lower()
        ids = [d for d in DEVICE_ID_RE.findall(question)]
        emps = EMP_RE.findall(question)
        wants = lambda *ws: any(w in q for w in ws)

        # ---------------- action intents
        if wants("ticket", "remediat"):
            return self._remediation(q, ids)
        if wants("replace", "replacement", "refresh"):
            return self._replacement(ids)
        if wants("upgrade", "order more", "add ram", "more ram", "bigger disk"):
            return self._upgrade(q, ids)
        if wants("notify", "tell ", "email", "message", "let them know", "alert the"):
            return self._notify(q, ids, emps)

        # ---------------- question answering / insights
        if ids:
            return Recipe("device_detail", [f"Fetch latest state and active findings for {', '.join(ids)}"],
                          [_call("get_device", device_id=d) for d in ids], composer=self._compose_device)
        if wants("insight", "summary", "overview", "health", "what should i", "anything i should", "status of the fleet",
                 "priorit", "report"):
            return Recipe("fleet_insights",
                          ["Get fleet overview", "Run all trend detectors (storage, RAM, battery, compliance drift)",
                           "Check unapproved software", "Rank and summarise findings with evidence"],
                          [_call("fleet_overview"), _call("detect_storage_pressure"), _call("detect_ram_pressure"),
                           _call("detect_battery_eol", include_ok=False), _call("detect_compliance_drift"),
                           _call("find_unapproved_software")],
                          composer=self._compose_insights)
        if wants("drift", "regress", "over time", "trend", "getting worse", "changed") and wants(
                "complian", "check", "screen", "lock", "polic"):
            return Recipe("compliance_drift", ["Compare first vs latest status of every check per device",
                                               "Report regressions with the transition snapshot"],
                          [_call("detect_compliance_drift")], composer=self._compose_drift)
        if wants("battery", "batteries"):
            return Recipe("battery_eol", ["Analyse battery cycles, condition and capacity trend"],
                          [_call("detect_battery_eol", include_ok=False)],
                          composer=lambda log: _fmt_findings(_results(log, "detect_battery_eol")[0],
                                                             "No batteries are at or approaching end of life",
                                                             "Batteries at or approaching end of life"))
        if wants("ram", "memory"):
            return Recipe("ram_pressure", ["Measure share of snapshots with RAM above threshold per device"],
                          [_call("detect_ram_pressure")],
                          composer=lambda log: _fmt_findings(_results(log, "detect_ram_pressure")[0],
                                                             "No devices are consistently RAM-constrained",
                                                             "Devices under sustained RAM pressure"))
        if wants("disk", "storage", "space"):
            if wants("consistent", "persistent", "trend", "over time", "always", "keep", "fill"):
                return Recipe("storage_pressure", ["Measure 30-day share of low-disk snapshots and fill rate"],
                              [_call("detect_storage_pressure")],
                              composer=lambda log: _fmt_findings(_results(log, "detect_storage_pressure")[0],
                                                                 "No devices are consistently low on storage",
                                                                 "Devices consistently low on storage"))
            m = re.search(r"(?:below|under|less than)\s*(\d+(?:\.\d+)?)\s*%", q)
            args = {"free_pct_below": float(m.group(1))} if m else {}
            return Recipe("low_disk", ["Find devices whose latest free disk is below threshold"],
                          [_call("find_low_disk_devices", **args)],
                          composer=lambda log: _fmt_findings(_results(log, "find_low_disk_devices")[0],
                                                             "No devices are low on disk space",
                                                             "Devices low on disk space"))
        m = re.search(r"(?:older than|below|before|earlier than|prior to)\s+(macos|windows)\s*([\d.]+)", q)
        if m:
            os_name = "macOS" if m.group(1) == "macos" else "Windows"
            return Recipe("os_older", [f"Compare each device's {os_name} version against {m.group(2)}"],
                          [_call("find_os_older_than", os_name=os_name, version=m.group(2))],
                          composer=lambda log: self._compose_os(_results(log, "find_os_older_than")[0], os_name, m.group(2)))
        if wants("unapproved", "unauthori", "risky", "not allowed", "shadow it", "torrent", "blacklist", "prohibited"):
            return Recipe("unapproved_software", ["Diff installed software against the approved list"],
                          [_call("find_unapproved_software")],
                          composer=lambda log: _fmt_findings(_results(log, "find_unapproved_software")[0],
                                                             "No unapproved software found",
                                                             "Devices with unapproved software"))
        if wants("complian", "failing", "fail ", "check"):
            sev = next((s for s in ("high", "medium", "low") if f"{s}-severity" in q or f"{s} severity" in q), None)
            args = {k: v for k, v in {"severity": sev, "check_id": _check_in(q),
                                      "laptops_only": wants("laptop", "notebook")}.items() if v}
            return Recipe("compliance_failures", ["Scan each device's latest compliance results"],
                          [_call("find_compliance_failures", **args)], composer=self._compose_compliance)
        apps = sorted({n for h in scope.devices() for n, _ in h.latest.software if n.lower() in q})
        if apps or wants("installed", "software", "app"):
            calls = [_call("search_installed_software", name=a) for a in apps] or [_call("find_unapproved_software")]
            return Recipe("software", ["Search the installed-software inventory"], calls,
                          composer=self._compose_software)
        if wants("device", "laptop", "fleet", "how many", "list", "machines", "computers", "macbook", "thinkpad", "dell"):
            model = next((m for m in ("MacBook Pro", "MacBook Air", "Mac mini", "ThinkPad", "Dell") if m.lower() in q), None)
            args = {k: v for k, v in {"model": model, "laptops_only": wants("laptop")}.items() if v}
            name = "list_devices" if args else "fleet_overview"
            return Recipe("inventory", ["Retrieve device inventory"], [_call(name, **args)],
                          composer=self._compose_inventory)
        return Recipe("out_of_scope", ["No telemetry tool fits this request"], [], composer=lambda log: (
            "I can only answer questions grounded in your fleet's device telemetry (disk, RAM, battery, OS, "
            "installed software, compliance) and propose IT actions. Try e.g. 'Which devices are low on disk "
            "space?' or 'Show compliance drift over time'."))

    # ------------------------------------------------------------------ action recipes
    def _remediation(self, q: str, ids: list[str]) -> Recipe:
        check = "unapproved_software" if any(w in q for w in ("software", "torrent", "unapproved")) else _check_in(q)
        if check == "unapproved_software":
            first = [_call("find_unapproved_software")]
        else:
            first = [_call("find_compliance_failures", **({"check_id": check} if check else {}))]
        first = first + [_call("get_device", device_id=d) for d in ids]

        def follow(log):
            res = _results(log, first[0]["name"])[0]
            calls = []
            for f in res["results"]:
                if ids and f["device_id"] not in ids:
                    continue
                cid = check or f["metrics"]["check_id"]
                note = (f"{f['finding']}. Please remediate {cid} on {f['device_id']} ({f['model']}).")
                calls.append(_call("open_remediation_ticket", device_id=f["device_id"], check_id=cid, note=note,
                                   evidence_ids=_evidence_of(f)))
            if ids and not calls:  # explicitly named device without a failing check -> let the policy decide
                calls = [_call("open_remediation_ticket", device_id=r["latest"]["device_id"],
                               check_id=check or "screen_lock", note="Requested by admin",
                               evidence_ids=[r["latest"]["evidence_id"]]) for r in _results(log, "get_device")]
            return calls
        return Recipe("propose_remediation", ["Find devices failing the check", "Propose one ticket per failing device",
                                              "Await human approval"], first, follow, self._compose_actions)

    def _replacement(self, ids: list[str]) -> Recipe:
        if ids:
            first = [_call("get_device", device_id=d) for d in ids]

            def follow(log):
                calls = []
                for r in _results(log, "get_device"):
                    dev = r["latest"]["device_id"]
                    reasons = [f["finding"] for f in r["results"]] or ["requested by admin"]
                    ev = [e for f in r["results"] for e in f["evidence"]][-3:] or [r["latest"]["evidence_id"]]
                    calls.append(_call("flag_device_for_replacement", device_id=dev, reason="; ".join(reasons[:3]),
                                       evidence_ids=ev))
                return calls
        else:
            first = [_call("detect_battery_eol", include_ok=False), _call("detect_ram_pressure"),
                     _call("detect_storage_pressure")]

            def follow(log):
                cands: dict[str, tuple[list[str], list[str]]] = {}
                for name in ("detect_battery_eol", "detect_ram_pressure", "detect_storage_pressure"):
                    for f in _results(log, name)[0]["results"]:
                        if f["status"] in ("end_of_life", "consistent"):
                            r, e = cands.setdefault(f["device_id"], ([], []))
                            r.append(f["finding"])
                            e += _evidence_of(f)
                return [_call("flag_device_for_replacement", device_id=d, reason="; ".join(r), evidence_ids=e)
                        for d, (r, e) in sorted(cands.items())]
        return Recipe("propose_replacement", ["Gather health evidence", "Propose replacement where criteria are met",
                                              "Await human approval"], first, follow, self._compose_actions)

    def _upgrade(self, q: str, ids: list[str]) -> Recipe:
        comp = "battery" if "battery" in q else "storage" if any(w in q for w in ("storage", "disk", "ssd")) else "ram"
        detector = {"ram": "detect_ram_pressure", "storage": "detect_storage_pressure", "battery": "detect_battery_eol"}[comp]
        first = [_call(detector)] + [_call("get_device", device_id=d) for d in ids]

        def spec_for(f: dict) -> str:
            if comp == "ram":
                return f"{f['metrics']['ram_gb'] * 2} GB"
            if comp == "storage":
                return f"{f['metrics']['disk_size_gb'] * 2} GB"
            return "OEM replacement battery"

        def follow(log):
            res = _results(log, detector)[0]
            calls = []
            for f in res["results"]:
                if ids and f["device_id"] not in ids:
                    continue
                if not ids and f["status"] not in ("consistent", "end_of_life"):
                    continue
                calls.append(_call("create_upgrade_order", device_id=f["device_id"], component=comp, spec=spec_for(f),
                                   justification=f["finding"], evidence_ids=_evidence_of(f)))
            for r in _results(log, "get_device"):  # named device with no qualifying finding: policy will judge
                dev = r["latest"]["device_id"]
                if not any(c["args"]["device_id"] == dev for c in calls):
                    cur = r["latest"]["ram_gb"] if comp == "ram" else r["latest"]["disk_size_gb"]
                    calls.append(_call("create_upgrade_order", device_id=dev, component=comp,
                                       spec=f"{cur * 2} GB" if comp != "battery" else "OEM replacement battery",
                                       justification="requested by admin", evidence_ids=[r["latest"]["evidence_id"]]))
            return calls
        return Recipe("propose_upgrade", [f"Find devices needing a {comp} upgrade", "Propose upgrade orders",
                                          "Await human approval"], first, follow, self._compose_actions)

    def _notify(self, q: str, ids: list[str], emps: list[str]) -> Recipe:
        if "battery" in q:
            first, topic = [_call("detect_battery_eol", include_ok=False)], "battery"
        elif any(w in q for w in ("disk", "storage", "space")):
            first, topic = [_call("find_low_disk_devices")], "disk"
        elif any(w in q for w in ("software", "torrent", "unapproved")):
            first, topic = [_call("find_unapproved_software")], "software"
        else:
            check = _check_in(q) or "screen_lock"
            first, topic = [_call("find_compliance_failures", check_id=check)], check

        def follow(log):
            res = _results(log, first[0]["name"])[0]
            by_emp: dict[str, list[dict]] = {}
            for f in res["results"]:
                if ids and f["device_id"] not in ids:
                    continue
                if emps and f["employee_id"] not in emps:
                    continue
                by_emp.setdefault(f["employee_id"], []).append(f)
            calls = []
            for emp, fs in sorted(by_emp.items()):
                devs = ", ".join(f"{f['model']} ({f['device_id']}): {f['finding']}" for f in fs)
                msg = f"Hi, IT here. We noticed an issue on your device - {devs}. Please contact IT or follow the self-service guide."
                calls.append(_call("notify_employee", employee_id=emp, message=msg[:480],
                                   evidence_ids=[e for f in fs for e in _evidence_of(f)][-3:]))
            return calls
        return Recipe("propose_notification", [f"Find employees affected by {topic} issues",
                                               "Draft one message per employee", "Await human approval"],
                      first, follow, self._compose_actions)

    # ------------------------------------------------------------------ composers
    def _compose_actions(self, log: list[dict]) -> str:
        reads = [e for e in log if e["name"] not in ACTION_TOOLS and not e["is_error"]]
        parts = []
        for e in reads:
            r = e["result"]
            if "count" in r:
                parts.append(f"{e['name']} returned {r['count']} result(s) [{r['query_id']}]")
            elif "latest" in r:
                parts.append(f"get_device {r['latest']['device_id']} [{r['latest']['evidence_id']}]")
        return "Evidence gathered: " + "; ".join(parts) + ".\n\n" + _action_summary(log)

    def _compose_device(self, log: list[dict]) -> str:
        out = []
        for e in log:
            if e["is_error"]:
                out.append(f"Could not retrieve {e['args'].get('device_id')}: {e['result'].get('error')}.")
                continue
            r = e["result"]
            s, w = r["latest"], r["window"]
            bat = s["battery"]
            out.append(
                f"{s['device_id']} ({s['model']}, {s['employee_id']}), latest snapshot {s['collected_at']} "
                f"[{s['evidence_id']}]: {s['os']}, RAM {s['ram_used_pct']}% used of {s['ram_gb']} GB "
                f"(30-day mean {w['mean_ram_used_pct']}%), disk {s['disk_free_pct']}% free of {s['disk_size_gb']} GB, "
                + (f"battery {bat['condition']} at {bat['cycle_count']} cycles" if bat else "no battery reported")
                + f", failing checks: {', '.join(s['failing_checks']) or 'none'}.")
            if r["results"]:
                out.append(f"Active findings [{r['query_id']}]:")
                out += [f"- {f['finding']} {_cite(f['evidence'], 2)}" for f in r["results"]]
            else:
                out.append(f"No active findings [{r['query_id']}].")
        return "\n".join(out)

    def _compose_os(self, res: dict, os_name: str, version: str) -> str:
        if res["count"] == 0:
            return f"0 devices run {os_name} older than {version} [{res['query_id']}]."
        lines = [f"{res['count']} device(s) run {os_name} older than {version} [{res['query_id']}]:"]
        lines += [f"- {f['device_id']} ({f['model']}, {f['employee_id']}): {f['finding']} {_cite(f['evidence'])}"
                  for f in res["results"]]
        return "\n".join(lines)

    def _compose_compliance(self, log: list[dict]) -> str:
        r = _results(log, "find_compliance_failures")[0]
        a = next(e["args"] for e in log if e["name"] == "find_compliance_failures")
        what = " ".join(filter(None, [a.get("severity") and f"{a['severity']}-severity",
                                      a.get("check_id"), "compliance check(s)"]))
        who = "laptops" if a.get("laptops_only") else "devices"
        if r["count"] == 0:
            return (f"No {who} are failing {what}. I scanned the latest snapshot of all {r['devices_scanned']} devices; "
                    f"checks in scope: {', '.join(r['checks_in_scope'])} [{r['query_id']}].")
        lines = [f"{r['count']} failing result(s) for {what} across {who} [{r['query_id']}]:"]
        lines += [f"- {f['device_id']} ({f['model']}, {f['employee_id']}): {f['finding']} {_cite(f['evidence'])}"
                  for f in r["results"]]
        return "\n".join(lines)

    def _compose_drift(self, log: list[dict]) -> str:
        r = _results(log, "detect_compliance_drift")[0]
        lines = []
        for cid, s in r["fleet_series"].items():
            lines.append(f"{cid}: {s['failing_devices_first_day']} failing device(s) on {s['first_day']} -> "
                         f"{s['failing_devices_last_day']} on {s['last_day']} [{r['query_id']}].")
        if r["regressions"]:
            lines.append(f"Regressions (pass -> fail, still failing) - {len(r['regressions'])}:")
            lines += [f"- {f['device_id']} ({f['model']}, {f['employee_id']}): {f['finding']}. {f['explanation']} "
                      f"{_cite(f['evidence'], 2)}" for f in r["regressions"]]
        else:
            lines.append("No checks regressed during the window.")
        if r["persistent_failures"]:
            by: dict[str, list[str]] = {}
            for f in r["persistent_failures"]:
                by.setdefault(f["metrics"]["check_id"], []).append(f["device_id"])
            for cid, ds in by.items():
                lines.append(f"Never passing in the window - {cid}: {len(ds)} device(s) [{r['query_id']}].")
        if r["improvements"]:
            lines.append(f"Improved (fail -> pass): {len(r['improvements'])} [{r['query_id']}].")
        return "\n".join(lines)

    def _compose_software(self, log: list[dict]) -> str:
        parts = []
        for e in log:
            if e["name"] == "search_installed_software":
                parts.append(_fmt_findings(e["result"], f"No devices have {e['args']['name']} installed",
                                           f"Devices with {e['args']['name']}"))
            elif e["name"] == "find_unapproved_software":
                parts.append(_fmt_findings(e["result"], "No unapproved software found", "Devices with unapproved software"))
        return "\n\n".join(parts)

    def _compose_inventory(self, log: list[dict]) -> str:
        e = log[0]
        r = e["result"]
        if e["name"] == "fleet_overview":
            return (f"{r['company']} has {r['device_count']} devices ({r['laptops']} laptops) used by "
                    f"{r['employee_count']} employees, as of {r['as_of'][:10]} [{r['query_id']}]. "
                    f"Models: {', '.join(f'{k} x{v}' for k, v in r['models'].items())}. "
                    f"OS: {', '.join(f'{k} x{v}' for k, v in r['os'].items())}.")
        lines = [f"{r['count']} matching device(s) [{r['query_id']}]:"]
        lines += [f"- {s['device_id']} ({s['model']}, {s['employee_id']}, {s['os']}) [{s['evidence_id']}]"
                  for s in r["results"]]
        return "\n".join(lines)

    def _compose_insights(self, log: list[dict]) -> str:
        ov = _results(log, "fleet_overview")[0]
        sections = [f"Fleet insights for {ov['company']} ({ov['device_count']} devices, 30 days to "
                    f"{ov['as_of'][:10]}) [{ov['query_id']}]."]
        n = 0

        def add(title: str, res: dict, keep: Callable[[dict], bool] = lambda f: True):
            nonlocal n
            fs = [f for f in res["results"] if keep(f)]
            if not fs:
                return
            n += 1
            sections.append(f"\n{n}. {title} - {len(fs)} device(s) [{res['query_id']}]")
            for f in fs:
                sections.append(f"   - Finding: {f['device_id']} ({f['model']}, {f['employee_id']}) {f['finding']}.\n"
                                f"     Why: {f['explanation']}\n     Evidence: {_cite(f['evidence'], 3)}")
        add("Batteries at end of life", _results(log, "detect_battery_eol")[0], lambda f: f["status"] == "end_of_life")
        add("Consistently low on storage", _results(log, "detect_storage_pressure")[0], lambda f: f["status"] == "consistent")
        add("Sustained RAM pressure", _results(log, "detect_ram_pressure")[0], lambda f: f["status"] == "consistent")
        drift = _results(log, "detect_compliance_drift")[0]
        if drift["regressions"]:
            n += 1
            sections.append(f"\n{n}. Compliance drift - {len(drift['regressions'])} regression(s) [{drift['query_id']}]")
            for cid, s in drift["fleet_series"].items():
                if s["failing_devices_last_day"] != s["failing_devices_first_day"]:
                    sections.append(f"   - {cid}: failing devices {s['failing_devices_first_day']} -> "
                                    f"{s['failing_devices_last_day']} over the window.")
            for f in drift["regressions"]:
                sections.append(f"   - {f['device_id']} ({f['employee_id']}): {f['finding']} {_cite(f['evidence'], 2)}")
        add("Unapproved software", _results(log, "find_unapproved_software")[0])
        add("RAM watchlist (intermittent pressure)", _results(log, "detect_ram_pressure")[0], lambda f: f["status"] == "watch")
        if n == 0:
            sections.append("No significant findings.")
        sections.append("\nSuggested next steps: replace end-of-life batteries, upgrade or replace storage/RAM-"
                        "constrained devices, open screen-lock remediation tickets. Ask me to propose any of these.")
        return "\n".join(sections)

    # ------------------------------------------------------------------ loop contract
    def act(self, state: dict, scope: TenantScope) -> dict:
        recipe = self._route(state["question"], scope)
        calls = recipe.next_calls(state.get("steps", 0), state.get("tool_log", []))
        if calls:
            return {"calls": [{"id": f"c{len(state.get('tool_log', [])) + i + 1}", **c} for i, c in enumerate(calls)]}
        return {"answer": recipe.composer(state.get("tool_log", []))}

    def revise(self, state: dict, feedback: list[str]) -> None:
        """Deterministic brain cannot rephrase; the graph falls back to a safe answer."""

    def tool_results_message(self, results: list[dict]) -> dict | None:
        return None
