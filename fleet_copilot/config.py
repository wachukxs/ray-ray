"""Single source of truth for paths, thresholds and policy constants.

Every threshold the agent uses to call something "low", "constrained" or "end-of-life"
lives here, so tool output, the evidence policy, and the eval ground truth all share
one definition. Tools echo the definition they applied back to the caller.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# Load <repo>/.env (gitignored) before reading any setting. Variables already set in the
# shell win over the file, so CI or a one-off `$env:...` override still works.
load_dotenv(ROOT / ".env", override=False)
DATA_PATH = Path(os.environ.get("FLEET_DATA_PATH", ROOT / "data" / "raw" / "device-telemetry-dataset.ndjson"))
STATE_DIR = Path(os.environ.get("FLEET_STATE_DIR", ROOT / "state"))

# Human-friendly tenant names. Used by the input guard to detect cross-tenant references.
COMPANY_NAMES: dict[str, str] = {
    "acme-001": "Acme",
    "globex-002": "Globex",
    "initech-003": "Initech",
}

# --- Storage -------------------------------------------------------------------------
LOW_DISK_FREE_PCT = 10.0          # latest snapshot free space below this => "low on disk"
STORAGE_PERSISTENT_SHARE = 0.5    # >= this share of window snapshots low => "consistently low"

# --- Memory --------------------------------------------------------------------------
RAM_PRESSURE_USED_PCT = 90.0      # snapshot with used RAM above this => "under pressure"
RAM_PERSISTENT_SHARE = 0.5        # >= this share of snapshots => "consistently constrained"
RAM_WATCH_SHARE = 0.2             # >= this share (but below persistent) => "watchlist"

# --- Battery -------------------------------------------------------------------------
BATTERY_EOL_CYCLES = 1000         # Apple/Lenovo/Dell rate most modern packs ~1000 cycles
BATTERY_APPROACHING_CYCLES = 800
BATTERY_CAPACITY_DROP_PCT = 3.0   # full-charge-capacity drop over the window that counts as degradation
BATTERY_SERVICE_CONDITIONS = {"Service Recommended", "Replace Soon", "Replace Now", "Service Battery"}

# --- OS / software -------------------------------------------------------------------
APPROVED_SOFTWARE = {"Google Chrome", "Slack", "Zoom", "1Password", "Visual Studio Code"}

# --- Agent ---------------------------------------------------------------------------
MAX_AGENT_STEPS = 8               # hard cap on agent<->tool iterations per turn
MAX_VERIFY_RETRIES = 1            # re-plan attempts when the verifier rejects an answer
MAX_NOTIFY_MESSAGE_CHARS = 500

CLAUDE_MODEL = os.environ.get("FLEET_COPILOT_MODEL", "claude-opus-5-5")
CLAUDE_EFFORT = os.environ.get("FLEET_COPILOT_EFFORT", "medium")

# LangChain brain: any provider init_chat_model supports, as "provider:model".
LANGCHAIN_MODEL = os.environ.get("FLEET_COPILOT_LC_MODEL") or None
_lc_temp = os.environ.get("FLEET_COPILOT_LC_TEMPERATURE")
LANGCHAIN_TEMPERATURE = float(_lc_temp) if _lc_temp else None


def default_mode() -> str:
    """offline unless explicitly asked for claude (or a key is present and mode unset)."""
    mode = os.environ.get("FLEET_COPILOT_MODE")
    if mode:
        return mode
    return "claude" if os.environ.get("ANTHROPIC_API_KEY") else "offline"
