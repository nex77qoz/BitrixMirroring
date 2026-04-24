from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from settings import ChatMapping, Settings, _parse_topic_ids, _validate_chat_mappings


class SettingsTestCase(unittest.TestCase):
    def test_parse_topic_ids_keeps_order_and_uniqueness(self) -> None:
        self.assertEqual(_parse_topic_ids("1, 2, 1, bad, 3"), (1, 2, 3))

    def test_validate_chat_mappings_rejects_conflicting_topic(self) -> None:
        mappings = (
            ChatMapping(1, -100, "chat1", (7,), ""),
            ChatMapping(2, -100, "chat2", (7,), ""),
        )
        with self.assertRaises(ValueError):
            _validate_chat_mappings(mappings)

    def test_from_env_builds_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "TELEGRAM_BOT_TOKEN": "token",
                "BITRIX_WEBHOOK_BASE": "https://example.bitrix24.ru/rest/1/token/",
                "BITRIX_BOT_ID": "12",
                "BITRIX_BOT_CLIENT_ID": "client-token",
                "MIRROR_STATE_DB_PATH": os.path.join(tmpdir, "state.sqlite3"),
                "ENABLE_SOCKS5_PROXY": "false",
                "BITRIX_WEBHOOK_BRIDGE_ENABLED": "true",
                "MIRROR_INTERNAL_WEBHOOK_SECRET": "internal-secret",
                "TELEGRAM_WEBHOOK_ENABLED": "true",
                "TELEGRAM_WEBHOOK_PUBLIC_URL": "https://bot.example.com",
                "TELEGRAM_WEBHOOK_SECRET": "tg-secret",
            }
            with patch.dict(os.environ, env, clear=True):
                settings = Settings.from_env()
        self.assertEqual(settings.bitrix_webhook_base, "https://example.bitrix24.ru/rest/1/token")
        self.assertTrue(settings.bitrix_webhook_bridge_enabled)
        self.assertTrue(settings.telegram_webhook_enabled)
        self.assertEqual(settings.telegram_webhook_path, "/telegram/webhook")

    def test_from_env_requires_internal_secret_when_bridge_enabled(self) -> None:
        env = {
            "TELEGRAM_BOT_TOKEN": "token",
            "BITRIX_WEBHOOK_BASE": "https://example.bitrix24.ru/rest/1/token",
            "BITRIX_BOT_ID": "12",
            "BITRIX_BOT_CLIENT_ID": "client-token",
            "BITRIX_WEBHOOK_BRIDGE_ENABLED": "true",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_env()

