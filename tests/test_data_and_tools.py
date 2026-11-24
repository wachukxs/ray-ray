import pytest

from fleet_copilot import analytics
from fleet_copilot.data import NotFoundInTenant, parse_version
from fleet_copilot.tools import TOOLS, run_tool


def test_dataset_shape(fleet):
    assert fleet.company_ids == ["acme-001", "globex-002", "initech-003"]
    assert sum(len(fleet.scope(c).devices()) for c in fleet.company_ids) == 25
    assert all(len(h.snapshots) == 30 for c in fleet.company_ids for h in fleet.scope(c).devices())


def test_scope_contains_only_own_devices(fleet):
    acme = fleet.scope("acme-001")
    assert {h.latest.company_id for h in acme.devices()} == {"acme-001"}


def test_foreign_device_lookup_looks_like_missing_device(fleet):
    acme = fleet.scope("acme-001")
    foreign = fleet.scope("globex-002").devices()[0].device_id
    with pytest.raises(NotFoundInTenant) as e1:
        acme.device(foreign)
    with pytest.raises(NotFoundInTenant) as e2:
        acme.device("DOESNOTEXIST")
    # same message shape: no oracle revealing that the foreign device exists
    assert str(e1.value).replace(foreign, "X") == str(e2.value).replace("DOESNOTEXIST", "X")


def test_version_compare():
    assert parse_version("14.5") < parse_version("15")
    assert parse_version("13.6") < parse_version("14.5")
    assert not parse_version("15.3") < parse_version("15")
    assert parse_version("10 22H2") < parse_version("11")


def test_no_tool_accepts_a_tenant_argument():
    for t in TOOLS.values():
        props = t.input_model.model_json_schema().get("properties", {})
        assert not {"company_id", "tenant", "tenant_id"} & set(props), t.name


def test_tool_rejects_injected_tenant_argument(ctx_for):
    res, err = run_tool(ctx_for("acme-001"), "find_low_disk_devices", {"company_id": "globex-002"})
    assert err and res["error"] == "invalid arguments"


def test_tool_results_are_registered_as_evidence(ctx_for):
    ctx = ctx_for("acme-001")
    res, err = run_tool(ctx, "find_low_disk_devices", {})
    assert not err and res["count"] == 2
    assert res["query_id"] in ctx.ledger.records
    for f in res["results"]:
        for ev in f["evidence"]:
            assert ev in ctx.ledger.records


def test_every_tool_call_is_audited(ctx_for):
    ctx = ctx_for("initech-003")
    run_tool(ctx, "detect_ram_pressure", {})
    run_tool(ctx, "get_device", {"device_id": "NOPE"})
    events = [e for e in ctx.audit.entries() if e["event"] == "tool_call"]
    assert [e["payload"]["tool"] for e in events] == ["detect_ram_pressure", "get_device"]
    assert events[1]["payload"]["is_error"] is True
    assert ctx.audit.verify()


def test_ram_pressure_distinguishes_spikes_from_sustained(fleet):
    g = {f.device_id: f.status for f in analytics.ram_pressure(fleet.scope("globex-002"))}
    assert g == {"JRZSGXVMKE6M": "watch"}            # 8/30 snapshots: watchlist, not "consistent"
    i = {f.device_id: f.status for f in analytics.ram_pressure(fleet.scope("initech-003"))}
    assert i == {"I3JLKILKQTAC": "consistent"}


def test_battery_gaps_are_not_treated_as_removal(fleet):
    mt7 = fleet.scope("acme-001").device("MT7PJB7N5LRE")
    assert mt7.is_laptop and len(mt7.battery_snapshots) == 25


def test_drift_cites_the_transition(fleet):
    d = analytics.compliance_drift(fleet.scope("acme-001"))
    f = next(x for x in d["regressions"] if x.device_id == "1LYSSFD074BB")
    pass_ev, fail_ev = f.evidence[0], f.evidence[1]
    scope = fleet.scope("acme-001")
    assert scope.snapshot(pass_ev).check("screen_lock").status == "pass"
    assert scope.snapshot(fail_ev).check("screen_lock").status == "fail"
