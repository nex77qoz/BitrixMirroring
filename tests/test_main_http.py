from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.helpers import make_settings

try:
    from fastapi.testclient import TestClient
    from main import _build_http_app
    IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - environment-specific import failure
    TestClient = None
    _build_http_app = None
    IMPORT_ERROR = exc


@unittest.skipIf(IMPORT_ERROR is not None, f"FastAPI runtime is unavailable: {IMPORT_ERROR}")
class MainHttpTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = make_settings()
        self.application = SimpleNamespace(
            bot_data={"telegram_webhook_status": {"ok": True}},
            bot=object(),
            process_update=AsyncMock(),
        )
        self.mirror = AsyncMock()
        self.client = TestClient(_build_http_app(self.settings, self.application, self.mirror))

    def test_health_exposes_runtime_flags(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["telegram_webhook_enabled"])

    def test_bitrix_event_bridge_requires_secret(self) -> None:
        response = self.client.post(
            self.settings.mirror_internal_event_path,
            json={"dialog_id": "chat42"},
            headers={"X-Internal-Webhook-Secret": "bad"},
        )
        self.assertEqual(response.status_code, 403)

    def test_bitrix_event_bridge_calls_schedule(self) -> None:
        self.mirror.schedule_bitrix_dialog_sync = AsyncMock(return_value=True)
        response = self.client.post(
            self.settings.mirror_internal_event_path,
            json={"dialog_id": "chat42", "event": "bitrix", "message_id": 7, "reply_id": 8},
            headers={"X-Internal-Webhook-Secret": self.settings.mirror_internal_webhook_secret},
        )
        self.assertEqual(response.status_code, 200)
        self.mirror.schedule_bitrix_dialog_sync.assert_awaited_once_with(
            "chat42",
            trigger="bitrix",
            message_id=7,
            reply_id=8,
        )

    def test_telegram_webhook_rejects_missing_secret(self) -> None:
        response = self.client.post(self.settings.telegram_webhook_path, json={"update_id": 1})
        self.assertEqual(response.status_code, 403)

    def test_telegram_webhook_processes_update(self) -> None:
        fake_update = object()
        with patch("main.Update.de_json", return_value=fake_update) as de_json:
            response = self.client.post(
                self.settings.telegram_webhook_path,
                json={"update_id": 1},
                headers={"X-Telegram-Bot-Api-Secret-Token": self.settings.telegram_webhook_secret},
            )
        self.assertEqual(response.status_code, 200)
        de_json.assert_called_once()
        self.application.process_update.assert_awaited_once_with(fake_update)
