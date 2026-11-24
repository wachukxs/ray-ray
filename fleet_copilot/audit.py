"""Append-only, hash-chained audit log (JSONL).

Every tool call, refusal, proposal, approval decision and executed action is recorded
with tenant, session, actor and the evidence IDs involved. Each line carries the hash
of the previous line, so truncation or edits are detectable with `verify()`.
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import config

_LOCK = threading.Lock()


def _read_last_line(path: Path) -> str | None:
    """Last non-empty line, read backwards from the end (O(line length), not O(file))."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        pos = f.tell()
        buf = b""
        while pos > 0:
            step = min(4096, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
            lines = [l for l in buf.split(b"\n") if l.strip()]
            if len(lines) > 1 or (lines and pos == 0):
                return lines[-1].decode("utf-8")
    return None


class AuditLog:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or config.STATE_DIR / "audit.jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # (file size, hash of last entry). Valid while nobody else has appended to the file;
        # a size mismatch (another writer) falls back to reading only the file's last line.
        self._tail: tuple[int, str] | None = None

    def _last_hash(self) -> str:
        if not self.path.exists():
            return "genesis"
        size = self.path.stat().st_size
        if self._tail and self._tail[0] == size:
            return self._tail[1]
        last = _read_last_line(self.path) if size else None
        return json.loads(last)["hash"] if last else "genesis"

    def record(self, *, event: str, tenant: str, session_id: str, actor: str = "agent",
               payload: dict | None = None, evidence: list[str] | None = None) -> dict:
        with _LOCK:
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "session_id": session_id,
                "tenant": tenant,
                "actor": actor,
                "event": event,
                "payload": payload or {},
                "evidence": evidence or [],
                "prev_hash": self._last_hash(),
            }
            body = json.dumps(entry, sort_keys=True, default=str)
            entry["hash"] = hashlib.sha256(body.encode()).hexdigest()[:16]
            with open(self.path, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(entry, default=str) + "\n")
            self._tail = (self.path.stat().st_size, entry["hash"])
            return entry

    def entries(self, session_id: str | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip()]
        return [r for r in rows if session_id is None or r["session_id"] == session_id]

    def verify(self) -> bool:
        prev = "genesis"
        for r in self.entries():
            h = r.pop("hash")
            if r["prev_hash"] != prev:
                return False
            if hashlib.sha256(json.dumps(r, sort_keys=True, default=str).encode()).hexdigest()[:16] != h:
                return False
            prev = h
        return True
