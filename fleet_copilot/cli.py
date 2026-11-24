"""Command-line interface.

    python -m fleet_copilot tenants
    python -m fleet_copilot ask   --tenant acme "Which devices are low on disk space?"
    python -m fleet_copilot ask   --tenant acme "Open tickets for screen lock failures" --approve P1,P2
    python -m fleet_copilot chat  --tenant acme
    python -m fleet_copilot audit --tail 20 [--verify]
"""
from __future__ import annotations

import argparse
import json
import sys

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from . import config
from .approvals import sign_decision
from .copilot import FleetCopilot, TurnResult, load_fleet

console = Console()


def resolve_tenant(t: str) -> str:
    t = t.strip().lower()
    for cid, name in config.COMPANY_NAMES.items():
        if t in (cid, name.lower()):
            return cid
    raise SystemExit(f"unknown tenant {t!r}; choose from {', '.join(config.COMPANY_NAMES.values())}")


def show(r: TurnResult, verbose: bool) -> None:
    if verbose:
        console.print(Panel(escape("\n".join(r.trace)), title="agent trace", border_style="dim"))
    if r.final is not None:
        console.print(Panel(escape(r.final), title="answer", border_style="green" if r.verification.get("passed", True) else "red"))
    if r.awaiting_approval:
        draft = r.state.get("draft", "")
        console.print(Panel(escape(draft), title="answer (actions pending approval)", border_style="yellow"))
        t = Table(title="Proposals awaiting approval")
        for col in ("id", "action", "target", "args", "policy"):
            t.add_column(col)
        for p in r.awaiting_approval:
            args = {k: v for k, v in p["args"].items() if k not in ("evidence_ids", "device_id", "employee_id")}
            t.add_row(p["proposal_id"], p["action"], p["target_device"] or p["target_employee"] or "",
                      escape(json.dumps(args)[:80]), escape("; ".join(p["policy"]["reasons"])[:80]))
        console.print(t)


def parse_decisions(spec: str | None, pending: list[dict]) -> dict[str, str]:
    ids = [p["proposal_id"] for p in pending]
    if not spec or spec == "none":
        return {i: "reject" for i in ids}
    chosen = set(ids) if spec == "all" else {s.strip() for s in spec.split(",")}
    return {i: "approve" if i in chosen else "reject" for i in ids}


def cmd_ask(a) -> int:
    c = FleetCopilot(resolve_tenant(a.tenant), mode=a.mode)
    r = c.ask(a.question)
    show(r, a.verbose)
    if r.awaiting_approval:
        if a.defer:
            console.print(f"[yellow]Waiting for approval on thread {r.thread_id}. Decide later with: "
                          f"fleet_copilot decide --tenant {a.tenant} --thread {r.thread_id} --approve P1,... "
                          f"(needs FLEET_CHECKPOINT_DB to survive this process).[/yellow]")
            return 0
        if a.approve is None:
            console.print("[yellow]Nothing executed. Re-run with --approve P1,P2 | all, or use `chat` to decide "
                          "interactively. Rejecting all pending proposals.[/yellow]")
        r = _decide(c, r.thread_id, parse_decisions(a.approve, r.awaiting_approval), a.approver)
        show(r, False)
    return 0


def _decide(c: FleetCopilot, thread_id: str, decisions: dict[str, str], approver: str) -> TurnResult:
    # The CLI stands in for the approval channel: it signs only if it holds FLEET_APPROVAL_SECRET.
    return c.decide(thread_id, decisions, approver, sign_decision(thread_id, approver, decisions))


def cmd_decide(a) -> int:
    """Decide on a turn that is waiting for approval, e.g. after a restart (needs FLEET_CHECKPOINT_DB)."""
    c = FleetCopilot(resolve_tenant(a.tenant), mode=a.mode)
    pending = c.pending(a.thread)
    if not pending:
        console.print(f"[yellow]No proposals awaiting approval on thread {a.thread}.[/yellow]")
        return 1
    show(_decide(c, a.thread, parse_decisions(a.approve, pending), a.approver), False)
    return 0


def cmd_chat(a) -> int:
    tenant = resolve_tenant(a.tenant)
    c = FleetCopilot(tenant, mode=a.mode)
    console.print(f"[bold]Fleet Copilot[/bold] - {config.COMPANY_NAMES[tenant]} - brain: {c.mode} - "
                  f"session {c.session_id}. Type 'exit' to quit.")
    while True:
        try:
            q = Prompt.ask("[cyan]admin[/cyan]")
        except (EOFError, KeyboardInterrupt):
            return 0
        if q.strip().lower() in ("exit", "quit"):
            return 0
        if not q.strip():
            continue
        r = c.ask(q)
        show(r, a.verbose)
        if r.awaiting_approval:
            spec = Prompt.ask("Approve which proposals? (comma-separated ids, 'all' or 'none')", default="none")
            r = _decide(c, r.thread_id, parse_decisions(spec, r.awaiting_approval), a.approver)
            show(r, False)


def cmd_audit(a) -> int:
    from .audit import AuditLog
    log = AuditLog()
    rows = log.entries()[-a.tail:]
    for r in rows:
        console.print(escape(f"{r['ts']} {r['tenant']:12} {r['session_id']} {r['actor']:16} {r['event']:28} "
                             f"{json.dumps(r['payload'], default=str)[:110]}"))
    if a.verify:
        console.print(f"hash chain intact: {log.verify()}")
    return 0


def cmd_tenants(a) -> int:
    fleet = load_fleet()
    for cid in fleet.company_ids:
        s = fleet.scope(cid)
        console.print(f"{config.COMPANY_NAMES.get(cid, cid):8} {cid:12} {len(s.devices())} devices, "
                      f"{len(s.employee_ids())} employees")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fleet_copilot", description="Agentic Fleet Copilot")
    sub = p.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--tenant", required=True, help="acme | globex | initech (or company id)")
    common.add_argument("--mode", choices=["offline", "claude", "langchain"], default=None,
                        help="brain to use (default: claude if ANTHROPIC_API_KEY is set, else offline; langchain uses FLEET_COPILOT_LC_MODEL)")
    common.add_argument("--approver", default="cli-admin", help="name recorded in the audit log for decisions")
    common.add_argument("-v", "--verbose", action="store_true", help="show plan / tool / verifier trace")
    s = sub.add_parser("ask", parents=[common], help="one question")
    s.add_argument("question")
    s.add_argument("--approve", default=None, help="proposal ids to approve: P1,P2 | all | none")
    s.add_argument("--defer", action="store_true", help="leave proposals pending; decide later with `decide`")
    s.set_defaults(fn=cmd_ask)
    s = sub.add_parser("decide", parents=[common], help="approve/reject a waiting turn (durable checkpoints)")
    s.add_argument("--thread", required=True, help="thread id shown when the turn paused")
    s.add_argument("--approve", default="none", help="proposal ids to approve: P1,P2 | all | none")
    s.set_defaults(fn=cmd_decide)
    s = sub.add_parser("chat", parents=[common], help="interactive session")
    s.set_defaults(fn=cmd_chat)
    s = sub.add_parser("audit", help="show audit log")
    s.add_argument("--tail", type=int, default=30)
    s.add_argument("--verify", action="store_true")
    s.set_defaults(fn=cmd_audit)
    s = sub.add_parser("tenants", help="list tenants")
    s.set_defaults(fn=cmd_tenants)
    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except ValueError as e:   # configuration errors (missing/unknown model, bad tenant) - no traceback
        console.print(f"[red]Configuration error:[/red] {escape(str(e))}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
