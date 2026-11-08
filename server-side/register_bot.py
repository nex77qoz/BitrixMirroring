from __future__ import annotations

import hashlib
import json
import os
import secrets
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BOT_CODE = "tg_mirror_bot_v2"
_RETRY_ATTEMPTS = 4
_RETRY_BASE_DELAY_SECONDS = 1.0
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


class _TransientError(RuntimeError):
    """Network/timeout failure that is safe to retry (no response was received)."""


def _rest_request_once(webhook_url: str, method: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    url = f"{webhook_url.rstrip('/')}/{method}"
    req = urllib.request.Request(  # noqa: S310 - webhook URL is operator-provided configuration
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        context = ssl.create_default_context()
        if os.environ.get("BITRIX_VERIFY_SSL", "true").strip().lower() in ("false", "0", "no", "off"):
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=15, context=context) as res:  # noqa: S310
            status = res.status
            payload = res.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            payload = e.read().decode("utf-8")
        except Exception as exc:
            raise RuntimeError(f"{method} -> HTTP {status} (unreadable body)") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _TransientError(f"{method} -> {exc}") from exc
    try:
        envelope = json.loads(payload)
    except ValueError as exc:
        raise RuntimeError(f"{method} -> non-JSON response (HTTP {status}); проверьте URL вебхука") from exc
    if not isinstance(envelope, dict):
        raise RuntimeError(f"{method} -> unexpected JSON payload (HTTP {status})")
    return status, envelope


def rest_call(
    webhook_url: str, method: str, body: dict[str, Any], *, retry: bool = True
) -> tuple[int, dict[str, Any]]:
    """Call one REST method of the on-premise portal. Returns (http_status, parsed_json).

    Transient 5xx/429 responses and network failures are retried with backoff.
    retry=False performs exactly one attempt — required for non-idempotent
    calls (imbot.v2.Bot.register) where a blind resend after a lost response
    could create a duplicate bot.
    """
    if not retry:
        return _rest_request_once(webhook_url, method, body)
    delay = _RETRY_BASE_DELAY_SECONDS
    last: tuple[int, dict[str, Any]] | None = None
    last_exc: _TransientError | None = None
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            status, envelope = _rest_request_once(webhook_url, method, body)
        except _TransientError as exc:
            last_exc = exc
            if attempt < _RETRY_ATTEMPTS:
                time.sleep(delay)
                delay *= 2
                continue
            raise
        last_exc = None
        if status not in _RETRYABLE_STATUSES:
            return status, envelope
        last = (status, envelope)
        if attempt < _RETRY_ATTEMPTS:
            time.sleep(delay)
            delay *= 2
    if last_exc is not None:
        raise last_exc
    assert last is not None
    return last


def envelope_error(envelope: dict[str, Any]) -> tuple[str, str]:
    return str(envelope.get("error") or ""), str(envelope.get("error_description") or "")


def envelope_result(envelope: dict[str, Any]) -> dict[str, Any]:
    result = envelope.get("result")
    return result if isinstance(result, dict) else {}


def _is_ok(status: int, envelope: dict[str, Any]) -> bool:
    return status == 200 and not envelope.get("error") and "result" in envelope


def list_bots(webhook_url: str, bot_token: str) -> list[dict[str, Any]]:
    status, envelope = rest_call(webhook_url, "imbot.v2.Bot.list", {"botToken": bot_token, "limit": 50})
    if not _is_ok(status, envelope):
        code, message = envelope_error(envelope)
        raise RuntimeError(f"imbot.v2.Bot.list -> HTTP {status} {code} {message}".strip())
    bots = envelope_result(envelope).get("bots")
    return [item for item in (bots or []) if isinstance(item, dict)]


def _bot_id(bot: dict[str, Any]) -> int | None:
    for key in ("id", "botId", "ID", "BOT_ID"):
        value = bot.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _is_mirror_code(code: object) -> bool:
    # the base code, or a suffix variant assigned when the base was taken
    if not isinstance(code, str):
        return False
    if code == BOT_CODE:
        return True
    prefix = f"{BOT_CODE}_"
    return code.startswith(prefix) and code[len(prefix):].isdigit()


def find_existing_bot(webhook_url: str, bot_token: str) -> tuple[str, int] | None:
    """Return (code, botId) of a mirror bot owned by this bot token, or None."""
    candidates: list[tuple[str, int]] = []
    for bot in list_bots(webhook_url, bot_token):
        bot_id = _bot_id(bot)
        code = bot.get("code")
        if bot_id is not None and _is_mirror_code(code):
            candidates.append((str(code), bot_id))
    if not candidates:
        return None
    candidates.sort(key=lambda cb: (0 if cb[0] == BOT_CODE else 1, len(cb[0]), cb[0]))
    return candidates[0]


def validate_webhook(webhook_url: str) -> tuple[bool, str]:
    status, envelope = rest_call(webhook_url, "scope", {})
    if not _is_ok(status, envelope):
        code, message = envelope_error(envelope)
        return False, f"Не удалось проверить вебхук: {code} | {message}".strip()
    scopes = envelope.get("result")
    if not isinstance(scopes, list):
        return False, "Не удалось подтвердить скоупы вебхука"
    for required in ("imbot", "im", "disk"):
        if required not in scopes:
            return False, f"Вебхук не имеет права (скоупа) {required}"
    return True, "Скоупы imbot/im/disk присутствуют"


def validate_bot_ready(webhook_url: str, bot_token: str, bot_id: int) -> tuple[bool, str]:
    """Verify the webhook, its scopes and the bot are usable before writing the install config."""
    ready, message = validate_webhook(webhook_url)
    if not ready:
        return False, message

    status, envelope = rest_call(webhook_url, "imbot.v2.Bot.get", {"botId": bot_id, "botToken": bot_token})
    if not _is_ok(status, envelope):
        code, message = envelope_error(envelope)
        return False, f"Бот недоступен: {code} | {message}".strip()
    return True, "Вебхук активен, скоупы imbot/im/disk присутствуют, бот доступен"


def print_result(status, action, bot_id, bot_token, message):
    print(f"status={status}")
    print(f"action={action}")
    print(f"bot_id={bot_id}")
    print(f"bot_token={bot_token}")
    print(f"message={message}")


def print_error(message):
    print("status=error")
    print(f"message={message}")


def credentials_path(webhook_url: str) -> Path:
    configured = os.environ.get("BITRIX_BOT_CREDENTIALS_FILE")
    if configured:
        return Path(configured)
    portal_key = hashlib.sha256(webhook_url.encode()).hexdigest()[:16]
    return Path.home() / ".local/state/bitrix-mirror" / f"{portal_key}.json"


def save_credentials(path: Path, webhook_url: str, bot_token: str, bot_id: str | int | None) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump({"BITRIX_WEBHOOK_URL": webhook_url, "BITRIX_BOT_TOKEN": bot_token,
                       "BITRIX_BOT_ID": bot_id, "BITRIX_VERIFY_SSL": os.environ.get("BITRIX_VERIFY_SSL", "true")}, handle)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def try_register(webhook_url: str, bot_token: str, bot_name: str) -> tuple[str, int] | dict[str, Any]:
    """Try imbot.v2.Bot.register with candidate codes. Returns (code, bot_id) or an error dict."""
    candidate_codes = [BOT_CODE] + [f"{BOT_CODE}_{n}" for n in range(2, 7)]
    last_error: dict[str, Any] = {"code": "", "message": "no attempts made"}
    for code in candidate_codes:
        body = {
            "botToken": bot_token,
            "fields": {
                "code": code, "botToken": bot_token, "properties": {"name": bot_name},
                "type": "supervisor", "eventMode": "fetch",
            },
        }
        # retry=False: a blind resend after a lost response could create a
        # second bot under the same code. On an ambiguous outcome, check
        # whether the bot was actually created before treating it as a failure.
        try:
            status, envelope = rest_call(webhook_url, "imbot.v2.Bot.register", body, retry=False)
        except _TransientError as exc:
            existing = find_existing_bot(webhook_url, bot_token)
            if existing is not None and existing[0] == code:
                return existing
            last_error = {"code": "", "message": str(exc)}
            continue
        if _is_ok(status, envelope):
            result = envelope_result(envelope)
            nested = result.get("bot")
            bot_id = _bot_id(nested if isinstance(nested, dict) else result)
            if bot_id is None:
                existing = find_existing_bot(webhook_url, bot_token)
                if existing is not None and existing[0] == code:
                    return existing
                return {"code": "REGISTRATION_FAILED", "message": f"Bitrix не вернул botId: {envelope}"}
            return code, bot_id
        code_err, message = envelope_error(envelope)
        last_error = {"code": code_err, "message": message}
        if code_err == "BOT_CODE_ALREADY_TAKEN":
            continue
        if status in _RETRYABLE_STATUSES:
            existing = find_existing_bot(webhook_url, bot_token)
            if existing is not None and existing[0] == code:
                return existing
            continue
        return last_error
    return last_error


def main():
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print_error("Usage: register_bot.py <webhook_url> [<bot_id> [<bot_name>]]")
        sys.exit(1)

    webhook_url = sys.argv[1].strip().rstrip("/")
    existing_bot_id = sys.argv[2].strip() if len(sys.argv) > 2 and sys.argv[2].strip() else None
    bot_name = sys.argv[3].strip() if len(sys.argv) > 3 and sys.argv[3].strip() else "Telegram Mirror V2"
    # A bot is owned by the token it was registered with: reuse it when given, mint a new one otherwise.
    bot_token = os.environ.get("BITRIX_BOT_TOKEN", "").strip()
    try:
        ready, message = validate_webhook(webhook_url)
        if not ready:
            print_error(message)
            sys.exit(1)
        recovery_path = credentials_path(webhook_url)
        if not bot_token and recovery_path.exists():
            saved = json.loads(recovery_path.read_text())
            if saved.get("BITRIX_WEBHOOK_URL") == webhook_url:
                bot_token = saved.get("BITRIX_BOT_TOKEN", "")
                existing_bot_id = existing_bot_id or str(saved.get("BITRIX_BOT_ID") or "")
        token_is_new = not bot_token
        if token_is_new:
            bot_token = secrets.token_hex(16)
        # Persist before the first remote mutation, including ambiguous network failures.
        save_credentials(recovery_path, webhook_url, bot_token, existing_bot_id)
        print(f"Реквизиты восстановления сохранены: {recovery_path}", file=sys.stderr)
        # Step 1: a bot id was supplied — check it is registered under this token.
        if existing_bot_id and not token_is_new:
            if not existing_bot_id.isdigit():
                print_error(f"Некорректный bot_id: {existing_bot_id}")
                sys.exit(1)
            ready, message = validate_bot_ready(webhook_url, bot_token, int(existing_bot_id))
            if ready:
                print_result("ok", "kept", int(existing_bot_id), bot_token, f"Бот уже зарегистрирован; {message}")
                sys.exit(0)
            # fall through to discovery/registration.

        # Step 2: reuse a mirror bot already owned by this token.
        if not token_is_new:
            existing = find_existing_bot(webhook_url, bot_token)
            if existing is not None:
                code, bot_id = existing
                ready, message = validate_bot_ready(webhook_url, bot_token, bot_id)
                if not ready:
                    print_error(message)
                    sys.exit(1)
                print_result("ok", "existing", bot_id, bot_token, f"Найден существующий бот (код: {code}); {message}")
                sys.exit(0)

        # Step 3: register a new supervisor bot.
        outcome = try_register(webhook_url, bot_token, bot_name)
        if isinstance(outcome, dict):
            err_code = outcome.get("code", "")
            err_message = outcome.get("message", "")
            hint = ""
            if err_code in ("INSUFFICIENT_SCOPE", "ACCESS_DENIED", "WRONG_AUTH_TYPE", "INVALID_CREDENTIALS"):
                hint = " (нужен входящий вебхук администратора со скоупами imbot, im, disk)"
            print_error(f"Ошибка регистрации бота: {err_code} | {err_message}{hint}")
            sys.exit(1)
        registered_code, new_bot_id = outcome
        save_credentials(recovery_path, webhook_url, bot_token, new_bot_id)
        ready, message = validate_bot_ready(webhook_url, bot_token, new_bot_id)
        if not ready:
            print_error(message)
            sys.exit(1)
        print_result("ok", "registered", new_bot_id, bot_token, f"Бот успешно зарегистрирован (код: {registered_code}); {message}")
    except (RuntimeError, OSError, ValueError) as exc:
        print_error(f"Ошибка обращения к REST API Bitrix24: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
