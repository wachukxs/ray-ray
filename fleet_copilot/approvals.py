"""Who may approve what, and how a decision proves where it came from.

Three checks run in the graph's approval node before any proposal can become `approved`:

1. **Authenticity.** When `FLEET_APPROVAL_SECRET` is set, a decision must carry an HMAC signature
   over (thread, approver, decisions). The signing side is the approval channel (admin UI, Slack
   app), which authenticates the human and holds the secret; the chat process only verifies. A
   chat message can therefore never approve anything, whatever it says. Without a secret (local
   development) decisions are accepted unsigned and the audit log records `signed: false`.
2. **Authorization.** The approver must be in the approver directory, scoped to the tenant, and
   hold a role allowed to approve that action type (e.g. procurement for upgrade orders).
3. **Freshness.** Checked at execution: a proposal expires after `PROPOSAL_TTL_HOURS`, or as soon
   as newer telemetry arrives than the data it was proposed on (its evidence may be stale).

The directory comes from `FLEET_APPROVERS_FILE` (JSON, see approvers.example.json). Without one,
a small development directory is used so the CLI, tests and evals run out of the box.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ACTION_ROLES = {
    "open_remediation_ticket": ["it_ops", "it_admin"],
    "notify_employee": ["it_ops", "it_admin"],
    "create_upgrade_order": ["procurement", "it_admin"],
    "flag_device_for_replacement": ["procurement", "it_admin"],
}

# Development-only identities (used when FLEET_APPROVERS_FILE is unset).
DEV_APPROVERS = {
    "cli-admin": {"roles": ["it_admin"], "tenants": ["*"]},
    "eval-harness": {"roles": ["it_admin"], "tenants": ["*"]},
    "alice": {"roles": ["it_admin"], "tenants": ["*"]},
    "bob": {"roles": ["it_ops"], "tenants": ["*"]},
    "carol": {"roles": ["procurement"], "tenants": ["acme-001"]},
}


@dataclass
class ApproverDirectory:
    approvers: dict[str, dict] = field(default_factory=lambda: dict(DEV_APPROVERS))
    action_roles: dict[str, list[str]] = field(default_factory=lambda: dict(DEFAULT_ACTION_ROLES))
    source: str = "dev-defaults"

    @classmethod
    def load(cls, path: str | Path | None = None) -> "ApproverDirectory":
        path = path or os.environ.get("FLEET_APPROVERS_FILE")
        if not path:
            return cls()
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(data["approvers"], {**DEFAULT_ACTION_ROLES, **data.get("action_roles", {})}, str(path))

    def authorize(self, approver: str, tenant: str, action: str) -> tuple[bool, str]:
        who = self.approvers.get(approver)
        if who is None:
            return False, f"{approver!r} is not a registered approver"
        tenants = who.get("tenants", [])
        if "*" not in tenants and tenant not in tenants:
            return False, f"{approver!r} cannot approve for tenant {tenant}"
        allowed = self.action_roles.get(action, [])
        if not set(who.get("roles", [])) & set(allowed):
            return False, f"{approver!r} lacks a role for {action} (needs one of {allowed})"
        return True, f"authorized via role {sorted(set(who['roles']) & set(allowed))[0]}"


def _secret() -> bytes | None:
    s = os.environ.get("FLEET_APPROVAL_SECRET")
    return s.encode() if s else None


def _message(thread_id: str, approver: str, decisions: dict[str, str]) -> bytes:
    return json.dumps([thread_id, approver, sorted(decisions.items())], separators=(",", ":")).encode()


def sign_decision(thread_id: str, approver: str, decisions: dict[str, str], secret: bytes | None = None) -> str | None:
    """Run by the approval channel after it has authenticated `approver`."""
    secret = secret or _secret()
    if not secret:
        return None
    return hmac.new(secret, _message(thread_id, approver, decisions), hashlib.sha256).hexdigest()


def verify_signature(thread_id: str, approver: str, decisions: dict[str, str], signature: str | None) -> tuple[bool, bool]:
    """Returns (ok, signed). With no secret configured every decision is accepted, unsigned."""
    secret = _secret()
    if not secret:
        return True, False
    if not signature:
        return False, True
    expected = sign_decision(thread_id, approver, decisions, secret)
    return hmac.compare_digest(expected, signature), True


def idempotency_key(tenant: str, thread_id: str, proposal: dict) -> str:
    """Stable per approved proposal. Passed to the downstream system (ticketing, procurement) so a
    retried execution never creates a second ticket or order."""
    args = {k: v for k, v in proposal["args"].items() if k != "evidence_ids"}
    body = json.dumps([tenant, thread_id, proposal["proposal_id"], proposal["action"], args], sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()[:32]
