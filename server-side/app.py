from fastapi import FastAPI, Request, HTTPException
from pathlib import Path
from urllib.parse import parse_qs
import fcntl
import httpx
import json
import logging
import os
import re

app = FastAPI()
LOG_FILE = Path(os.getenv("BITRIX_LOG_PATH", "/opt/bitrix-bot/bitrix.log"))
LOG_MAX_SIZE = 50 * 1024 * 1024  # 50 MB per file

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("bitrix-bot")

BITRIX_WEBHOOK_BASE = os.getenv("BITRIX_WEBHOOK_BASE", "").rstrip("/")
BITRIX_CLIENT_ID = (
    os.getenv("BITRIX_CLIENT_ID", "").strip()
    or os.getenv("BITRIX_BOT_CLIENT_ID", "").strip()
)
BITRIX_BOT_ID = os.getenv("BITRIX_BOT_ID", "").strip()  # опционально, как fallback
BITRIX_WEBHOOK_BRIDGE_ENABLED = os.getenv("BITRIX_WEBHOOK_BRIDGE_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
MIRROR_INTERNAL_BASE_URL = os.getenv("MIRROR_INTERNAL_BASE_URL", "").strip().rstrip("/")
MIRROR_INTERNAL_EVENT_PATH = os.getenv("MIRROR_INTERNAL_EVENT_PATH", "/internal/bitrix/event").strip() or "/internal/bitrix/event"
MIRROR_INTERNAL_WEBHOOK_SECRET = os.getenv("MIRROR_INTERNAL_WEBHOOK_SECRET", "").strip()
MIRROR_INTERNAL_TIMEOUT_SECONDS = float(os.getenv("MIRROR_INTERNAL_TIMEOUT_SECONDS", "10"))
FORWARDED_EVENTS = {
    item.strip()
    for item in os.getenv("BITRIX_FORWARDED_EVENTS", "ONIMBOTMESSAGEADD,ONIMBOTJOINCHAT").split(",")
    if item.strip()
}

# Webhook authentication: Bitrix sends application_token with every event.
# If BITRIX_WEBHOOK_TOKEN is empty, the first incoming token will be auto-captured.
WEBHOOK_TOKEN = os.getenv("BITRIX_WEBHOOK_TOKEN", "").strip()
_ENV_FILE = Path(os.getenv("ENV_FILE_PATH", "")).resolve() if os.getenv("ENV_FILE_PATH") else (Path(__file__).resolve().parent.parent / ".env")


def _extract_incoming_token(payload: dict) -> str:
    """Extract application_token from Bitrix webhook payload."""
    token = ""
    auth = payload.get("auth")
    if isinstance(auth, dict):
        token = auth.get("application_token", "")
    if not token:
        token = payload.get("application_token", "")
    return token.strip()


def _auto_capture_token(token: str) -> None:
    """Save the first received application_token into .env and activate auth."""
    global WEBHOOK_TOKEN
    if WEBHOOK_TOKEN or not token:
        return
    WEBHOOK_TOKEN = token
    logger.info("Auto-captured application_token from first webhook event")
    try:
        env_path = _ENV_FILE
        if env_path.is_file():
            with open(env_path, "r+", encoding="utf-8") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                content = f.read()
                if "BITRIX_WEBHOOK_TOKEN=" in content:
                    content = re.sub(
                        r"^BITRIX_WEBHOOK_TOKEN=.*$",
                        f"BITRIX_WEBHOOK_TOKEN={token}",
                        content,
                        flags=re.MULTILINE,
                    )
                else:
                    content += f"\nBITRIX_WEBHOOK_TOKEN={token}\n"
                f.seek(0)
                f.write(content)
                f.truncate()
                fcntl.flock(f, fcntl.LOCK_UN)
            logger.info("Saved BITRIX_WEBHOOK_TOKEN to %s", env_path)
        else:
            logger.warning(".env not found at %s — token not persisted", env_path)
    except Exception as exc:
        logger.error("Failed to save token to .env: %s", exc)

# Secret patterns to redact from logs
_SECRET_PATTERNS = re.compile(
    r"(auth|token|password|secret|webhook|key|pwd|access_token|refresh_token)"
    r"(['\"]?\s*[:=]\s*['\"]?)([^\s'\"&,;}{)\]]{4,})",
    re.IGNORECASE,
)


def _sanitize_for_log(data) -> str:
    """Redact sensitive values from data before writing to log."""
    if isinstance(data, (dict, list)):
        text = json.dumps(data, ensure_ascii=False, indent=2)
    else:
        text = str(data)
    # Redact secrets
    text = _SECRET_PATTERNS.sub(lambda m: m.group(1) + m.group(2) + "***REDACTED***", text)
    # Truncate very long values (e.g. base64 file content)
    if len(text) > 4096:
        text = text[:4096] + "\n... [TRUNCATED]"
    return text


def _rotate_log_if_needed() -> None:
    """Simple size-based log rotation."""
    if not LOG_FILE.exists():
        return
    try:
        if LOG_FILE.stat().st_size > LOG_MAX_SIZE:
            rotated = LOG_FILE.with_suffix(".log.1")
            if rotated.exists():
                rotated.unlink()
            LOG_FILE.rename(rotated)
    except OSError:
        pass


def write_log(title: str, data) -> None:
    _rotate_log_if_needed()
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"\n=== {title} ===\n")
        f.write(_sanitize_for_log(data))
        f.write("\n")


