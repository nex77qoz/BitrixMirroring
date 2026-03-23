# Инструкция по развёртыванию Telegram ↔ Bitrix бота

Документ описывает развёртывание **всех серверных компонентов проекта** из одного репозитория:

- Telegram polling-процесс для двустороннего зеркалирования сообщений;
- FastAPI webhook-процесс для входящих событий Bitrix;
- monitoring dashboard для просмотра статусов сервисов, логов и управления chat mapping;
- NGINX как внешний reverse proxy;
- `systemd` unit-файлы для каждого фонового процесса.

## Что находится в репозитории

- `main.py` — основной Telegram polling-процесс. 
- `server-side/app.py` — HTTP webhook для событий Bitrix. 
- `server-side/monitor_app.py` — monitoring dashboard с Basic Auth и управлением mapping'ами. 
- `requirements.txt` — общие Python-зависимости для всех процессов. 
- `env.example` — полный пример `.env` для локального и серверного запуска. 
- `server-side/bitrix-bot.env.example` — серверный шаблон `.env` с абсолютными путями. 
- `server-side/nginx` — шаблон конфигурации NGINX, который проксирует `/bitrix/bot` и `/monitor`. 
- `server-side/bitrix-bot.service` — systemd unit для webhook-процесса. 
- `server-side/bitrix-telegram-mirror.service` — systemd unit для Telegram polling-процесса. 
- `server-side/bitrix-monitor.service` — systemd unit для monitoring dashboard. 

## Архитектура запуска

В production запускаются **три systemd-сервиса** и один reverse proxy:

1. `bitrix-telegram-mirror.service` → `python /opt/bitrix-bot/main.py`  
   Основной Telegram-бот, который работает через polling и синхронизирует Telegram ↔ Bitrix.
2. `bitrix-bot.service` → `uvicorn app:app --host 127.0.0.1 --port 8081`  
   HTTP endpoint для webhook от Bitrix.
3. `bitrix-monitor.service` → `uvicorn monitor_app:app --host 127.0.0.1 --port 8082`  
   Внутренний monitoring dashboard с просмотром статусов и логов сервисов, а также управлением `chat_mappings`.
4. `nginx`  
   Принимает внешний HTTP(S)-трафик и проксирует:
   - `/bitrix/bot` → `127.0.0.1:8081`
   - `/monitor` → `127.0.0.1:8082`

> Важно: monitoring dashboard сам читает общий `.env` и использует ту же SQLite базу `MIRROR_STATE_DB_PATH`, что и основной mirror-сервис.

## Требования к серверу

Рекомендуемый минимальный стек:

- Ubuntu 22.04/24.04 или другой Linux с `systemd`; 
- Python 3.11+; 
- NGINX; 
- доступ в интернет для Telegram API и Bitrix24; 
- домен или поддомен для webhook Bitrix; 
- при использовании `/monitor` во внешней сети — надёжный пароль `MONITOR_PASSWORD`, а лучше ещё и HTTPS.

## Целевая структура на сервере

Пример размещения:

```text
/opt/bitrix-bot/
├── .venv/
├── server-side/
│   ├── app.py
│   ├── monitor_app.py
│   ├── bitrix-bot.service
│   ├── bitrix-telegram-mirror.service
│   ├── bitrix-monitor.service
│   └── nginx
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

Если нужен HTTPS через Let's Encrypt, дополнительно:

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

Либо используйте более полный шаблон:

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

- `BITRIX_CLIENT_ID`;
- `BITRIX_BOT_CLIENT_ID`.

### Переменные для monitoring dashboard

Для `server-side/monitor_app.py` дополнительно рекомендуется задать:

```dotenv
MONITOR_USERNAME=admin
MONITOR_PASSWORD=СЛОЖНЫЙ_ПАРОЛЬ
```

- `MONITOR_PASSWORD` обязателен для нормальной работы `/monitor`;
- если пароль не задан, dashboard будет отвечать ошибкой конфигурации;
- dashboard использует `MIRROR_STATE_DB_PATH`, поэтому путь к SQLite-файлу должен быть одинаковым для mirror-сервиса и monitor-сервиса.

### Важные замечания по `.env`

- `CHAT_MAPPINGS` — рекомендуемый режим для нескольких чатов;
- если `CHAT_MAPPINGS` пуст, код перейдёт в legacy-режим и потребует:
  - `BITRIX_DIALOG_ID`;
  - `ALLOWED_TELEGRAM_CHAT_ID`;
- `MIRROR_STATE_DB_PATH` лучше сразу указывать абсолютным путём;
- `BITRIX_CURSOR_STATE_PATH` тоже лучше указывать абсолютным путём;
- для server-side deployment удобно использовать значения вида:

```dotenv
MIRROR_STATE_DB_PATH=/opt/bitrix-bot/mirror_state.sqlite3
BITRIX_CURSOR_STATE_PATH=/opt/bitrix-bot/bitrix_cursor_state.json
```

## 5. Проверка ручного запуска всех сервисов

Перед настройкой `systemd` полезно проверить каждый процесс вручную.

### 5.1. Проверка Telegram polling-процесса

```bash
cd /opt/bitrix-bot
source .venv/bin/activate
set -a
source .env
set +a
python main.py
```

### 5.2. Проверка webhook-процесса

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

### 5.3. Проверка monitoring dashboard

```bash
cd /opt/bitrix-bot
source .venv/bin/activate
set -a
source .env
set +a
cd server-side
uvicorn monitor_app:app --host 127.0.0.1 --port 8082
```

Проверка health endpoint:

```bash
curl http://127.0.0.1:8082/monitor/health
```

Ожидаемый ответ:

```json
{"ok":true}
```

Если `MONITOR_PASSWORD` задан, интерфейс будет доступен по адресу:

```text
http://127.0.0.1:8082/monitor
```

## 6. Настройка NGINX

В репозитории уже есть шаблон `server-side/nginx`. Скопируйте его в `/etc/nginx/sites-available/bitrix-bot` и замените домен.

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

    location /monitor {
        proxy_pass http://127.0.0.1:8082;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
        proxy_buffering off;
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

После этого внешний webhook URL в Bitrix должен иметь вид:

```text
https://bot.example.com/bitrix/bot
```

А monitoring dashboard будет доступен по адресу:

```text
https://bot.example.com/monitor
```

## 7. Настройка systemd для webhook-процесса

В репозитории есть шаблон `server-side/bitrix-bot.service`.

Установка:

```bash
sudo cp /opt/bitrix-bot/server-side/bitrix-bot.service /etc/systemd/system/bitrix-bot.service
sudo nano /etc/systemd/system/bitrix-bot.service
```

Проверьте, что внутри актуальны:

- `WorkingDirectory=/opt/bitrix-bot/server-side`;
- `EnvironmentFile=/opt/bitrix-bot/.env`;
- `ExecStart=/opt/bitrix-bot/.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8081`.

Применение:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bitrix-bot.service
sudo systemctl status bitrix-bot.service
```

