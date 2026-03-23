# Инструкция по развёртыванию Telegram ↔ Bitrix бота

Ниже описан вариант, при котором **все артефакты лежат в одном репозитории**:

- Python-код Telegram-бота и логики синхронизации
- FastAPI webhook-обработчик для событий Bitrix
- `requirements.txt`
- шаблон env-файла
- конфиг NGINX
- unit-файл systemd

## Что находится в репозитории

- `main.py` — основной Telegram polling-процесс
- `server-side/app.py` — HTTP webhook для Bitrix
- `requirements.txt` — Python-зависимости для обоих процессов
- `env.example` — основной пример env-файла
- `server-side/bitrix-bot.env.example` — пример env-файла для сервера
- `server-side/nginx` — шаблон конфигурации NGINX
- `server-side/bitrix-bot.service` — шаблон unit-файла systemd

## Архитектура запуска

На сервере запускаются два процесса:

1. `python main.py` — Telegram-бот, который работает через polling
2. `uvicorn app:app --host 127.0.0.1 --port 8081` — HTTP endpoint для webhook от Bitrix

NGINX принимает внешний HTTP(S)-трафик и проксирует запросы на `127.0.0.1:8081`.

## Требования к серверу

Рекомендуемый минимальный стек:

- Ubuntu 22.04/24.04 или другой Linux с `systemd`
- Python 3.11+
- NGINX
- доступ в интернет для Telegram API и Bitrix24
- домен или поддомен для webhook Bitrix

## Структура на сервере

Пример целевого размещения:

```text
/opt/bitrix-bot/
├── .venv/
├── server-side/
├── main.py
├── requirements.txt
├── env.example
├── .env
├── mirror_state.sqlite3
└── bitrix_cursor_state.json
```

## 1. Установка системных пакетов

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nginx
```

Если используете HTTPS через Let's Encrypt, дополнительно:

```bash
sudo apt install -y certbot python3-certbot-nginx
```

## 2. Клонирование репозитория

```bash
sudo mkdir -p /opt/bitrix-bot
sudo chown "$USER":"$USER" /opt/bitrix-bot
cd /opt/bitrix-bot
git clone <URL_ВАШЕГО_РЕПОЗИТОРИЯ> .
```

## 3. Создание виртуального окружения и установка зависимостей

```bash
cd /opt/bitrix-bot
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## 4. Настройка переменных окружения

Создайте рабочий env-файл:

```bash
cd /opt/bitrix-bot
cp server-side/bitrix-bot.env.example .env
```

Или используйте `env.example` как основу:

```bash
cp env.example .env
```

### Минимально обязательные переменные

Заполните в `.env`:

```dotenv
TELEGRAM_BOT_TOKEN=...
BITRIX_WEBHOOK_BASE=https://YOUR_COMPANY.bitrix24.ru/rest/1/WEBHOOK_CODE
CHAT_MAPPINGS=[{"tg_chat_id": -1001234567890, "bitrix_dialog_id": "chat2941"}]
```

### Если включён режим чат-бота Bitrix

Нужно также указать:

```dotenv
BITRIX_USE_CHAT_BOT=true
BITRIX_BOT_ID=123
BITRIX_BOT_CLIENT_ID=local.******
```

Для `server-side/app.py` допустимы оба имени переменной client id:

- `BITRIX_CLIENT_ID`
- `BITRIX_BOT_CLIENT_ID`

### Важные замечания по env

- `CHAT_MAPPINGS` — рекомендуемый режим для нескольких чатов.
- Если `CHAT_MAPPINGS` пуст, код перейдёт в legacy-режим и потребует:
  - `BITRIX_DIALOG_ID`
  - `ALLOWED_TELEGRAM_CHAT_ID`
- `MIRROR_STATE_DB_PATH` лучше сразу указывать абсолютным путём, например:

```dotenv
MIRROR_STATE_DB_PATH=/opt/bitrix-bot/mirror_state.sqlite3
BITRIX_CURSOR_STATE_PATH=/opt/bitrix-bot/bitrix_cursor_state.json
```

## 5. Проверка локального запуска на сервере

### Проверка Telegram-процесса

```bash
cd /opt/bitrix-bot
source .venv/bin/activate
set -a
source .env
set +a
python main.py
```

### Проверка webhook-процесса

