"""Atomic JSON store helpers."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src.json_store import atomic_json_save, load_json


def test_atomic_save_survives_empty_primary(tmp_path: Path):
    path = tmp_path / "cache.json"
    atomic_json_save(path, {"days": {"2026-07-01": {"pv": 1}}})

    path.write_text("", encoding="utf-8")
    data = load_json(path, default={"days": {}})
    assert "2026-07-01" in data.get("days", {})
    # Primary still empty until next explicit save — read path does not rewrite disk.
    assert path.read_text(encoding="utf-8") == ""


def test_load_returns_default_when_missing(tmp_path: Path):
    path = tmp_path / "missing.json"
    assert load_json(path, default={"ok": True}) == {"ok": True}


def test_concurrent_atomic_save_leaves_valid_json(tmp_path: Path):
    path = tmp_path / "cache.json"

    def _write(n: int) -> None:
        atomic_json_save(path, {"n": n})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_write, range(40)))

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["n"] in range(40)
    assert list(tmp_path.glob("*.tmp")) == []
