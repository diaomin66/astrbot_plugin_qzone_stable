"""Persistent storage helpers."""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

from .models import BridgeState
from .utils import ensure_dir, now_iso


class StateStore:
    def __init__(self, root: Path):
        self.root = ensure_dir(root)
        self.path = self.root / "state.json"

    def read(self) -> BridgeState:
        if not self.path.exists():
            return BridgeState()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            backup = self.root / f"state.corrupt.{secrets.token_hex(4)}.json"
            try:
                self.path.replace(backup)
            except Exception:
                pass
            return BridgeState()
        return BridgeState.from_dict(payload)

    def write(self, state: BridgeState) -> None:
        payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        tmp = self.path.with_name(f"{self.path.name}.tmp.{secrets.token_hex(4)}")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self.path)

    def update(self, updater) -> BridgeState:
        state = self.read()
        updater(state)
        self.write(state)
        return state


def ensure_state_secret(state: BridgeState) -> BridgeState:
    if not state.runtime.secret:
        state.runtime.secret = secrets.token_urlsafe(32)
        state.runtime.started_at = state.runtime.started_at or now_iso()
    return state
