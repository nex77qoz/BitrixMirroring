from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar, cast

REGISTER_BOT = Path(__file__).parents[1] / "server-side" / "register_bot.py"


class _PortalStub(BaseHTTPRequestHandler):
    """Fake portal: POST /rest/1/hook/<method> answered from scenario[method]."""

    scenario: ClassVar[dict[str, Any]] = {}
    requests: ClassVar[list[dict[str, object]]] = []

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        body = json.loads(raw or b"{}")
        method = urllib.parse.urlparse(self.path).path.rsplit("/", 1)[-1]
        type(self).requests.append({"method": method, "path": self.path, "body": body})
        status, payload = self.scenario.get(method, (404, {"error": "ERROR_METHOD_NOT_FOUND", "error_description": "no"}))
        out = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args: object) -> None:
        pass


SCOPES = (200, {"result": ["imbot", "im", "disk"]})
BOT_OK = (200, {"result": {"bot": {"id": 42, "code": "tg_mirror_bot_v2"}}})


class RegisterBotRestTest(unittest.TestCase):
    def _run(self, scenario: dict[str, object], args: list[str], *, expect_ok: bool = True, env: dict[str, str] | None = None) -> tuple[dict[str, str], list[dict[str, object]]]:
        _PortalStub.scenario = scenario
        _PortalStub.requests = []
        server = HTTPServer(("127.0.0.1", 0), _PortalStub)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            hook = f"http://127.0.0.1:{server.server_address[1]}/rest/1/hook/"
            result = subprocess.run(
                [sys.executable, str(REGISTER_BOT), hook, *args],
                capture_output=True, text=True, timeout=30,
                env={"PATH": "/usr/bin:/bin", "BITRIX_BOT_CREDENTIALS_FILE": str(Path(self.recovery.name) / "bot.json"), **(env or {})},
            )
            expected_rc = 0 if expect_ok else 1
            self.assertEqual(result.returncode, expected_rc, result.stdout + result.stderr)
            fields: dict[str, str] = {}
            for line in result.stdout.splitlines():
                if "=" in line:
                    key, _, value = line.partition("=")
                    fields[key] = value
            return fields, list(_PortalStub.requests)
        finally:
            server.shutdown()
            server.server_close()

    def setUp(self) -> None:
        self.recovery = tempfile.TemporaryDirectory()
        self.addCleanup(self.recovery.cleanup)

    def test_registers_new_supervisor_bot_with_generated_token(self) -> None:
        fields, requests = self._run(
            {"scope": SCOPES, "imbot.v2.Bot.register": (200, {"result": {"bot": {"id": 42}}}), "imbot.v2.Bot.get": BOT_OK},
            [],
        )
        self.assertEqual(fields["status"], "ok")
        self.assertEqual(fields["action"], "registered")
        self.assertEqual(fields["bot_id"], "42")
        self.assertRegex(fields["bot_token"], r"^[0-9a-f]{32}$")
        register = next(r for r in requests if r["method"] == "imbot.v2.Bot.register")
        body = cast(dict[str, Any], register["body"])
        self.assertEqual(body["botToken"], fields["bot_token"])
        self.assertEqual(
            body["fields"],
            {
                "code": "tg_mirror_bot_v2", "botToken": body["botToken"], "properties": {"name": "Telegram Mirror V2"},
                "type": "supervisor", "eventMode": "fetch",
            },
        )

    def test_code_taken_tries_all_suffix_variants_then_fails(self) -> None:
        taken = (400, {"error": "BOT_CODE_ALREADY_TAKEN", "error_description": "taken"})
        fields, requests = self._run(
            {"scope": SCOPES, "imbot.v2.Bot.register": taken},
            [], expect_ok=False,
        )
        self.assertEqual(fields["status"], "error")
        self.assertEqual(sum(1 for r in requests if r["method"] == "imbot.v2.Bot.register"), 6)

    def test_keeps_existing_bot_with_token(self) -> None:
        fields, requests = self._run(
            {"scope": SCOPES, "imbot.v2.Bot.get": BOT_OK},
            ["42"], env={"BITRIX_BOT_TOKEN": "tok"},
        )
        self.assertEqual(fields["action"], "kept")
        self.assertEqual(fields["bot_id"], "42")
        self.assertEqual(fields["bot_token"], "tok")
        self.assertEqual([r for r in requests if r["method"] == "imbot.v2.Bot.register"], [])

    def test_reuses_existing_bot_from_list_with_token(self) -> None:
        fields, requests = self._run(
            {"scope": SCOPES, "imbot.v2.Bot.get": BOT_OK,
             "imbot.v2.Bot.list": (200, {"result": {"bots": [{"id": 61, "code": "tg_mirror_bot_v2_2"}, {"id": 55, "code": "tg_mirror_bot_v2"}]}})},
            ["", "Telegram Mirror"], env={"BITRIX_BOT_TOKEN": "tok"},
        )
        self.assertEqual(fields["action"], "existing")
        self.assertEqual(fields["bot_id"], "55")
        self.assertEqual([r for r in requests if r["method"] == "imbot.v2.Bot.register"], [])

    def test_insufficient_scope_error_is_reported_with_hint(self) -> None:
        fields, _ = self._run(
            {"scope": SCOPES, "imbot.v2.Bot.register": (403, {"error": "INSUFFICIENT_SCOPE", "error_description": "no imbot"})},
            ["", "Telegram Mirror"], expect_ok=False,
        )
        self.assertEqual(fields["status"], "error")
        self.assertIn("imbot, im, disk", fields["message"])

    def test_stdout_contract_keys_are_present(self) -> None:
        fields, _ = self._run(
            {"scope": SCOPES, "imbot.v2.Bot.get": BOT_OK}, ["42"], env={"BITRIX_BOT_TOKEN": "tok"},
        )
        self.assertEqual(set(fields), {"status", "action", "bot_id", "bot_token", "message"})

    def test_rejects_webhook_without_required_scope(self) -> None:
        fields, requests = self._run(
            {"scope": (200, {"result": ["imbot", "im"]}), "imbot.v2.Bot.register": (200, {"result": {"bot": {"id": 42}}})},
            [], expect_ok=False,
        )
        self.assertIn("disk", fields["message"])
        self.assertFalse(any(r["method"] == "imbot.v2.Bot.register" for r in requests))

    def test_preserves_credentials_when_readiness_check_fails(self) -> None:
        fields, requests = self._run(
            {"scope": SCOPES, "imbot.v2.Bot.register": (200, {"result": {"bot": {"id": 42}}}),
             "imbot.v2.Bot.get": (403, {"error": "ACCESS_DENIED"})},
            [], expect_ok=False,
        )
        self.assertEqual(fields["status"], "error")
        path = Path(self.recovery.name) / "bot.json"
        saved = json.loads(path.read_text())
        register = next(r for r in requests if r["method"] == "imbot.v2.Bot.register")
        self.assertEqual(saved["BITRIX_BOT_TOKEN"], cast(dict[str, Any], register["body"])["botToken"])
        self.assertEqual(saved["BITRIX_BOT_ID"], 42)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class InstallRegistrationWiringTest(unittest.TestCase):
    """install.sh must call register_bot with the webhook URL and unregister through imbot.v2."""

    def test_install_invokes_register_bot_with_webhook_url(self) -> None:
        source = (Path(__file__).parents[1] / "install.sh").read_text(encoding="utf-8")
        self.assertIn('python3 "$reg_script" "$BITRIX_WEBHOOK_URL"', source)
        self.assertNotIn("VIBE", source.upper())

    def test_uninstall_can_keep_bot_for_reuse_on_next_install(self) -> None:
        source = (Path(__file__).parents[1] / "install.sh").read_text(encoding="utf-8")
        uninstall = source[source.index("do_uninstall() {"):]
        self.assertIn("Удалить созданного бота в Bitrix24?", uninstall)
        self.assertIn("Бот Bitrix24 сохранён", uninstall)
        self.assertIn("используйте тот же BITRIX_BOT_TOKEN", uninstall)
        self.assertNotIn('source "$ENV_FILE"', source)

    def test_manual_registration_hint_points_at_register_script(self) -> None:
        source = (Path(__file__).parents[1] / "install.sh").read_text(encoding="utf-8")
        summary = source[source.index("print_summary()") :]
        self.assertIn("register_bot.py", summary)
        self.assertIn("BITRIX_BOT_TOKEN", summary)

    def test_telegram_webhook_check_reports_request_and_transport_details(self) -> None:
        source = (Path(__file__).parents[1] / "install.sh").read_text(encoding="utf-8")
        self.assertIn("GET https://api.telegram.org/bot<скрыт>/getWebhookInfo", source)
        self.assertIn("HTTP ${webhook_http_code}, curl exit ${webhook_curl_exit}", source)
        self.assertIn("webhook_api_error", source)


if __name__ == "__main__":
    unittest.main()
