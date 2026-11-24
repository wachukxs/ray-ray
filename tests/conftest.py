import pytest

from fleet_copilot.actions import ActionStore
from fleet_copilot.audit import AuditLog
from fleet_copilot.copilot import load_fleet
from fleet_copilot.tools import ToolContext


@pytest.fixture(scope="session")
def fleet():
    return load_fleet()


@pytest.fixture
def state_dir(tmp_path):
    return tmp_path


@pytest.fixture
def ctx_for(fleet, tmp_path):
    def make(tenant="acme-001"):
        return ToolContext(fleet.scope(tenant), "test", AuditLog(tmp_path / "audit.jsonl"),
                           ActionStore(tmp_path / "actions.jsonl"))
    return make
