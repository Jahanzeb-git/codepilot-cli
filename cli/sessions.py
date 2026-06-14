"""
cli/sessions.py  –  Session file helpers
"""

from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime
from typing import NamedTuple


# Sessions are stored relative to the user's config dir when installed via pipx,
# falling back to a local ./sessions directory for development.
def _session_dir() -> Path:
    import os
    xdg = os.environ.get("XDG_DATA_HOME", "")
    if xdg:
        d = Path(xdg) / "codepilot" / "sessions"
    else:
        d = Path.home() / ".local" / "share" / "codepilot" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


SESSION_DIR = _session_dir()


class SessionInfo(NamedTuple):
    session_id: str
    path: Path
    updated_at: str
    message_count: int


def list_sessions() -> list[SessionInfo]:
    """Return all saved sessions sorted by most-recently-modified."""
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    sessions: list[SessionInfo] = []
    for f in SESSION_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            messages = data.get("messages", [])
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            sessions.append(SessionInfo(
                session_id=f.stem,
                path=f,
                updated_at=mtime.strftime("%Y-%m-%d %H:%M"),
                message_count=len(messages),
            ))
        except Exception:
            continue
    return sorted(sessions, key=lambda s: s.path.stat().st_mtime, reverse=True)


def next_session_id() -> str:
    """Generate the next sequential devtool<N> id."""
    existing = list_sessions()
    nums = []
    for s in existing:
        sid = s.session_id
        if sid.startswith("devtool") and sid[7:].isdigit():
            nums.append(int(sid[7:]))
    next_n = (max(nums) + 1) if nums else 100
    return f"devtool{next_n}"