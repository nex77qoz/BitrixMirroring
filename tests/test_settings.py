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
                "BITRIX_WEBHOOK_URL": " https://portal.example.com/rest/1/hook/ ",
                "BITRIX_BOT_TOKEN": " bot-token ",
                "BITRIX_BOT_ID": "12",
                "BITRIX_MAX_UPLOAD_FILE_BYTES": "2048",
                "MIRROR_STATE_DB_PATH": os.path.join(tmpdir, "state.sqlite3"),
                "ENABLE_SOCKS5_PROXY": "false",
                "TELEGRAM_WEBHOOK_ENABLED": "true",
                "TELEGRAM_WEBHOOK_PUBLIC_URL": "https://bot.example.com",
                "TELEGRAM_WEBHOOK_SECRET": "tg-secret",
            }
            with patch.dict(os.environ, env, clear=True):
                settings = Settings.from_env()
        self.assertEqual(settings.bitrix_webhook_url, "https://portal.example.com/rest/1/hook")
        self.assertEqual(settings.bitrix_bot_token, "bot-token")
        self.assertEqual(settings.bitrix_max_upload_file_bytes, 2048)
        self.assertTrue(settings.telegram_webhook_enabled)
        self.assertEqual(settings.telegram_webhook_path, "/telegram/webhook")

    def test_from_env_requires_bot_token(self) -> None:
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "token",
            "BITRIX_WEBHOOK_URL": "https://portal.example.com/rest/1/hook/",
            "BITRIX_BOT_TOKEN": "   ",
            "BITRIX_BOT_ID": "12",
        }, clear=True):
            with self.assertRaisesRegex(ValueError, "BITRIX_BOT_TOKEN is required"):
                Settings.from_env()

    def test_from_env_defaults_for_on_premise(self) -> None:
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "token",
            "BITRIX_WEBHOOK_URL": "https://portal.example.com/rest/1/hook",
            "BITRIX_BOT_TOKEN": "bot-token",
            "BITRIX_BOT_ID": "12",
        }, clear=True):
            settings = Settings.from_env()
        self.assertTrue(settings.bitrix_verify_ssl)
        self.assertEqual(settings.bitrix_min_request_interval_seconds, 0.0)
        self.assertEqual(settings.bitrix_poll_interval_seconds, 2.0)
        self.assertEqual(settings.bitrix_max_upload_file_bytes, 30 * 1024 * 1024)

    def test_from_env_rejects_non_http_webhook_url(self) -> None:
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "token",
            "BITRIX_WEBHOOK_URL": "portal.example.com/rest/1/hook",
            "BITRIX_BOT_TOKEN": "bot-token",
            "BITRIX_BOT_ID": "12",
        }, clear=True):
            with self.assertRaisesRegex(ValueError, "BITRIX_WEBHOOK_URL"):
                Settings.from_env()
