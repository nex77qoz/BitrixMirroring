from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from main import _allowed_updates, _build_http_app
from tests.helpers import make_settings


@pytest.fixture
def http_context():
    settings = make_settings()
    application = SimpleNamespace(
        bot_data={"telegram_webhook_status": {"ok": True}},
        bot=object(),
        process_update=AsyncMock(),
    )
    mirror = SimpleNamespace(
        is_forwarding_enabled=Mock(return_value=True),
        set_forwarding_enabled=AsyncMock(return_value=False),
        schedule_bitrix_dialog_sync=AsyncMock(),
    )
    return settings, application, mirror


async def request(app, method: str, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


@pytest.mark.asyncio
async def test_health_exposes_runtime_flags(http_context) -> None:
    settings, application, mirror = http_context
    mirror.state_store = AsyncMock()
    mirror.state_store.load_bitrix_event_offset = AsyncMock(return_value=123)
    mirror._bitrix_event_task = SimpleNamespace(done=Mock(return_value=False))

    response = await request(_build_http_app(settings, application, mirror), "GET", "/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["telegram_webhook_enabled"] is True
    assert payload["forwarding_enabled"] is True
    assert payload["checks"]["db"] == "ok"
    assert payload["checks"]["bitrix_event_fetcher_alive"] is True


@pytest.mark.asyncio
async def test_forwarding_status_requires_secret(http_context) -> None:
    settings, application, mirror = http_context
    response = await request(_build_http_app(settings, application, mirror), "GET", "/internal/forwarding")

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_forwarding_toggle_calls_mirror(http_context) -> None:
    settings, application, mirror = http_context
    response = await request(
        _build_http_app(settings, application, mirror),
        "POST",
        "/internal/forwarding",
        json={"enabled": False},
        headers={"X-Internal-Webhook-Secret": settings.mirror_internal_webhook_secret},
    )

    assert response.status_code == 200
    assert response.json()["forwarding_enabled"] is False
    mirror.set_forwarding_enabled.assert_awaited_once_with(False)


@pytest.mark.asyncio
async def test_telegram_webhook_rejects_missing_secret(http_context) -> None:
    settings, application, mirror = http_context
    response = await request(
        _build_http_app(settings, application, mirror),
        "POST",
        settings.telegram_webhook_path,
        json={"update_id": 1},
    )

    assert response.status_code == 403


def test_allowed_updates_include_callback_query() -> None:
    assert "callback_query" in _allowed_updates()


@pytest.mark.asyncio
async def test_telegram_webhook_processes_update(http_context) -> None:
    settings, application, mirror = http_context
    fake_update = object()
    with patch("main.Update.de_json", return_value=fake_update) as de_json:
        response = await request(
            _build_http_app(settings, application, mirror),
            "POST",
            settings.telegram_webhook_path,
            json={"update_id": 1},
            headers={"X-Telegram-Bot-Api-Secret-Token": settings.telegram_webhook_secret},
        )

    assert response.status_code == 200
    de_json.assert_called_once()
    application.process_update.assert_awaited_once_with(fake_update)
