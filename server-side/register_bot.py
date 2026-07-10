import sys
import os
import secrets
import urllib.request
import urllib.error
import json

def call_rest(webhook_url, method, payload):
    url = f"{webhook_url.rstrip('/')}/{method}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            return json.loads(res.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode('utf-8'))
        except Exception:
            return {"error": "HTTP_ERROR", "error_description": str(e)}
    except Exception as e:
        return {"error": "CONNECTION_ERROR", "error_description": str(e)}

def print_result(status, action, bot_id, bot_token, message):
    print(f"status={status}")
    print(f"action={action}")
    print(f"bot_id={bot_id}")
    print(f"bot_token={bot_token}")
    print(f"message={message}")

def print_error(message):
    print(f"status=error")
    print(f"message={message}")

def main():
    if len(sys.argv) < 2:
        print_error("Usage: register_bot.py <webhook_base> [<bot_id> <bot_token>]")
        sys.exit(1)

    webhook_base = sys.argv[1]
    existing_bot_id = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2].strip() else None
    existing_bot_token = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3].strip() else None

    # Step 1: Check if current credentials are valid and support V2
    if existing_bot_id and existing_bot_token:
        try:
            bot_id_int = int(existing_bot_id)
            res = call_rest(webhook_base, "imbot.v2.Event.get", {
                "botId": bot_id_int,
                "botToken": existing_bot_token,
                "limit": 1
            })
            if "result" in res and "error" not in res:
                print_result("ok", "none", bot_id_int, existing_bot_token, "Бот успешно проверен и уже работает на API 2.0")
                sys.exit(0)
        except Exception:
            pass

    # Step 2: Query list of bots to find if tg_mirror_bot exists
    res = call_rest(webhook_base, "imbot.bot.list", {})
    if "error" in res:
        print_error(f"Не удалось получить список ботов: {res.get('error')} | {res.get('error_description')}")
        sys.exit(1)

    bots = res.get("result") or {}
    found_bot_id = None
    
    if isinstance(bots, dict):
        for bid, bdata in bots.items():
            if isinstance(bdata, dict) and bdata.get("CODE") == "tg_mirror_bot":
                found_bot_id = bid
                break
    elif isinstance(bots, list):
        for bdata in bots:
            if isinstance(bdata, dict) and bdata.get("CODE") == "tg_mirror_bot":
                found_bot_id = bdata.get("ID")
                break

    # Step 3: If found, unregister it
    if found_bot_id:
        # Try both v2 and legacy unregister methods to ensure cleanup
        call_rest(webhook_base, "imbot.v2.Bot.unregister", {
            "botId": found_bot_id
        })
        call_rest(webhook_base, "imbot.unregister", {
            "BOT_ID": found_bot_id,
            "botId": found_bot_id
        })

    # Step 4: Register new bot using imbot.v2.Bot.register
    new_token = secrets.token_hex(16)
    reg_payload = {
        "botToken": new_token,
        "fields": {
            "code": "tg_mirror_bot",
            "type": "supervisor",
            "eventMode": "fetch",
            "isHidden": False,
            "properties": {
                "name": "Telegram Mirror",
                "desc": "Mirrors chats between Telegram and Bitrix24"
            }
        }
    }
    
    reg_res = call_rest(webhook_base, "imbot.v2.Bot.register", reg_payload)
    if "error" in reg_res:
        print_error(f"Ошибка регистрации бота: {reg_res.get('error')} | {reg_res.get('error_description')}")
        sys.exit(1)

    new_bot_id = reg_res.get("result")
    if not isinstance(new_bot_id, (int, str)):
        print_error(f"Некорректный ID нового бота в ответе: {reg_res}")
        sys.exit(1)

    print_result("ok", "registered", int(new_bot_id), new_token, "Бот успешно зарегистрирован с Chatbot API 2.0")

if __name__ == "__main__":
    main()