def split_key(key: str):
    return re.findall(r"([^\[\]]+)", key)


def nested_set(target: dict, parts: list[str], value):
    cur = target
    for part in parts[:-1]:
        if part not in cur or not isinstance(cur[part], dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value


def parse_bitrix_form(raw: bytes) -> dict:
    text = raw.decode("utf-8", errors="ignore")
    parsed = parse_qs(text, keep_blank_values=True)
    result = {}
    for key, values in parsed.items():
        value = values[0] if len(values) == 1 else values
        nested_set(result, split_key(key), value)
    return result


def detect_bot_id(payload: dict) -> str:
    params = payload.get("data", {}).get("PARAMS", {}) or {}
    if params.get("BOT_ID"):
        return str(params["BOT_ID"])

    bots = payload.get("data", {}).get("BOT", {}) or {}
    if isinstance(bots, dict) and bots:
        first_bot = next(iter(bots.values()))
        if isinstance(first_bot, dict):
            if first_bot.get("bot_id"):
                return str(first_bot["bot_id"])

    return BITRIX_BOT_ID


def detect_dialog_id(payload: dict) -> str:
    params = payload.get("data", {}).get("PARAMS", {}) or {}
    for key in ("DIALOG_ID", "CHAT_ID", "FROM_CHAT", "TO_CHAT"):
        value = str(params.get(key, "")).strip()
        if value:
            return value

    message_data = payload.get("data", {}).get("MESSAGE", {}) or {}
    if isinstance(message_data, dict):
        for key in ("DIALOG_ID", "CHAT_ID"):
            value = str(message_data.get(key, "")).strip()
            if value:
                return value

    return ""


def detect_message_text(payload: dict) -> str:
    params = payload.get("data", {}).get("PARAMS", {}) or {}
    for key in ("MESSAGE", "TEXT"):
        value = str(params.get(key, "")).strip()
        if value:
            return value

    message_data = payload.get("data", {}).get("MESSAGE", {}) or {}
    if isinstance(message_data, dict):
        for key in ("MESSAGE", "TEXT"):
            value = str(message_data.get(key, "")).strip()
            if value:
                return value

    return ""


def detect_message_id(payload: dict) -> int | None:
    message_data = payload.get("data", {}).get("MESSAGE", {}) or {}
    if isinstance(message_data, dict):
        for key in ("id", "ID", "MESSAGE_ID"):
            value = message_data.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.strip().isdigit():
                return int(value.strip())

    params = payload.get("data", {}).get("PARAMS", {}) or {}
    for key in ("MESSAGE_ID", "ID"):
        value = params.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


def _bridge_is_configured() -> bool:
    return BITRIX_WEBHOOK_BRIDGE_ENABLED and bool(MIRROR_INTERNAL_BASE_URL and MIRROR_INTERNAL_WEBHOOK_SECRET)


def send_bot_message(dialog_id: str, message: str, bot_id: str = ""):
    final_bot_id = bot_id or BITRIX_BOT_ID
    if not BITRIX_WEBHOOK_BASE:
        raise RuntimeError("BITRIX_WEBHOOK_BASE is empty")
    if not BITRIX_CLIENT_ID:
        raise RuntimeError("BITRIX_CLIENT_ID is empty")
    if not final_bot_id:
        raise RuntimeError("BOT_ID not found in payload and BITRIX_BOT_ID is empty")

    url = f"{BITRIX_WEBHOOK_BASE}/imbot.message.add.json"
    payload = {
        "BOT_ID": final_bot_id,
        "CLIENT_ID": BITRIX_CLIENT_ID,
        "DIALOG_ID": dialog_id,
        "MESSAGE": message,
        "URL_PREVIEW": "N",
    }

    import requests

    r = requests.post(url, data=payload, timeout=20)
    write_log("BITRIX_SEND_REQUEST", {"DIALOG_ID": dialog_id, "MESSAGE": message[:200]})
    write_log("BITRIX_SEND_RESPONSE", f"{r.status_code}")
    r.raise_for_status()
    return r.text


async def forward_event_to_mirror(*, event: str, dialog_id: str, message_id: int | None) -> None:
    if not _bridge_is_configured():
        raise RuntimeError("MIRROR_INTERNAL_BASE_URL or MIRROR_INTERNAL_WEBHOOK_SECRET is not configured")

    target_url = f"{MIRROR_INTERNAL_BASE_URL}{MIRROR_INTERNAL_EVENT_PATH}"
    payload = {
        "event": event,
        "dialog_id": dialog_id,
        "message_id": message_id,
    }
    headers = {"X-Internal-Webhook-Secret": MIRROR_INTERNAL_WEBHOOK_SECRET}

    async with httpx.AsyncClient(timeout=MIRROR_INTERNAL_TIMEOUT_SECONDS) as client:
        response = await client.post(target_url, json=payload, headers=headers)
        write_log("MIRROR_BRIDGE_REQUEST", payload)
        write_log("MIRROR_BRIDGE_RESPONSE", {"status_code": response.status_code, "body": response.text[:500]})
        response.raise_for_status()


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/bitrix/bot")
async def bitrix_bot(request: Request):
    raw = await request.body()
    payload = parse_bitrix_form(raw)

    # ── Webhook authentication ────────────────────────────────────────────
    incoming_token = _extract_incoming_token(payload)

    if not WEBHOOK_TOKEN:
        # First event — auto-capture the token and allow through
        _auto_capture_token(incoming_token)
    else:
        if incoming_token != WEBHOOK_TOKEN:
            logger.warning("Rejected webhook: invalid or missing application_token")
            raise HTTPException(status_code=403, detail="Forbidden")

    write_log("EVENT", {"event": payload.get("event", ""), "data_keys": list((payload.get("data") or {}).keys())})

    event = payload.get("event", "")
    params = payload.get("data", {}).get("PARAMS", {}) or {}

    dialog_id = detect_dialog_id(payload)
    message_text = detect_message_text(payload)
    message_id = detect_message_id(payload)
    bot_id = detect_bot_id(payload)

    write_log(
        "DETECTED_CONTEXT",
        {
            "event": event,
            "dialog_id": dialog_id,
            "message_id": message_id,
            "message_text": message_text[:200] if message_text else "",
            "bot_id": bot_id,
            "has_params": bool(params),
        },
    )

    try:
        if event in FORWARDED_EVENTS and dialog_id and _bridge_is_configured():
            await forward_event_to_mirror(event=event, dialog_id=dialog_id, message_id=message_id)
        elif event == "ONIMBOTJOINCHAT" and dialog_id:
            send_bot_message(
                dialog_id=dialog_id,
                bot_id=bot_id,
                message="Привет. Бот подключён и готов к работе."
            )

        elif event == "ONIMBOTMESSAGEADD" and dialog_id:
            text_lower = message_text.lower()
            reply = None

            if text_lower in {"/start", "start"}:
                reply = "Привет. Я получил сообщение и могу отвечать в этот чат."
            elif text_lower in {"/ping", "ping"}:
                reply = "pong"

            if reply is not None:
                send_bot_message(
                    dialog_id=dialog_id,
                    bot_id=bot_id,
                    message=reply
                )

    except Exception as e:
        write_log("ERROR", repr(e))
        logger.exception("Bot handler error")
        raise HTTPException(status_code=502, detail="Bridge delivery failed") from e

    return {"result": True}
