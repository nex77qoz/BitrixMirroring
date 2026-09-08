import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BASE_URL = "https://vibecode.bitrix24.tech/v1"
BOT_CODE = "tg_mirror_bot_v2"


def vibe_request(api_key: str, base_url: str, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    """Perform one Vibe API call. Returns (http_status, parsed_json_envelope).

    Raises RuntimeError on transport failure or a non-JSON response.
    """
    url = f"{base_url.rstrip('/')}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(  # noqa: S310 - base URL is HTTPS by configuration
        url,
        data=data,
        headers={
            "X-Api-Key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as res:  # noqa: S310 - HTTPS-only endpoint
            status = res.status
            payload = res.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            payload = e.read().decode("utf-8")
        except Exception as exc:
            raise RuntimeError(f"{method} {path} -> HTTP {status} (unreadable body)") from exc
    except Exception as exc:
        raise RuntimeError(f"{method} {path} -> {exc}") from exc
    try:
        envelope = json.loads(payload)
    except ValueError as exc:
        raise RuntimeError(f"{method} {path} -> non-JSON response (HTTP {status})") from exc
    if not isinstance(envelope, dict):
        raise RuntimeError(f"{method} {path} -> unexpected JSON payload (HTTP {status})")
    return status, envelope


def vibe_get(api_key: str, base_url: str, path: str) -> tuple[int, dict[str, Any]]:
    return vibe_request(api_key, base_url, "GET", path)


def vibe_post(api_key: str, base_url: str, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return vibe_request(api_key, base_url, "POST", path, body)


def envelope_error(envelope: dict[str, Any]) -> tuple[str, str]:
    error = envelope.get("error")
    if isinstance(error, dict):
        return str(error.get("code") or ""), str(error.get("message") or "")
    return "", ""


def envelope_data(envelope: dict[str, Any]) -> dict[str, Any]:
    data = envelope.get("data")
    return data if isinstance(data, dict) else {}


def list_bots(api_key: str, base_url: str) -> list[dict[str, Any]]:
    status, envelope = vibe_get(api_key, base_url, "/bots?limit=200")
    if status != 200 or not envelope.get("success"):
        code, message = envelope_error(envelope)
        raise RuntimeError(f"GET /bots -> HTTP {status} {code} {message}".strip())
    data = envelope_data(envelope)
    items = data.get("bots") if isinstance(data.get("bots"), list) else data.get("items")
    return [item for item in (items or []) if isinstance(item, dict)]


def _bot_id(bot: dict[str, Any]) -> int | None:
    # GET /v1/bots names the id field "id"; POST /v1/bots and the 409 conflict
    # envelope name it "botId". Accept either so a second install with the same
    # key recognises (and reuses) the existing mirror bot.
    for key in ("id", "botId"):
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


def find_existing_bot(api_key: str, base_url: str) -> tuple[str, int] | None:
    """Return (code, botId) of a mirror bot owned by this key, or None.

    A second install reusing the same API key hits this path and adopts the
    existing bot (base code first, then the lowest suffix variant) instead of
    registering a duplicate.
    """
    candidates: list[tuple[str, int]] = []
    for bot in list_bots(api_key, base_url):
        bot_id = _bot_id(bot)
        code = bot.get("code")
        if bot_id is not None and _is_mirror_code(code):
            candidates.append((str(code), bot_id))
    if not candidates:
        return None
    candidates.sort(key=lambda cb: (0 if cb[0] == BOT_CODE else 1, len(cb[0]), cb[0]))
    return candidates[0]


def validate_bot_ready(api_key: str, base_url: str, bot_id: int) -> tuple[bool, str]:
    """Verify the API key and bot are usable before writing the install config."""
    status, envelope = vibe_get(api_key, base_url, "/me")
    if status != 200 or not envelope.get("success"):
        code, message = envelope_error(envelope)
        return False, f"Не удалось проверить API-ключ: {code} | {message}".strip()
    key_data = envelope_data(envelope)
    scopes = key_data.get("scopes", key_data.get("scope"))
    if isinstance(scopes, str):
        scopes = [part.strip() for part in scopes.replace(",", " ").split()]
    if not isinstance(scopes, list):
        return False, "Не удалось подтвердить скоупы API-ключа"
    if "imbot" not in scopes:
        return False, "API-ключ не имеет скоупа imbot"
    access_mode = key_data.get("accessMode")
    if isinstance(access_mode, str) and access_mode.upper() != "READWRITE":
        return False, f"API-ключ имеет режим {access_mode}, требуется READWRITE"
    key_state = key_data.get("status")
    if isinstance(key_state, str) and key_state.lower() not in {"active", "enabled"}:
        return False, f"API-ключ неактивен (status={key_state})"

    status, envelope = vibe_get(api_key, base_url, f"/bots/{bot_id}")
    if status != 200 or not envelope.get("success"):
        code, message = envelope_error(envelope)
        return False, f"Бот недоступен: {code} | {message}".strip()
    data = envelope_data(envelope)
    bot = data.get("bot") if isinstance(data.get("bot"), dict) else data
    active_values: list[bool] = []
    if isinstance(bot, dict) and isinstance(bot.get("active"), bool):
        active_values.append(bot["active"])
    users = data.get("users")
    if isinstance(users, list):
        for user in users:
            if isinstance(user, dict) and str(user.get("id")) == str(bot_id) and isinstance(user.get("active"), bool):
                active_values.append(user["active"])
    if not active_values:
        return False, "Не удалось подтвердить активность бота в Bitrix24"
    if any(value is False for value in active_values):
        return False, "Бот неактивен в Bitrix24"
    return True, "API-ключ активен, скоуп imbot присутствует, бот активен"


def print_result(status, action, bot_id, bot_token, message):
    print(f"status={status}")
    print(f"action={action}")
    print(f"bot_id={bot_id}")
    print(f"bot_token={bot_token}")
    print(f"message={message}")


def print_error(message):
    print("status=error")
    print(f"message={message}")


def try_register(api_key: str, base_url: str, bot_name: str) -> tuple[str, int] | dict[str, Any]:
    """Try POST /bots with candidate codes. Returns (code, bot_id) or an error dict."""
    candidate_codes = [BOT_CODE] + [f"{BOT_CODE}_{n}" for n in range(2, 7)]
    last_error: dict[str, Any] = {"code": "", "message": "no attempts made"}
    for code in candidate_codes:
        body = {"code": code, "name": bot_name, "type": "supervisor", "eventMode": "fetch"}
        status, envelope = vibe_post(api_key, base_url, "/bots", body)
        if status in (200, 201) and envelope.get("success"):
            data = envelope_data(envelope)
            bot_id = data.get("botId")
            if not isinstance(bot_id, int):
                bot = data.get("bot")
                if isinstance(bot, dict) and isinstance(bot.get("id"), int):
                    bot_id = bot["id"]
            if isinstance(bot_id, int):
                return code, bot_id
            return {"code": "REGISTRATION_FAILED", "message": f"Vibe не вернул botId: {envelope}"}
        code_err, message = envelope_error(envelope)
        last_error = {"code": code_err, "message": message, "_status": status}
        if status == 409 and code_err == "BOT_ALREADY_EXISTS":
            data = envelope_data(envelope)
            conflicting_bot_id = data.get("botId")
            if isinstance(conflicting_bot_id, int):
                # The code is owned by another API key's bot record — do not
                # silently spawn suffix bots; tell the operator to transfer.
                return {
                    "code": "BOT_ALREADY_EXISTS",
                    "message": (
                        f"код {code} занят ботом (botId={conflicting_bot_id}) другого API-ключа — "
                        "перенесите владение (POST /v1/bots/:id/transfer) или используйте другой ключ"
                    ),
                }
            # 409 without data: external bot holds the code on the portal — try next candidate.
            continue
        return last_error
    return last_error


def main():
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print_error("Usage: register_bot.py <vibe_api_key> [<bot_id> [<bot_name>]]")
        sys.exit(1)

    api_key = sys.argv[1].strip()
    base_url = (os.environ.get("VIBE_BASE_URL", "") or DEFAULT_BASE_URL).strip().rstrip("/") or DEFAULT_BASE_URL
    existing_bot_id = sys.argv[2].strip() if len(sys.argv) > 2 and sys.argv[2].strip() else None
    bot_name = sys.argv[3].strip() if len(sys.argv) > 3 and sys.argv[3].strip() else "Telegram Mirror V2"

    try:
        # Step 1: a bot id was supplied — check it is registered under this key.
        if existing_bot_id:
            if not existing_bot_id.isdigit():
                print_error(f"Некорректный bot_id: {existing_bot_id}")
                sys.exit(1)
            status, envelope = vibe_get(api_key, base_url, f"/bots/{existing_bot_id}")
            if status == 200 and envelope.get("success"):
                ready, message = validate_bot_ready(api_key, base_url, int(existing_bot_id))
                if not ready:
                    print_error(message)
                    sys.exit(1)
                print_result("ok", "kept", int(existing_bot_id), "", f"Бот уже зарегистрирован через Vibe API; {message}")
                sys.exit(0)
            # 404/403 or any other answer — fall through to discovery/registration.

        # Step 2: reuse a mirror bot already owned by this key (base code or
        # a _2.._6 suffix) — this is what lets a second server adopt the bot.
        existing = find_existing_bot(api_key, base_url)
        if existing is not None:
            code, bot_id = existing
            ready, message = validate_bot_ready(api_key, base_url, bot_id)
            if not ready:
                print_error(message)
                sys.exit(1)
            print_result("ok", "existing", bot_id, "", f"Найден существующий бот Vibe (код: {code}); {message}")
            sys.exit(0)

        # Step 3: register a new supervisor bot.
        outcome = try_register(api_key, base_url, bot_name)
        if isinstance(outcome, dict):
            err_code = outcome.get("code", "")
            err_message = outcome.get("message", "")
            hint = ""
            if err_code in ("WRITE_BLOCKED_READONLY_KEY", "SCOPE_DENIED", "TOKEN_MISSING") or outcome.get("_status") == 401:
                hint = " (нужен READWRITE-ключ со скоупами imbot, disk)"
            print_error(f"Ошибка регистрации бота через Vibe API: {err_code} | {err_message}{hint}")
            sys.exit(1)
        registered_code, new_bot_id = outcome
        ready, message = validate_bot_ready(api_key, base_url, new_bot_id)
        if not ready:
            print_error(message)
            sys.exit(1)
        print_result("ok", "registered", new_bot_id, "", f"Бот успешно зарегистрирован через Vibe API (код: {registered_code}); {message}")
    except RuntimeError as exc:
        print_error(f"Ошибка обращения к Vibe API: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
