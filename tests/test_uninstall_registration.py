from __future__ import annotations

import json
import subprocess
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar

REGISTER_BOT = Path(__file__).parents[1] / "server-side" / "register_bot.py"


class _VibeStub(BaseHTTPRequestHandler):
    scenario: ClassVar[dict[str, object]] = {}
    requests: ClassVar[list[dict[str, object]]] = []

    def _send(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self, method: str, body: dict[str, object] | None = None) -> None:
        type(self).requests.append({
            "method": method,
            "path": urllib.parse.urlparse(self.path).path,
            "api_key": self.headers.get("X-Api-Key"),
            "body": body,
        })

    def do_GET(self) -> None:
        self._record("GET")
        scenario = self.scenario
        assert isinstance(scenario, dict)
        if self.path in scenario.get("get", {}):
            status, payload = scenario["get"][self.path]  # type: ignore[index]
            self._send(status, payload)
            return
        self._send(404, {"success": False, "error": {"code": "BOT_NOT_FOUND", "message": "no"}})

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        body = json.loads(raw or b"{}")
        self._record("POST", body)
        scenario = self.scenario
        assert isinstance(scenario, dict)
        status, payload = scenario.get("post", (500, {"success": False, "error": {"code": "?"}}))  # type: ignore[assignment]
        self._send(status, payload)  # type: ignore[arg-type]

    def log_message(self, *args: object) -> None:
        pass


class RegisterBotVibeTest(unittest.TestCase):
    def _run(self, scenario: dict[str, object], args: list[str], *, expect_ok: bool = True) -> tuple[dict[str, str], list[dict[str, object]]]:
        _VibeStub.scenario = scenario
        _VibeStub.requests = []
        server = HTTPServer(("127.0.0.1", 0), _VibeStub)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}/v1"
            result = subprocess.run(
                [sys.executable, str(REGISTER_BOT), "vibe_api_test", *args],
                capture_output=True, text=True, timeout=30,
                env={"VIBE_BASE_URL": base, "PATH": "/usr/bin:/bin"},
            )
            expected_rc = 0 if expect_ok else 1
            self.assertEqual(result.returncode, expected_rc, result.stdout + result.stderr)
            fields: dict[str, str] = {}
            for line in result.stdout.splitlines():
                if "=" in line:
                    key, _, value = line.partition("=")
                    fields[key] = value
            return fields, list(_VibeStub.requests)
        finally:
            server.shutdown()
            server.server_close()

    def test_registers_new_supervisor_bot_when_id_not_found(self) -> None:
        fields, requests = self._run(
            {
                "get": {"/v1/bots": (200, {"success": True, "data": {"items": []}})},
                "post": (201, {"success": True, "data": {"botId": 42}}),
            },
            ["", "Telegram Mirror"],
        )
        self.assertEqual(fields["status"], "ok")
        self.assertEqual(fields["action"], "registered")
        self.assertEqual(fields["bot_id"], "42")
        self.assertEqual(fields["bot_token"], "")
        post = next(r for r in requests if r["method"] == "POST")
        self.assertEqual(post["path"], "/v1/bots")
        self.assertEqual(post["api_key"], "vibe_api_test")
        self.assertEqual(
            post["body"],
            {"code": "tg_mirror_bot_v2", "name": "Telegram Mirror", "type": "supervisor", "eventMode": "fetch"},
        )

    def test_keeps_existing_bot_id_under_this_key(self) -> None:
        fields, requests = self._run(
            {"get": {"/v1/bots/42": (200, {"success": True, "data": {"botId": 42}})}},
            ["42"],
        )
        self.assertEqual(fields["status"], "ok")
        self.assertEqual(fields["action"], "kept")
        self.assertEqual(fields["bot_id"], "42")
        self.assertEqual([r for r in requests if r["method"] == "POST"], [])

    def test_discovers_bot_by_code_from_list(self) -> None:
        fields, requests = self._run(
            {"get": {"/v1/bots": (200, {"success": True, "data": {"items": [{"botId": 55, "code": "tg_mirror_bot_v2"}]}})}},
            ["", "Telegram Mirror"],
        )
        self.assertEqual(fields["status"], "ok")
        self.assertEqual(fields["action"], "existing")
        self.assertEqual(fields["bot_id"], "55")
        self.assertEqual([r for r in requests if r["method"] == "POST"], [])

    def test_409_with_bot_id_reports_foreign_owner(self) -> None:
        fields, _ = self._run(
            {
                "get": {"/v1/bots": (200, {"success": True, "data": {"items": []}})},
                "post": (409, {"success": False, "error": {"code": "BOT_ALREADY_EXISTS", "message": "exists"}, "data": {"botId": 99, "code": "tg_mirror_bot_v2"}}),
            },
            ["", "Telegram Mirror"],
            expect_ok=False,
        )
        self.assertEqual(fields["status"], "error")
        self.assertIn("transfer", fields["message"])

    def test_readonly_key_error_is_reported_with_hint(self) -> None:
        fields, _ = self._run(
            {
                "get": {"/v1/bots": (200, {"success": True, "data": {"items": []}})},
                "post": (403, {"success": False, "error": {"code": "WRITE_BLOCKED_READONLY_KEY", "message": "read-only"}}),
            },
            ["", "Telegram Mirror"],
            expect_ok=False,
        )
        self.assertEqual(fields["status"], "error")
        self.assertIn("READWRITE", fields["message"])

    def test_stdout_contract_keys_are_present(self) -> None:
        fields, _ = self._run(
            {"get": {"/v1/bots/42": (200, {"success": True, "data": {"botId": 42}})}},
            ["42"],
        )
        self.assertEqual(set(fields), {"status", "action", "bot_id", "bot_token", "message"})


class InstallRegistrationWiringTest(unittest.TestCase):
    """install.sh must call register_bot with the Vibe key and keep the Vibe DELETE contract."""

    def test_install_invokes_register_bot_with_vibe_api_key(self) -> None:
        source = (Path(__file__).parents[1] / "install.sh").read_text(encoding="utf-8")
        self.assertIn('python3 "$reg_script" "$VIBE_API_KEY"', source)
        self.assertNotIn("BITRIX_WEBHOOK_BASE", source)
        self.assertNotIn("BITRIX_BOT_CLIENT_ID", source)

    def test_manual_registration_hint_uses_vibe_payload(self) -> None:
        source = (Path(__file__).parents[1] / "install.sh").read_text(encoding="utf-8")
        summary = source[source.index("print_summary()") :]
        self.assertIn("tg_mirror_bot_v2", summary)
        self.assertIn("supervisor", summary)
        self.assertIn("eventMode", summary)
        self.assertNotIn("imbot.v2.Bot.register", summary)


if __name__ == "__main__":
    unittest.main()
