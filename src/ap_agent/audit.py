"""Append-only audit log.

Each entry is one JSON line holding the hash of the entry before it, so editing or
deleting a past line breaks the chain and `verify()` reports where.

Several processes write this file (the review page, the MCP server, CLI commands).
Every write takes a cross-process lock and chains to the last line actually on disk,
never to a copy held in memory, so interleaved writers keep the chain intact.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ap_agent.filelock import exclusive
from ap_agent.models import to_jsonable

GENESIS = "0" * 64


def _digest(entry: dict[str, Any]) -> str:
    body = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # (size, mtime_ns, hash) of the file as this instance last left it. Only a shortcut:
        # if another process appended since, size or mtime differ and the tail is re-read.
        self._seen: tuple[int, int, str] | None = None

    def _stamp(self) -> tuple[int, int] | None:
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return None
        return st.st_size, st.st_mtime_ns

    def _tail_hash(self) -> str:
        """Hash of the last entry on disk. Call only while holding the file lock."""
        stamp = self._stamp()
        if stamp is None:
            return GENESIS
        if self._seen is not None and self._seen[:2] == stamp:
            return self._seen[2]
        return self._read_tail_hash()

    def _read_tail_hash(self) -> str:
        if not self.path.exists():
            return GENESIS
        with open(self.path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            chunk = 65536
            while True:
                start = max(0, size - chunk)
                fh.seek(start)
                lines = [ln for ln in fh.read(size - start).splitlines() if ln.strip()]
                # The first line of a partial read may be cut off; only trust it at offset 0.
                if lines and (start == 0 or len(lines) > 1):
                    return str(json.loads(lines[-1])["hash"])
                if start == 0:
                    return GENESIS
                chunk *= 4

    def write(self, event: str, actor: str, **details: Any) -> dict[str, Any]:
        with exclusive(self.path):
            entry = {
                "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "event": event,
                "actor": actor,
                "details": to_jsonable(details),
                "prev": self._tail_hash(),
            }
            entry["hash"] = _digest(entry)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            stamp = self._stamp()
            self._seen = (*stamp, entry["hash"]) if stamp else None
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
