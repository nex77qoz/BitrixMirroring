import os
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path

# Setup temporary DB path before importing app.py so it uses this path
temp_dir = tempfile.TemporaryDirectory()
db_path = os.path.join(temp_dir.name, "test_mirror_state.sqlite3")
os.environ["MIRROR_STATE_DB_PATH"] = db_path
os.environ["BITRIX_LOG_PATH"] = os.path.join(temp_dir.name, "test_bitrix.log")

# Setup DB schema
conn = sqlite3.connect(db_path)
with conn:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bitrix_dialog_id TEXT NOT NULL,
            token TEXT NOT NULL UNIQUE,
            expires_at_unix INTEGER NOT NULL,
            created_at_unix INTEGER NOT NULL
        )
    """)
conn.close()

import sys
sys.path.append(str(Path(__file__).parent.parent / "server-side"))
from app import app
from fastapi.testclient import TestClient


class TestServerApp(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        # Clear database table pending_connections before each test
        conn = sqlite3.connect(db_path)
        with conn:
            conn.execute("DELETE FROM pending_connections")
        conn.close()

    def tearDown(self) -> None:
        pass

    @classmethod
    def tearDownClass(cls) -> None:
        temp_dir.cleanup()

    @patch("app.send_bot_message", new_callable=AsyncMock)
    async def test_tg_connect_success(self, mock_send_bot_message: AsyncMock) -> None:
        response = self.client.post(
            "/bitrix/bot",
            data={
                "event": "ONIMBOTMESSAGEADD",
                "data[PARAMS][DIALOG_ID]": "chat_test_123",
                "data[PARAMS][MESSAGE]": "/tg_connect",
                "data[PARAMS][BOT_ID]": "7",
            }
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"result": True})

        # Check DB
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT bitrix_dialog_id, token, expires_at_unix FROM pending_connections")
        rows = cursor.fetchall()
        conn.close()

        self.assertEqual(len(rows), 1)
        dialog_id, token, expires_at = rows[0]
        self.assertEqual(dialog_id, "chat_test_123")
        self.assertEqual(len(token), 8)  # secrets.token_hex(4) produces 8 hex chars

        # Check reply sent to Bitrix
        mock_send_bot_message.assert_called_once()
        call_kwargs = mock_send_bot_message.call_args[1]
        self.assertEqual(call_kwargs["dialog_id"], "chat_test_123")
        self.assertEqual(call_kwargs["bot_id"], "7")
        self.assertIn("🔑 Одноразовый токен сгенерирован.", call_kwargs["message"])
        self.assertIn(f"/connect chat_test_123 {token}", call_kwargs["message"])

    @patch("app.send_bot_message", new_callable=AsyncMock)
    async def test_tg_connect_case_insensitive(self, mock_send_bot_message: AsyncMock) -> None:
        response = self.client.post(
            "/bitrix/bot",
            data={
                "event": "ONIMBOTMESSAGEADD",
                "data[PARAMS][DIALOG_ID]": "chat_test_456",
                "data[PARAMS][MESSAGE]": "  /tg_connect   ",
                "data[PARAMS][BOT_ID]": "7",
            }
        )
        self.assertEqual(response.status_code, 200)

        # Check DB
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT token FROM pending_connections")
        rows = cursor.fetchall()
        conn.close()

        self.assertEqual(len(rows), 1)
        token = rows[0][0]

        mock_send_bot_message.assert_called_once()
        self.assertIn(token, mock_send_bot_message.call_args[1]["message"])

    @patch("app.send_bot_message", new_callable=AsyncMock)
    async def test_other_message_forwarded(self, mock_send_bot_message: AsyncMock) -> None:
        # If bridge is configured, ordinary messages are forwarded, not handled locally
        with patch("app._bridge_is_configured", return_value=True), \
             patch("app.forward_event_to_mirror", new_callable=AsyncMock) as mock_forward:
            response = self.client.post(
                "/bitrix/bot",
                data={
                    "event": "ONIMBOTMESSAGEADD",
                    "data[PARAMS][DIALOG_ID]": "chat_test_789",
                    "data[PARAMS][MESSAGE]": "Hello, bot!",
                    "data[PARAMS][BOT_ID]": "7",
                }
            )
            self.assertEqual(response.status_code, 200)
            mock_forward.assert_called_once()
            mock_send_bot_message.assert_not_called()

    @patch("app.send_bot_message", new_callable=AsyncMock)
    async def test_tg_connect_prefix_matching(self, mock_send_bot_message: AsyncMock) -> None:
        # A message starting with /tg_connect but not strictly matching should not trigger tg_connect
        with patch("app._bridge_is_configured", return_value=True), \
             patch("app.forward_event_to_mirror", new_callable=AsyncMock) as mock_forward:
            response = self.client.post(
                "/bitrix/bot",
                data={
                    "event": "ONIMBOTMESSAGEADD",
                    "data[PARAMS][DIALOG_ID]": "chat_test_999",
                    "data[PARAMS][MESSAGE]": "/tg_connector",
                    "data[PARAMS][BOT_ID]": "7",
                }
            )
            self.assertEqual(response.status_code, 200)
            mock_forward.assert_called_once()
            mock_send_bot_message.assert_not_called()

    @patch("app.send_bot_message", new_callable=AsyncMock)
    async def test_tg_connect_with_trailing(self, mock_send_bot_message: AsyncMock) -> None:
        # A message with trailing text like `/tg_connect argument` should be parsed as tg_connect
        response = self.client.post(
            "/bitrix/bot",
            data={
                "event": "ONIMBOTMESSAGEADD",
                "data[PARAMS][DIALOG_ID]": "chat_test_111",
                "data[PARAMS][MESSAGE]": "/tg_connect argument",
                "data[PARAMS][BOT_ID]": "7",
            }
        )
        self.assertEqual(response.status_code, 200)
        # Check DB
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT token FROM pending_connections")
        rows = cursor.fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)
        mock_send_bot_message.assert_called_once()


class DbConnectWalLoggingTest(unittest.TestCase):
    def test_db_connect_logs_warning_when_wal_pragma_fails(self) -> None:
        _server_side = os.path.join(os.path.dirname(__file__), "..", "server-side")
        if _server_side not in sys.path:
            sys.path.insert(0, _server_side)

        from unittest.mock import patch, MagicMock
        import monitor_app

        mock_conn = MagicMock(spec=sqlite3.Connection)
        mock_conn.execute.side_effect = sqlite3.OperationalError("disk I/O error")

        with patch("monitor_app.sqlite3.connect", return_value=mock_conn):
            with self.assertLogs("monitor_app", level="WARNING") as log_ctx:
                conn = monitor_app._db_connect()

        self.assertIn("WAL", " ".join(log_ctx.output))
        self.assertIs(conn, mock_conn)
