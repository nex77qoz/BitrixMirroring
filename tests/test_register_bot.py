import json
import ssl
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import register_bot

REGISTER_BOT = Path(__file__).parents[1] / "server-side" / "register_bot.py"


def test_missing_api_key_exits_with_error_contract():
    result = subprocess.run(
        [sys.executable, str(REGISTER_BOT)],
        capture_output=True, text=True, timeout=30,
        env={"PATH": "/usr/bin:/bin"},
    )

    assert result.returncode == 1
    assert "status=error" in result.stdout
    assert "Usage: register_bot.py <webhook_url>" in result.stdout


def test_registration_uses_imbot_v2_methods_with_bot_token():
    source = REGISTER_BOT.read_text(encoding="utf-8")

    assert "imbot.v2.Bot.register" in source
    assert "tg_mirror_bot_v2" in source
    assert "botToken" in source
    assert "vibe" not in source.lower()


@pytest.mark.parametrize("verify", ["true", "false"])
def test_registration_applies_tls_setting(monkeypatch, verify):
    monkeypatch.setenv("BITRIX_VERIFY_SSL", verify)
    contexts = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return b'{"result": ["imbot", "im", "disk"]}'

    def urlopen(request, *, timeout, context):
        contexts.append(context)
        return Response()

    monkeypatch.setattr(register_bot.urllib.request, "urlopen", urlopen)
    register_bot._rest_request_once("https://portal/rest/1/hook", "scope", {})
    assert contexts[0].check_hostname is (verify == "true")
    assert contexts[0].verify_mode == (ssl.CERT_REQUIRED if verify == "true" else ssl.CERT_NONE)


def test_retry_recovers_token_after_post_registration_failure(monkeypatch, tmp_path, capsys):
    recovery = tmp_path / "recovery.json"
    monkeypatch.setenv("BITRIX_BOT_CREDENTIALS_FILE", str(recovery))
    monkeypatch.delenv("BITRIX_BOT_TOKEN", raising=False)
    monkeypatch.setattr(sys, "argv", [str(REGISTER_BOT), "https://portal/rest/1/hook"])
    tokens = []
    bot_ready = False

    def rest_call(url, method, body, **kwargs) -> tuple[int, dict[str, Any]]:
        if method == "scope":
            return 200, {"result": ["imbot", "im", "disk"]}
        if method == "imbot.v2.Bot.register":
            tokens.append(body["botToken"])
            return 200, {"result": {"bot": {"id": 42}}}
        if method == "imbot.v2.Bot.get":
            assert body["botToken"] == tokens[0]
            return (200, {"result": {"bot": {"id": 42}}}) if bot_ready else (403, {"error": "ACCESS_DENIED"})
        pytest.fail(f"Unexpected method: {method}")

    monkeypatch.setattr(register_bot, "rest_call", rest_call)
    with pytest.raises(SystemExit, match="1"):
        register_bot.main()
    assert json.loads(recovery.read_text())["BITRIX_BOT_TOKEN"] == tokens[0]
    capsys.readouterr()
    bot_ready = True
    with pytest.raises(SystemExit, match="0"):
        register_bot.main()
    assert "action=kept" in capsys.readouterr().out
    assert len(tokens) == 1
