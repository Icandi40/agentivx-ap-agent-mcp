"""Append-only audit log.

Each entry is one JSON line holding the hash of the entry before it, so editing or
deleting a past line breaks the chain and `verify()` reports where.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ap_agent.models import to_jsonable

GENESIS = "0" * 64


def _digest(entry: dict[str, Any]) -> str:
    body = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._last = self._read_last_hash()

    def _read_last_hash(self) -> str:
        if not self.path.exists():
            return GENESIS
        last = GENESIS
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    last = json.loads(line)["hash"]
        return last

    def write(self, event: str, actor: str, **details: Any) -> dict[str, Any]:
        with self._lock:
            entry = {
                "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "event": event,
                "actor": actor,
                "details": to_jsonable(details),
                "prev": self._last,
            }
            entry["hash"] = _digest(entry)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
            self._last = entry["hash"]
            return entry

    def entries(self, proposal_id: str | None = None, invoice_ref: str | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                e = json.loads(line)
                d = e.get("details", {})
                if proposal_id and d.get("proposal_id") != proposal_id:
                    continue
                if invoice_ref and d.get("invoice_ref") != invoice_ref:
                    continue
                out.append(e)
        return out

    def verify(self) -> tuple[bool, str]:
        prev = GENESIS
        for i, e in enumerate(self.entries(), start=1):
            claimed = e.pop("hash")
            if e["prev"] != prev or _digest(e) != claimed:
                return False, f"chain broken at line {i}"
            prev = claimed
        return True, "ok"
