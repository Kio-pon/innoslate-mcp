"""Per-project workspace folder and the write audit log."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from . import config
from .urls import Project


def workspace(project: Project) -> Path:
    p = config.home() / project.key
    p.mkdir(parents=True, exist_ok=True)
    return p


def audit(project: Project, event: str, **data: Any) -> None:
    """One JSON line per write. Holds ids, numbers and outcomes, never credentials."""
    row = {"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "project": project.key, "event": event, **data}
    with (workspace(project) / "audit.log").open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def number_index(project: Project) -> dict[str, list[int]]:
    """number -> entity ids, from the last snapshot. Only a lookup aid: every hit is re-read live."""
    snap = workspace(project) / "snapshot.json"
    if not snap.exists():
        return {}
    try:
        data = json.loads(snap.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    idx: dict[str, list[int]] = {}
    for e in data.get("entities", []):
        if e.get("n"):
            idx.setdefault(e["n"], []).append(e["id"])
    return idx


def read_file(project: Project, name: str, offset: int = 0, max_chars: int = 30000) -> dict:
    ws = workspace(project)
    aliases = {"map": "PROJECT_MAP.md", "nomenclature": "NOMENCLATURE.md", "audit": "audit.log"}
    rel = aliases.get(name.lower(), name)
    path = (ws / rel).resolve()
    if ws.resolve() not in path.parents and path != ws.resolve():
        raise ValueError("Only files inside the project workspace can be read with this tool.")
    if not path.exists():
        files = sorted(str(p.relative_to(ws)) for p in ws.rglob("*") if p.is_file())
        raise FileNotFoundError(f"{rel} does not exist. Files: {files}. Run innoslate_initialize first if the list is empty.")
    text = path.read_text(encoding="utf-8", errors="replace")
    offset = max(0, int(offset))
    chunk = text[offset:offset + max(1000, min(int(max_chars), 200000))]
    end = offset + len(chunk)
    return {"file": str(path), "total_chars": len(text), "offset": offset,
            "next_offset": end if end < len(text) else None, "text": chunk}
