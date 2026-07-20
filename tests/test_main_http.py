from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests.helpers import make_settings

try:
    from fastapi.testclient import TestClient

    from main import _allowed_updates, _build_http_app
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
        self.mirror = SimpleNamespace(
            schedule_bitrix_dialog_sync=AsyncMock(),
        )
        self.client = TestClient(_build_http_app(self.settings, self.application, self.mirror))

    def test_health_exposes_runtime_flags(self) -> None:
        self.mirror.state_store = AsyncMock()
        self.mirror.state_store.load_bitrix_event_offset = AsyncMock(return_value=123)
        self.mirror._bitrix_event_task = SimpleNamespace(done=Mock(return_value=False))

        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["telegram_webhook_enabled"])
        self.assertEqual(payload["checks"]["db"], "ok")
        self.assertEqual(payload["checks"]["bitrix_event_fetcher_alive"], True)

    def test_telegram_webhook_rejects_missing_secret(self) -> None:
        response = self.client.post(self.settings.telegram_webhook_path, json={"update_id": 1})
        self.assertEqual(response.status_code, 403)

    def test_allowed_updates_include_callback_query(self) -> None:
        self.assertIn("callback_query", _allowed_updates())

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
