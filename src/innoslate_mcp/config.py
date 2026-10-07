"""Settings, workspace paths and the local credentials file.

Everything lives under one home directory (INNOSLATE_MCP_HOME, default ~/.innoslate-mcp):

    .env                  optional INNOSLATE_USERNAME / INNOSLATE_PASSWORD
    browser-profile/      Chromium profile the server owns
    <org>-<project>/      one workspace per Innoslate project
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path


def home() -> Path:
    p = Path(os.environ.get("INNOSLATE_MCP_HOME") or Path.home() / ".innoslate-mcp").expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k.startswith("export "):
            k = k[7:].strip()
        out[k] = v.strip().strip('"').strip("'")
    return out


def setting(name: str, default: str = "") -> str:
    """Process environment first, then the .env file in the home directory."""
    if os.environ.get(name):
        return os.environ[name]
    return _parse_env_file(home() / ".env").get(name, default)


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str

    def __repr__(self) -> str:  # never let a password reach a log or a traceback
        return f"Credentials(username={self.username!r}, password=<hidden>)"


def credentials() -> Credentials | None:
    user, pw = setting("INNOSLATE_USERNAME"), setting("INNOSLATE_PASSWORD")
    if user and pw:
        return Credentials(user, pw)
    return None


def env_file_warning() -> str | None:
    """On POSIX, warn when the .env file holding a password is readable by others."""
    path = home() / ".env"
    if os.name != "posix" or not path.exists():
        return None
    mode = path.stat().st_mode
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        return f"{path} is readable by other users. Run: chmod 600 '{path}'"
    return None


def headless() -> bool:
    return setting("INNOSLATE_HEADLESS", "").lower() in ("1", "true", "yes")


def cdp_url() -> str:
    return setting("INNOSLATE_CDP_URL", "")


def profile_dir() -> Path:
    p = Path(setting("INNOSLATE_PROFILE_DIR") or home() / "browser-profile").expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def max_upload_mb() -> int:
    try:
        return int(setting("INNOSLATE_MAX_UPLOAD_MB", "25"))
    except ValueError:
        return 25
