from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

INSTALL_SH = Path(__file__).parents[1] / "install.sh"


def extract_function(name: str) -> str:
    """Pull one top-level bash function out of install.sh (it ends at column 0 '}')."""
    source = INSTALL_SH.read_text(encoding="utf-8")
    start = source.index(f"{name}() {{")
    end = source.index("\n}\n", start) + len("\n}\n")
    return source[start:end]


def run_bash(script: str) -> subprocess.CompletedProcess[str]:
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
        handle.write(script)
        path = handle.name
    # Clean env: the installer functions must not see the ambient shell's
    # real VIBE_API_KEY / BITRIX_BOT_ID.
    return subprocess.run(["bash", path], capture_output=True, text=True, timeout=30,
                          env={"PATH": "/usr/bin:/bin"})


class BitrixBotCredentialsDeliveryTest(unittest.TestCase):
    def setUp(self) -> None:
        source = INSTALL_SH.read_text(encoding="utf-8")
        # Constants and helpers needed by the extracted functions live above run_cmd().
        self.prologue = source[: source.index("run_cmd() {")]

    def _notify_script(self, env: dict[str, str]) -> str:
        curl_stub = """
curl() { printf '%s\\n' "$*" >> "$CAPTURE_FILE"; echo '{"ok":true}'; }
"""
        body = f"""
set -uo pipefail
{self.prologue}
{curl_stub}
{extract_function("notify_telegram_admins_about_bitrix_bot")}
TG_ADMIN_IDS="111"
{chr(10).join(f'export {k}={v!r}' for k, v in env.items())}
notify_telegram_admins_about_bitrix_bot
"""
        return body

    def test_admin_notification_contains_bot_id_without_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            capture = Path(tmpdir) / "curl.log"
            result = run_bash(self._notify_script({
                "TG_ADMIN_IDS": "111",
                "BITRIX_BOT_ID": "777",
                "TELEGRAM_BOT_TOKEN": "tg-token",
                "VIBE_API_KEY": "vibe_api_secret",
                "CAPTURE_FILE": str(capture),
            }))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            sent = capture.read_text(encoding="utf-8") if capture.exists() else ""
        # the notification text must carry the bot id...
        self.assertIn("Bot ID:", sent.replace("\\n", "\n"))
        self.assertIn("777", sent)
        # ...but never the Vibe API key or any bot token
        self.assertNotIn("vibe_api_secret", sent)
        self.assertNotIn("Токен", sent)
        self.assertNotIn("BOT_TOKEN", extract_function("notify_telegram_admins_about_bitrix_bot").replace("TELEGRAM_BOT_TOKEN", ""))


class UnregisterBitrixBotTest(unittest.TestCase):
    def setUp(self) -> None:
        source = INSTALL_SH.read_text(encoding="utf-8")
        self.prologue = source[: source.index("run_cmd() {")]

    def _unregister_script(self, env_file: str, capture: str, response: str, exit_code: int = 0) -> str:
        curl_stub = f"""
curl() {{ printf '%s\\n' "$*" >> "$CAPTURE_FILE"; cat <<'CURLBODY'
{response}
CURLBODY
  return {exit_code}; }}
"""
        return f"""
set -uo pipefail
{self.prologue}
LOG_FILE=/dev/null
ENV_FILE="{env_file}"
CAPTURE_FILE="{capture}"
{curl_stub}
{extract_function("unregister_bitrix_bot")}
unregister_bitrix_bot
"""

    def _run(self, env: dict[str, str], response: str, exit_code: int = 0) -> tuple[str, str]:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")
            capture = Path(tmpdir) / "curl.log"
            script_file = Path(tmpdir) / "run.sh"
            script_file.write_text(self._unregister_script(str(env_file), str(capture), response, exit_code), encoding="utf-8")
            result = subprocess.run(["bash", str(script_file)], capture_output=True, text=True, timeout=30, env={"PATH": "/usr/bin:/bin"})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            sent = capture.read_text(encoding="utf-8") if capture.exists() else ""
            return sent, result.stdout

    def test_deletes_bot_via_vibe_with_api_key_header(self) -> None:
        sent, stdout = self._run(
            {"VIBE_API_KEY": "vibe_api_test", "VIBE_BASE_URL": "https://vibe.example.com/v1", "BITRIX_BOT_ID": "777"},
            '{"success": true, "data": {"deleted": true}}',
        )
        self.assertIn("DELETE", sent)
        self.assertIn("X-Api-Key:", sent)
        self.assertIn("vibe_api_test", sent)
        self.assertIn("https://vibe.example.com/v1/bots/777", sent)
        self.assertIn("Бот удалён", stdout)
        # no botToken in the request body — the platform stores tokens itself
        self.assertNotIn("botToken", sent)

    def test_reports_failure_on_unsuccessful_envelope(self) -> None:
        _, stdout = self._run(
            {"VIBE_API_KEY": "vibe_api_test", "BITRIX_BOT_ID": "777"},
            '{"success": false, "error": {"code": "BOT_NOT_FOUND", "message": "gone"}}',
        )
        self.assertIn("не удалил", stdout)

    def test_skips_without_key_or_id(self) -> None:
        for env in ({}, {"VIBE_API_KEY": "k"}, {"BITRIX_BOT_ID": "5"}):
            with self.subTest(env=env):
                sent, stdout = self._run(env, '{"success": true}')
                self.assertEqual(sent, "")
                self.assertIn("не заданы", stdout)


if __name__ == "__main__":
    unittest.main()
