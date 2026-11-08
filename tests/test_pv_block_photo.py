"""PV block photo upload and serving."""

import asyncio

import pytest
from fastapi import HTTPException

from src.routes import data as data_routes


class _FakeRequest:
    def __init__(self, body: bytes, content_type: str):
        self._body = body
        self.headers = {"content-type": content_type}

    async def body(self) -> bytes:
        return self._body


def test_upload_stores_photo_and_serves_it(tmp_path, monkeypatch):
    monkeypatch.setattr(data_routes, "PV_PHOTO_DIR", tmp_path / "pv_photos")
    out = asyncio.run(data_routes.api_pv_block_photo_upload(_FakeRequest(b"\xff\xd8jpeg", "image/jpeg")))
    name = out["photo"]
    assert name.endswith(".jpg")
    assert (tmp_path / "pv_photos" / name).read_bytes() == b"\xff\xd8jpeg"
    resp = asyncio.run(data_routes.api_pv_block_photo(name))
    assert str(resp.path) == str(tmp_path / "pv_photos" / name)


def test_upload_rejects_non_image(tmp_path, monkeypatch):
    monkeypatch.setattr(data_routes, "PV_PHOTO_DIR", tmp_path / "pv_photos")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(data_routes.api_pv_block_photo_upload(_FakeRequest(b"x", "text/plain")))
    assert exc.value.status_code == 415


def test_serve_rejects_path_outside_photo_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(data_routes, "PV_PHOTO_DIR", tmp_path / "pv_photos")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(data_routes.api_pv_block_photo("../solar_smart.db"))
    assert exc.value.status_code == 404
