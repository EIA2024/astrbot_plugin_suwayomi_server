"""Tests for utils/downloader.py (no network)."""
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from plugin_pkg.suwayomi.client import SuwayomiClient
from plugin_pkg.utils.downloader import (
    download_cover,
    download_images,
    download_one,
    resolve_image_url,
)


@pytest.mark.asyncio
async def test_download_images_creates_missing_custom_tmp(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "tmp"

    async def fake_download_one(session, url, dest, retries=3):
        dest.write_bytes(b"x")
        return True

    monkeypatch.setattr("plugin_pkg.utils.downloader.download_one", fake_download_one)

    paths, tmp_dir = await download_images(
        ["http://x/1", "http://x/2"],
        custom_tmp=str(target),
        headers={},
    )

    assert target.is_dir()
    assert tmp_dir.parent == target
    assert len(paths) == 2 and all(p for p in paths)


@pytest.mark.asyncio
async def test_download_images_returns_empty_paths_on_failure(tmp_path, monkeypatch):
    async def failing(session, url, dest, retries=3):
        raise OSError("boom")

    monkeypatch.setattr("plugin_pkg.utils.downloader.download_one", failing)

    paths, tmp_dir = await download_images(
        ["http://x/1", "http://x/2"],
        custom_tmp=str(tmp_path),
    )

    assert paths == ["", ""]


@pytest.mark.asyncio
async def test_download_one_retries_then_succeeds(tmp_path):
    responses = [500, 200]

    class Resp:
        def __init__(self, status):
            self.status = status
            self.headers = {"Content-Type": "image/jpeg"}
            self.content = self  # download_one 以 iter_chunked 流式读取

        async def read(self):
            return b"data"

        async def iter_chunked(self, n):
            yield b"data"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    def fake_get(url, timeout=None):
        return Resp(responses.pop(0))

    session = AsyncMock()
    session.get = fake_get

    ok = await download_one(session, "http://x/1", tmp_path / "img", retries=2)

    assert ok is True
    assert (tmp_path / "img.jpg").exists()


@pytest.mark.asyncio
async def test_download_cover_returns_none_when_no_thumbnail(monkeypatch):
    client = SuwayomiClient("http://localhost:4567", "none", "", "")

    async def unexpected(*args, **kwargs):
        raise AssertionError("download_images should not be called")

    monkeypatch.setattr("plugin_pkg.utils.downloader.download_images", unexpected)

    path, tmp_dir = await download_cover(client, None)

    assert path is None
    assert tmp_dir is None


@pytest.mark.asyncio
async def test_download_cover_success_with_relative_url(tmp_path, monkeypatch):
    client = SuwayomiClient("http://localhost:4567", "basic", "admin", "pass")
    cover_tmp = tmp_path / "cover"
    cover_tmp.mkdir()
    cover_file = cover_tmp / "0000.jpg"
    cover_file.write_bytes(b"cover")
    captured = {}

    async def fake_download_images(urls, **kwargs):
        captured["urls"] = urls
        captured["headers"] = kwargs.get("headers")
        return [str(cover_file)], cover_tmp

    monkeypatch.setattr("plugin_pkg.utils.downloader.download_images", fake_download_images)

    path, tmp_dir = await download_cover(
        client,
        "/api/v1/manga/1/thumbnail",
        headers={"Authorization": "Basic xyz"},
    )

    assert path == str(cover_file)
    assert tmp_dir == cover_tmp
    assert captured["urls"] == ["http://localhost:4567/api/v1/manga/1/thumbnail"]
    assert captured["headers"] == {"Authorization": "Basic xyz"}


@pytest.mark.asyncio
async def test_download_cover_failure_cleans_tmp_dir(tmp_path, monkeypatch):
    client = SuwayomiClient("http://localhost:4567", "none", "", "")
    cover_tmp = tmp_path / "cover"
    cover_tmp.mkdir()

    async def fake_download_images(urls, **kwargs):
        return [""], cover_tmp

    monkeypatch.setattr("plugin_pkg.utils.downloader.download_images", fake_download_images)

    path, tmp_dir = await download_cover(client, "/api/v1/manga/1/thumbnail")

    assert path is None
    assert tmp_dir is None
    assert not cover_tmp.exists()


@pytest.mark.asyncio
async def test_download_cover_external_url_does_not_forward_auth(tmp_path, monkeypatch):
    client = SuwayomiClient("http://localhost:4567", "basic", "admin", "pass")
    cover_tmp = tmp_path / "cover"
    cover_tmp.mkdir()
    cover_file = cover_tmp / "0000.jpg"
    cover_file.write_bytes(b"cover")
    captured = {}

    async def fake_download_images(urls, **kwargs):
        captured["headers"] = kwargs.get("headers")
        return [str(cover_file)], cover_tmp

    monkeypatch.setattr("plugin_pkg.utils.downloader.download_images", fake_download_images)

    await download_cover(
        client,
        "https://cdn.example.com/cover.jpg",
        headers={"Authorization": "Basic xyz"},
    )

    assert captured["headers"] is None


@pytest.mark.asyncio
async def test_download_cover_same_origin_absolute_keeps_auth(tmp_path, monkeypatch):
    client = SuwayomiClient("http://localhost:4567", "basic", "admin", "pass")
    cover_tmp = tmp_path / "cover"
    cover_tmp.mkdir()
    cover_file = cover_tmp / "0000.jpg"
    cover_file.write_bytes(b"cover")
    captured = {}

    async def fake_download_images(urls, **kwargs):
        captured["headers"] = kwargs.get("headers")
        return [str(cover_file)], cover_tmp

    monkeypatch.setattr("plugin_pkg.utils.downloader.download_images", fake_download_images)

    await download_cover(
        client,
        "http://localhost:4567/api/v1/manga/1/thumbnail",
        headers={"Authorization": "Basic xyz"},
    )

    assert captured["headers"] == {"Authorization": "Basic xyz"}


@pytest.mark.asyncio
async def test_download_cover_absolute_url_without_server_url_does_not_forward_headers(tmp_path, monkeypatch):
    client = SuwayomiClient("", "none", "", "")
    cover_tmp = tmp_path / "cover"
    cover_tmp.mkdir()
    cover_file = cover_tmp / "0000.jpg"
    cover_file.write_bytes(b"cover")
    captured = {}

    async def fake_download_images(urls, **kwargs):
        captured["headers"] = kwargs.get("headers")
        return [str(cover_file)], cover_tmp

    monkeypatch.setattr("plugin_pkg.utils.downloader.download_images", fake_download_images)

    path, tmp_dir = await download_cover(
        client,
        "https://example.com/cover.jpg",
        headers={"Authorization": "Basic xyz"},
    )

    assert path == str(cover_file)
    assert tmp_dir == cover_tmp
    assert captured["headers"] is None


@pytest.mark.asyncio
async def test_download_cover_swallows_download_exception(tmp_path, monkeypatch):
    client = SuwayomiClient("http://localhost:4567", "none", "", "")

    async def boom(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr("plugin_pkg.utils.downloader.download_images", boom)

    path, tmp_dir = await download_cover(client, "/api/v1/manga/1/thumbnail")

    assert path is None
    assert tmp_dir is None


class TestResolveImageUrlSsrfGuard:
    """第三方绝对封面地址指向私网/环回 → 拒绝下载（防 SSRF）。"""

    def _client(self, url="http://localhost:4567"):
        return SuwayomiClient(url, "none", "", "")

    def test_rejects_metadata_service_ip(self):
        url, headers = resolve_image_url(
            self._client(), "http://169.254.169.254/latest/meta-data", {"Authorization": "Bearer x"}
        )
        assert url is None and headers is None

    def test_rejects_private_lan_ip(self):
        url, headers = resolve_image_url(
            self._client(), "http://10.0.0.5:8080/cover.jpg", {"Authorization": "Bearer x"}
        )
        assert url is None and headers is None

    def test_rejects_localhost_hostname(self):
        url, headers = resolve_image_url(
            self._client(), "http://localhost:9999/cover.jpg", {"Authorization": "Bearer x"}
        )
        assert url is None and headers is None

    def test_allows_same_server_private_address_with_auth(self):
        client = self._client("http://192.168.1.5:4567")
        auth = {"Authorization": "Bearer x"}
        url, headers = resolve_image_url(
            client, "http://192.168.1.5:4567/api/v1/manga/1/thumbnail", auth
        )
        # Suwayomi 本机部署在内网是常态：同源私网地址必须放行并携带凭据
        assert url is not None and headers is auth

    def test_allows_public_absolute_url_without_auth(self):
        url, headers = resolve_image_url(
            self._client(), "https://cdn.example.com/cover.jpg", {"Authorization": "Bearer x"}
        )
        assert url == "https://cdn.example.com/cover.jpg"
        assert headers is None
