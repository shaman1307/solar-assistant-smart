"""Atomic JSON file read/write with backup recovery."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_save_locks: dict[str, threading.Lock] = {}
_save_locks_guard = threading.Lock()


def _save_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _save_locks_guard:
        lock = _save_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _save_locks[key] = lock
        return lock


def load_json(path: Path, *, default: dict[str, Any] | None = None) -> dict[str, Any]:
    """Load JSON; on corrupt/empty primary file try ``.bak`` backup."""
    fallback = dict(default or {})
    for candidate in (path, Path(str(path) + ".bak")):
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8").strip()
            if not text:
                log.warning("JSON file empty: %s", candidate)
                continue
            data = json.loads(text)
            if not isinstance(data, dict):
                log.warning("JSON root is not object: %s", candidate)
                continue
            if candidate != path:
                log.warning("Restored %s from backup (read-only)", path.name)
            return data
        except (json.JSONDecodeError, OSError, TypeError) as exc:
            log.warning("JSON read failed %s: %s", candidate, exc)
    return fallback


def atomic_json_save(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically (unique tmp + replace) and keep a ``.bak`` copy."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2)
    with _save_lock(path):
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        try:
            shutil.copy2(path, Path(str(path) + ".bak"))
        except OSError as exc:
            log.warning("JSON backup failed for %s: %s", path.name, exc)
