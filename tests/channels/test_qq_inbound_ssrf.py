"""Regression tests for QQ inbound attachment download SSRF protection."""

from __future__ import annotations

import pytest

pytest.importorskip("botpy")


def _make_channel():
    from nanobot.bus.queue import MessageBus
    from nanobot.channels.qq.runtime import QQChannel, QQConfig

    bus = MessageBus()
    config = QQConfig(app_id="test_app", secret="test_secret", allow_from=["*"])
    return QQChannel(config, bus)


class _FakeDownloadResp:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.headers: dict[str, str] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeDownloadHttp:
    """Records get() calls; never performs real I/O."""

    def __init__(self, status: int = 200) -> None:
        self.get_calls: list[tuple[str, dict]] = []
        self._status = status

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return _FakeDownloadResp(self._status)


@pytest.mark.asyncio
async def test_inbound_download_blocks_ssrf_target() -> None:
    """An inbound attachment URL resolving to an internal target is never fetched.

    Mirrors the outbound _read_media_bytes guard and the napcat/dingtalk
    inbound download protection.
    """
    channel = _make_channel()
    fake_http = _FakeDownloadHttp()
    channel._http = fake_http

    result = await channel._download_to_media_dir_chunked(
        "http://169.254.169.254/latest/meta-data/", filename_hint="x.bin"
    )

    assert result is None
    assert fake_http.get_calls == []


@pytest.mark.asyncio
async def test_inbound_download_does_not_follow_redirects() -> None:
    """A redirect on an inbound attachment download must be rejected, not followed."""
    channel = _make_channel()
    fake_http = _FakeDownloadHttp(status=302)
    channel._http = fake_http

    result = await channel._download_to_media_dir_chunked(
        "https://example.com/attachment.bin", filename_hint="x.bin"
    )

    assert result is None
    assert len(fake_http.get_calls) == 1
    assert fake_http.get_calls[0][1]["allow_redirects"] is False
