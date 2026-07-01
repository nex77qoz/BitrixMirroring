import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Setup temporary DB path and log path before importing app.py
temp_dir = tempfile.TemporaryDirectory()
db_path = os.path.join(temp_dir.name, "test_mirror_state.sqlite3")
os.environ["MIRROR_STATE_DB_PATH"] = db_path
os.environ["BITRIX_LOG_PATH"] = os.path.join(temp_dir.name, "test_bitrix.log")

sys.path.append(str(Path(__file__).parent.parent / "server-side"))
from app import app
from fastapi.testclient import TestClient


class TestServerApp(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        temp_dir.cleanup()

    @patch("app.send_bot_message", new_callable=AsyncMock)
    async def test_health_endpoint(self, mock_send_bot_message: AsyncMock) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})

    @patch("app.send_bot_message", new_callable=AsyncMock)
    async def test_start_command(self, mock_send_bot_message: AsyncMock) -> None:
        response = self.client.post(
            "/bitrix/bot",
            data={
                "event": "ONIMBOTMESSAGEADD",
                "data[PARAMS][DIALOG_ID]": "chat_test_123",
                "data[PARAMS][MESSAGE]": "/start",
                "data[PARAMS][BOT_ID]": "7",
            }
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"result": True})

        mock_send_bot_message.assert_called_once()
        call_kwargs = mock_send_bot_message.call_args[1]
        self.assertEqual(call_kwargs["dialog_id"], "chat_test_123")
        self.assertEqual(call_kwargs["bot_id"], "7")
        self.assertIn("Привет. Я получил сообщение", call_kwargs["message"])

    @patch("app.send_bot_message", new_callable=AsyncMock)
    async def test_ping_command(self, mock_send_bot_message: AsyncMock) -> None:
        response = self.client.post(
            "/bitrix/bot",
            data={
                "event": "ONIMBOTMESSAGEADD",
                "data[PARAMS][DIALOG_ID]": "chat_test_123",
                "data[PARAMS][MESSAGE]": "ping",
                "data[PARAMS][BOT_ID]": "7",
            }
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"result": True})

        mock_send_bot_message.assert_called_once()
        call_kwargs = mock_send_bot_message.call_args[1]
        self.assertEqual(call_kwargs["message"], "pong")


class DbConnectWalLoggingTest(unittest.TestCase):
    def test_db_connect_logs_warning_when_wal_pragma_fails(self) -> None:
        _server_side = os.path.join(os.path.dirname(__file__), "..", "server-side")
        if _server_side not in sys.path:
            sys.path.insert(0, _server_side)

        import sqlite3
        from unittest.mock import MagicMock

        import monitor_app

        mock_conn = MagicMock(spec=sqlite3.Connection)
        mock_conn.execute.side_effect = sqlite3.OperationalError("disk I/O error")

        with patch("monitor_app.sqlite3.connect", return_value=mock_conn):
            with self.assertLogs("monitor_app", level="WARNING") as log_ctx:
                conn = monitor_app._db_connect()

        self.assertIn("WAL", " ".join(log_ctx.output))
        self.assertIs(conn, mock_conn)
