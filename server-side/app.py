from fastapi import FastAPI, Request
from pathlib import Path
from urllib.parse import parse_qs
import json
import logging
import os
import re
import requests

app = FastAPI()
LOG_FILE = Path("/opt/bitrix-bot/bitrix.log")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("bitrix-bot")

BITRIX_WEBHOOK_BASE = os.getenv("BITRIX_WEBHOOK_BASE", "").rstrip("/")
BITRIX_CLIENT_ID = (
    os.getenv("BITRIX_CLIENT_ID", "").strip()
    or os.getenv("BITRIX_BOT_CLIENT_ID", "").strip()
)
BITRIX_BOT_ID = os.getenv("BITRIX_BOT_ID", "").strip()  # опционально, как fallback


def write_log(title: str, data) -> None:
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"\n=== {title} ===\n")
        if isinstance(data, (dict, list)):
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            f.write(str(data))
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

    r = requests.post(url, data=payload, timeout=20)
    write_log("BITRIX_SEND_REQUEST", payload)
    write_log("BITRIX_SEND_RESPONSE", f"{r.status_code} {r.text}")
    r.raise_for_status()
    return r.text


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/bitrix/bot")
async def bitrix_bot(request: Request):
    raw = await request.body()
    payload = parse_bitrix_form(raw)

    write_log("RAW", raw.decode("utf-8", errors="ignore"))
    write_log("PARSED", payload)

    event = payload.get("event", "")
    params = payload.get("data", {}).get("PARAMS", {}) or {}

    dialog_id = detect_dialog_id(payload)
    message_text = detect_message_text(payload)
    bot_id = detect_bot_id(payload)

    write_log(
        "DETECTED_CONTEXT",
        {
            "event": event,
            "dialog_id": dialog_id,
            "message_text": message_text,
            "bot_id": bot_id,
            "has_params": bool(params),
            "client_id_source": "BITRIX_CLIENT_ID" if os.getenv("BITRIX_CLIENT_ID", "").strip() else "BITRIX_BOT_CLIENT_ID",
        },
    )

    try:
        if event == "ONIMBOTJOINCHAT" and dialog_id:
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

    return {"result": True}