```bash
cd /opt/bitrix-bot
source .venv/bin/activate
set -a
source .env
set +a
cd server-side
uvicorn app:app --host 127.0.0.1 --port 8081
```

Проверка health endpoint:

```bash
curl http://127.0.0.1:8081/health
```

Ожидаемый ответ:

```json
{"ok":true}
```

## 6. Настройка NGINX

В репозитории уже есть шаблон `server-side/nginx`. Скопируйте его в `/etc/nginx/sites-available/bitrix-bot` и поменяйте домен.

Пример конфига:

```nginx
server {
    listen 80;
    server_name bot.example.com;

    location /bitrix/bot {
        proxy_pass http://127.0.0.1:8081/bitrix/bot;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /health {
        return 200 "ok\n";
        add_header Content-Type text/plain;
    }
}
```

Команды установки:

```bash
sudo cp /opt/bitrix-bot/server-side/nginx /etc/nginx/sites-available/bitrix-bot
sudo nano /etc/nginx/sites-available/bitrix-bot
sudo ln -sf /etc/nginx/sites-available/bitrix-bot /etc/nginx/sites-enabled/bitrix-bot
sudo nginx -t
sudo systemctl reload nginx
```

### Если нужен HTTPS

После выпуска сертификата конфиг можно автоматически обновить:

```bash
sudo certbot --nginx -d bot.example.com
```

После этого укажите в Bitrix webhook URL вида:

```text
https://bot.example.com/bitrix/bot
```

## 7. Настройка systemd для webhook-процесса

В репозитории есть шаблон `server-side/bitrix-bot.service`.

Установка:

```bash
sudo cp /opt/bitrix-bot/server-side/bitrix-bot.service /etc/systemd/system/bitrix-bot.service
sudo nano /etc/systemd/system/bitrix-bot.service
```

Проверьте, что внутри актуальны:

- `WorkingDirectory=/opt/bitrix-bot/server-side`
- `EnvironmentFile=/opt/bitrix-bot/.env`
- `ExecStart=/opt/bitrix-bot/.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8081`

Рекомендуемый unit:

```ini
[Unit]
Description=Bitrix Bot Webhook Handler
After=network.target

[Service]
User=root
WorkingDirectory=/opt/bitrix-bot/server-side
EnvironmentFile=/opt/bitrix-bot/.env
ExecStart=/opt/bitrix-bot/.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8081
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Применение:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bitrix-bot.service
sudo systemctl status bitrix-bot.service
```

## 8. Настройка systemd для Telegram polling-процесса

Для основного процесса нужен отдельный unit. Добавьте на сервере файл `/etc/systemd/system/bitrix-telegram-mirror.service`:

```ini
[Unit]
Description=Telegram Bitrix Mirror Bot
After=network.target

[Service]
User=root
WorkingDirectory=/opt/bitrix-bot
EnvironmentFile=/opt/bitrix-bot/.env
ExecStart=/opt/bitrix-bot/.venv/bin/python /opt/bitrix-bot/main.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Активируйте его:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bitrix-telegram-mirror.service
sudo systemctl status bitrix-telegram-mirror.service
```

## 9. Настройка webhook в Bitrix24

В Bitrix укажите адрес обработчика:

```text
https://bot.example.com/bitrix/bot
```

Дальше зарегистрируйте нужные события Bitrix на этот URL в настройках вашего приложения / бота.

## 10. Полезные команды для сопровождения

### Просмотр логов webhook-процесса

```bash
journalctl -u bitrix-bot.service -f
```

### Просмотр логов Telegram-процесса

```bash
journalctl -u bitrix-telegram-mirror.service -f
```

### Перезапуск после обновления кода

```bash
cd /opt/bitrix-bot
git pull
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart bitrix-bot.service
sudo systemctl restart bitrix-telegram-mirror.service
```

## 11. Что должно быть в одном репозитории

Чтобы репозиторий был самодостаточным, в нём уже должны лежать:

- исходники Python
- `requirements.txt`
- `.env` шаблон (`env.example` или `server-side/bitrix-bot.env.example`)
- `server-side/nginx`
- `server-side/bitrix-bot.service`
- эта инструкция `DEPLOYMENT.md`

Если хотите, следующим сообщением я могу ещё подготовить:

1. готовый `README.md` с кратким quick start;
2. отдельный systemd unit-файл для `main.py` прямо в репозитории;
3. production-ready HTTPS-конфиг NGINX с redirect `80 -> 443`.
