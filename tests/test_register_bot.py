import subprocess
import sys
from pathlib import Path

REGISTER_BOT = Path(__file__).parents[1] / "server-side" / "register_bot.py"


def test_missing_api_key_exits_with_error_contract():
    result = subprocess.run(
        [sys.executable, str(REGISTER_BOT)],
        capture_output=True, text=True, timeout=30,
        env={"VIBE_BASE_URL": "http://127.0.0.1:1/v1", "PATH": "/usr/bin:/bin"},
    )

    assert result.returncode == 1
    assert "status=error" in result.stdout
    assert "Usage: register_bot.py <vibe_api_key>" in result.stdout


def test_registration_uses_vibe_endpoints_not_portal_rest():
    source = REGISTER_BOT.read_text(encoding="utf-8")

    assert "/bots" in source
    assert "tg_mirror_bot_v2" in source
    assert "imbot.v2" not in source
    assert "botToken" not in source
    assert ".bitrix-registration-token" not in source