## 8. Настройка systemd для Telegram polling-процесса

В репозитории уже есть шаблон `server-side/bitrix-telegram-mirror.service`.

Установка:

```bash
sudo cp /opt/bitrix-bot/server-side/bitrix-telegram-mirror.service /etc/systemd/system/bitrix-telegram-mirror.service
sudo nano /etc/systemd/system/bitrix-telegram-mirror.service
```

Проверьте, что внутри актуальны:

- `WorkingDirectory=/opt/bitrix-bot`;
- `EnvironmentFile=/opt/bitrix-bot/.env`;
- `ExecStart=/opt/bitrix-bot/.venv/bin/python /opt/bitrix-bot/main.py`.

Применение:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bitrix-telegram-mirror.service
sudo systemctl status bitrix-telegram-mirror.service
```

## 9. Настройка systemd для monitoring dashboard

В репозитории уже есть шаблон `server-side/bitrix-monitor.service`.

Установка:

```bash
sudo cp /opt/bitrix-bot/server-side/bitrix-monitor.service /etc/systemd/system/bitrix-monitor.service
sudo nano /etc/systemd/system/bitrix-monitor.service
```

Проверьте, что внутри актуальны:

- `WorkingDirectory=/opt/bitrix-bot/server-side`;
- `EnvironmentFile=/opt/bitrix-bot/.env`;
- `ExecStart=/opt/bitrix-bot/.venv/bin/uvicorn monitor_app:app --host 127.0.0.1 --port 8082`.

Применение:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bitrix-monitor.service
sudo systemctl status bitrix-monitor.service
```

## 10. Настройка webhook в Bitrix24

В Bitrix укажите адрес обработчика:

```text
https://bot.example.com/bitrix/bot
```

После этого зарегистрируйте нужные события Bitrix на этот URL в настройках вашего приложения или бота.

## 11. Полезные команды для сопровождения

### Проверка статусов всех сервисов

```bash
sudo systemctl status bitrix-bot.service bitrix-telegram-mirror.service bitrix-monitor.service
```

### Просмотр логов webhook-процесса

```bash
journalctl -u bitrix-bot.service -f
```

### Просмотр логов Telegram-процесса

```bash
journalctl -u bitrix-telegram-mirror.service -f
```

### Просмотр логов monitoring dashboard

```bash
journalctl -u bitrix-monitor.service -f
```

### Перезапуск всех процессов после обновления кода

```bash
cd /opt/bitrix-bot
git pull
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart bitrix-bot.service
sudo systemctl restart bitrix-telegram-mirror.service
sudo systemctl restart bitrix-monitor.service
```

### Если вы добавили или удалили chat mapping через `/monitor`

После изменений в `chat_mappings` рекомендуется перезапустить mirror-сервис, чтобы он перечитал конфигурацию и состояние:

```bash
sudo systemctl restart bitrix-telegram-mirror.service
```

## 12. Краткий checklist после деплоя

1. `bitrix-bot.service` активен и отвечает на `http://127.0.0.1:8081/health`;
2. `bitrix-telegram-mirror.service` активен и не падает в цикле перезапуска;
3. `bitrix-monitor.service` активен и отвечает на `http://127.0.0.1:8082/monitor/health`;
4. NGINX проксирует `/bitrix/bot` и `/monitor`;
5. в `.env` задан `MONITOR_PASSWORD`;
6. `MIRROR_STATE_DB_PATH` указывает на один и тот же SQLite-файл для mirror и monitor;
7. в Bitrix зарегистрирован webhook URL `https://bot.example.com/bitrix/bot`.

## 13. Что должно быть в одном репозитории

Чтобы репозиторий был самодостаточным, в нём уже должны лежать:

- исходники Python;
- `requirements.txt`;
- `.env` шаблон (`env.example` или `server-side/bitrix-bot.env.example`);
- `server-side/nginx`;
- `server-side/bitrix-bot.service`;
- `server-side/bitrix-telegram-mirror.service`;
- `server-side/bitrix-monitor.service`;
- эта инструкция `DEPLOYMENT.md`.
