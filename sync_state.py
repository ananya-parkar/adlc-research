# sync_state.py
"""
Tracks the last successful sync timestamp per source, so a connector
can do incremental fetches (only new/updated items) instead of
re-pulling everything on every run.

Stored as local JSON for now — swap this for a Postgres table
(one row per source/project) when you move off JSON storage.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

STATE_PATH = Path(__file__).parent / "storage" / "sync_state.json"

def get_last_sync(source_key: str) -> Optional[datetime]:
    if not STATE_PATH.exists():
        return None
    state = json.loads(STATE_PATH.read_text())
    raw = state.get(source_key)
    if not raw:
        return None
    return datetime.fromisoformat(raw)


def set_last_sync(source_key: str, when: Optional[datetime] = None) -> None:
    when = when or datetime.now(timezone.utc)
    state = {}
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text())
    state[source_key] = when.isoformat()
    STATE_PATH.write_text(json.dumps(state, indent=2))