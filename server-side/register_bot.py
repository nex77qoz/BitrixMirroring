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
    status, envelope = vibe_get(api_key, base_url, "/bots")
    if status != 200 or not envelope.get("success"):
        code, message = envelope_error(envelope)
        raise RuntimeError(f"GET /bots -> HTTP {status} {code} {message}".strip())
    data = envelope_data(envelope)
    items = data.get("items") if isinstance(data.get("items"), list) else data.get("bots")
    return [item for item in (items or []) if isinstance(item, dict)]


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
                print_result("ok", "kept", int(existing_bot_id), "", "Бот уже зарегистрирован через Vibe API")
                sys.exit(0)
            # 404/403 or any other answer — fall through to discovery/registration.

        # Step 2: discover a bot with our code owned by this key.
        for bot in list_bots(api_key, base_url):
            if bot.get("code") == BOT_CODE and isinstance(bot.get("botId"), int):
                print_result("ok", "existing", bot["botId"], "", f"Найден существующий бот Vibe (код: {BOT_CODE})")
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
        print_result("ok", "registered", new_bot_id, "", f"Бот успешно зарегистрирован через Vibe API (код: {registered_code})")
    except RuntimeError as exc:
        print_error(f"Ошибка обращения к Vibe API: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
