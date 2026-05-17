"""Persistent storage helpers."""

from __future__ import annotations

import json
import os
import secrets
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from .models import BridgeState
from .utils import ensure_dir, now_iso


_LOCKS: dict[Path, threading.RLock] = {}
_LOCKS_GUARD = threading.RLock()


def _thread_lock_for(path: Path) -> threading.RLock:
    key = path.resolve(strict=False)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class StateStore:
    def __init__(self, root: Path):
        self.root = ensure_dir(root)
        self.path = self.root / "state.json"
        self.lock_path = self.root / "state.json.lock"
        self._thread_lock = _thread_lock_for(self.path)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._thread_lock:
            with self.lock_path.open("a+b") as lock_file:
                lock_file.seek(0)
                if not lock_file.read(1):
                    lock_file.write(b"\0")
                    lock_file.flush()
                lock_file.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                    try:
                        yield
                    finally:
                        lock_file.seek(0)
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def read(self) -> BridgeState:
        with self._locked():
            return self._read_unlocked()

    def _read_unlocked(self) -> BridgeState:
        if not self.path.exists():
            return BridgeState()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            backup = self.root / f"state.corrupt.{secrets.token_hex(4)}.json"
            try:
                self.path.replace(backup)
                _chmod_private(backup)
            except Exception:
                pass
            return BridgeState()
        return BridgeState.from_dict(payload)

    def write(self, state: BridgeState) -> None:
        with self._locked():
            current = self._read_unlocked()
            if current.session.revision > state.session.revision:
                state.session = current.session
            self._write_unlocked(state)

    def _write_unlocked(self, state: BridgeState) -> None:
        payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        tmp = self.path.with_name(f"{self.path.name}.tmp.{secrets.token_hex(4)}")
        tmp.write_text(payload, encoding="utf-8")
        _chmod_private(tmp)
        tmp.replace(self.path)
        _chmod_private(self.path)

    def update(self, updater: Callable[[BridgeState], None]) -> BridgeState:
        with self._locked():
            state = self._read_unlocked()
            original_revision = state.session.revision
            updater(state)
            current = self._read_unlocked()
            if current.session.revision > original_revision and state.session.revision <= current.session.revision:
                state.session = current.session
            self._write_unlocked(state)
            return state


def ensure_state_secret(state: BridgeState) -> BridgeState:
    if not state.runtime.secret:
        state.runtime.secret = secrets.token_urlsafe(32)
        state.runtime.started_at = state.runtime.started_at or now_iso()
    return state
