"""Small atomic JSON persistence helper for plugin-local caches."""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, TypeVar


T = TypeVar("T")

_LOCKS: dict[Path, threading.RLock] = {}
_LOCKS_LOCK = threading.RLock()


def _default_payload() -> dict[str, Any]:
    return {"next_id": 1, "items": []}


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _lock_for(path: Path) -> threading.RLock:
    key = path.resolve(strict=False)
    with _LOCKS_LOCK:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


class AtomicItemStoreFile:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = _lock_for(self.path)

    def read(self) -> dict[str, Any]:
        with self._lock:
            return self._read_unlocked()

    def write(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._write_unlocked(payload)

    def transact(self, updater: Callable[[dict[str, Any]], T]) -> T:
        with self._lock:
            payload = self._read_unlocked()
            result = updater(payload)
            self._write_unlocked(payload)
            return result

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return _default_payload()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self._quarantine_corrupt_unlocked()
            return _default_payload()
        if not isinstance(payload, dict):
            self._quarantine_corrupt_unlocked()
            return _default_payload()
        payload.setdefault("next_id", 1)
        if not isinstance(payload.get("items"), list):
            payload["items"] = []
        return payload

    def _write_unlocked(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(f"{self.path.name}.tmp.{uuid.uuid4().hex}")
        data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        try:
            tmp_path.write_text(data, encoding="utf-8")
            _chmod_private(tmp_path)
            os.replace(tmp_path, self.path)
            _chmod_private(self.path)
        finally:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass

    def _quarantine_corrupt_unlocked(self) -> None:
        if not self.path.exists():
            return
        backup = self.path.with_name(f"{self.path.name}.corrupt.{uuid.uuid4().hex}")
        try:
            os.replace(self.path, backup)
            _chmod_private(backup)
        except OSError:
            pass
