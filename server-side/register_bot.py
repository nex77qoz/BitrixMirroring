import json
import secrets
import sys
import urllib.error
import urllib.request

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
    print("status=error")
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

    # Step 2: Register a new bot. The list method requires botToken with webhook auth,
    # which is unavailable during first installation.
    candidate_codes = ["tg_mirror_bot", "tg_mirror_bot_v2", "tg_mirror_bot_v3"]

    # Step 3: Register using imbot.v2.Bot.register (trying candidate codes in sequence)
    new_token = secrets.token_hex(16)
    reg_res = None
    registered_code = None

    for code in candidate_codes:
        reg_payload = {
            "botToken": new_token,
            "fields": {
                "code": code,
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
        if "error" not in reg_res:
            registered_code = code
            break
        elif reg_res.get("error") != "BOT_CODE_ALREADY_TAKEN":
            # If the error is not about code conflict, break early (e.g. portal issue, permissions error)
            break

    if not registered_code or "error" in reg_res:
        print_error(f"Ошибка регистрации бота: {reg_res.get('error')} | {reg_res.get('error_description')}")
        sys.exit(1)

    result_data = reg_res.get("result")
    new_bot_id = None
    if isinstance(result_data, dict):
        new_bot_id = result_data.get("bot", {}).get("id")
    elif isinstance(result_data, (int, str)):
        new_bot_id = result_data

    if new_bot_id is None or not isinstance(new_bot_id, (int, str)):
        print_error(f"Некорректный ID нового бота в ответе: {reg_res}")
        sys.exit(1)

    print_result("ok", "registered", int(new_bot_id), new_token, f"Бот успешно зарегистрирован с Chatbot API 2.0 (код: {registered_code})")

if __name__ == "__main__":
    main()
