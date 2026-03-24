# Инструкция по развёртыванию Telegram ↔ Bitrix бота

Документ описывает развёртывание **всех серверных компонентов проекта** из одного репозитория:

- Telegram polling/webhook-процесс для двустороннего зеркалирования сообщений;
- FastAPI webhook-процесс для входящих событий Bitrix;
- monitoring dashboard для просмотра статусов сервисов, логов и управления chat mapping;
- NGINX как внешний reverse proxy;
- `systemd` unit-файлы для каждого фонового процесса.

## Что находится в репозитории

- `main.py` — основной Telegram polling/webhook-процесс. 
- `server-side/app.py` — HTTP webhook для событий Bitrix. 
- `server-side/monitor_app.py` — monitoring dashboard с Basic Auth и управлением mapping'ами. 
- `requirements.txt` — общие Python-зависимости для всех процессов. 
- `env.example` — полный пример `.env` для локального и серверного запуска. 
- `server-side/bitrix-bot.env.example` — серверный шаблон `.env` с абсолютными путями. 
- `server-side/nginx` — шаблон конфигурации NGINX, который проксирует `/bitrix/bot`, `/telegram/webhook` и `/monitor`. 
- `server-side/bitrix-bot.service` — systemd unit для webhook-процесса. 
- `server-side/bitrix-telegram-mirror.service` — systemd unit для Telegram polling-процесса. 
- `server-side/bitrix-monitor.service` — systemd unit для monitoring dashboard. 

## Архитектура запуска

В production запускаются **три systemd-сервиса** и один reverse proxy:

1. `bitrix-telegram-mirror.service` → `python /opt/bitrix-bot/main.py`  
    Основной Telegram-бот, который работает через polling или webhook и синхронизирует Telegram ↔ Bitrix. При включённом Bitrix bridge этот же процесс поднимает внутренний HTTP endpoint на localhost.
2. `bitrix-bot.service` → `uvicorn app:app --host 127.0.0.1 --port 8081`  
   HTTP endpoint для webhook от Bitrix.
3. `bitrix-monitor.service` → `uvicorn monitor_app:app --host 127.0.0.1 --port 8082`  
   Внутренний monitoring dashboard с просмотром статусов и логов сервисов, а также управлением `chat_mappings`.
4. `nginx`  
   Принимает внешний HTTP(S)-трафик и проксирует:
   - `/bitrix/bot` → `127.0.0.1:8081`
    - `/telegram/webhook` → `127.0.0.1:8090`
   - `/monitor` → `127.0.0.1:8082`

> Важно: monitoring dashboard сам читает общий `.env` и использует ту же SQLite базу `MIRROR_STATE_DB_PATH`, что и основной mirror-сервис.

## Требования к серверу

Рекомендуемый минимальный стек:

- Ubuntu 22.04/24.04 или другой Linux с `systemd`; 
- Python 3.11+; 
- NGINX; 
- доступ в интернет для Telegram API и Bitrix24; 
- домен или поддомен для webhook Bitrix; 
- при использовании `/monitor` во внешней сети — надёжный пароль `MONITOR_PASSWORD`, HTTPS и ограничение по IP.

## Автоматическая установка (рекомендуется)

Скрипт `install.sh` выполняет полную установку со всеми мерами безопасности:

```bash
sudo bash install.sh
```

Вместе с основной установкой скрипт настроит:

- **Сервисный пользователь** `bitrix-bot` — все сервисы работают под ним, а не под `root`;
- **SSH-безопасность** — опциональное отключение пароля / запрет root-логина;
- **HTTPS** через acme.sh с TLS 1.2/1.3;
- **HTTP security headers** — HSTS, X-Content-Type-Options, X-Frame-Options и др.;
- **Rate limiting** — nginx limit_req для `/bitrix/bot` и `/monitor`;
- **IP-ограничение** для `/monitor`;
- **Webhook-аутентификация** — проверка `application_token` Bitrix;
- **Fail2ban** — автоматическая блокировка IP при превышении лимитов;
- **UFW файрвол** — только порты 22, 80, 443;
- **Logrotate** — ежедневная ротация логов, хранение 7 дней;
- **Очистка БД** — автоматическое удаление записей старше 7 дней;
- **Лимит файлов** — 100 МБ на файл, 10 ГБ кэш с автоочисткой;
- **Санитизация логов** — секреты и токены не попадают в логи.

Управление:

```bash
sudo bash /opt/bitrix-bot/install.sh --update      # обновление
sudo bash /opt/bitrix-bot/install.sh --uninstall    # удаление
```

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
BITRIX_BOT_CLIENT_ID=local.******
```

### Переменные для bot client id в server-side/app.py

Допустимы оба имени переменной client id:

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
- для мгновенного Bitrix → Telegram bridge дополнительно задайте:

```dotenv
BITRIX_WEBHOOK_BRIDGE_ENABLED=true
MIRROR_HTTP_HOST=127.0.0.1
MIRROR_HTTP_PORT=8090
MIRROR_INTERNAL_BASE_URL=http://127.0.0.1:8090
MIRROR_INTERNAL_EVENT_PATH=/internal/bitrix/event
MIRROR_INTERNAL_WEBHOOK_SECRET=СЛОЖНЫЙ_СЕКРЕТ
```

- для перевода Telegram на webhook задайте:

```dotenv
TELEGRAM_WEBHOOK_ENABLED=true
TELEGRAM_WEBHOOK_PUBLIC_URL=https://bot.example.com
TELEGRAM_WEBHOOK_PATH=/telegram/webhook
TELEGRAM_WEBHOOK_SECRET=СЛОЖНЫЙ_СЕКРЕТ
TELEGRAM_WEBHOOK_DROP_PENDING_UPDATES=true
TELEGRAM_WEBHOOK_STRICT_VERIFY=true
```

- для server-side deployment удобно использовать значения вида:

```dotenv
MIRROR_STATE_DB_PATH=/opt/bitrix-bot/mirror_state.sqlite3
BITRIX_CURSOR_STATE_PATH=/opt/bitrix-bot/bitrix_cursor_state.json
```

## 5. Проверка ручного запуска всех сервисов

Перед настройкой `systemd` полезно проверить каждый процесс вручную.

### 5.1. Проверка Telegram mirror-процесса

```bash
cd /opt/bitrix-bot
source .venv/bin/activate
set -a
source .env
set +a
python main.py
```

Если включён `BITRIX_WEBHOOK_BRIDGE_ENABLED=true` или `TELEGRAM_WEBHOOK_ENABLED=true`, основной процесс также поднимет HTTP listener на `MIRROR_HTTP_HOST:MIRROR_HTTP_PORT`. Для локальной проверки:

```bash
curl http://127.0.0.1:8090/health
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

### Если нужен HTTPS (ручная установка)

При автоматической установке через `install.sh` сертификат выпускается скриптом через **acme.sh**. При ручной установке:

#### Установка acme.sh

```bash
curl https://get.acme.sh | sh -s email=your@email.com
source ~/.bashrc
```

#### Выпуск сертификата

```bash
# Остановите nginx на время выпуска (standalone-режим)
sudo systemctl stop nginx

~/.acme.sh/acme.sh --issue -d bot.example.com --standalone

# Установка сертификата
mkdir -p /etc/ssl/bitrix-bot
~/.acme.sh/acme.sh --install-cert -d bot.example.com \
    --key-file       /etc/ssl/bitrix-bot/key.pem \
    --fullchain-file /etc/ssl/bitrix-bot/cert.pem \
    --reloadcmd      "systemctl reload nginx"

sudo systemctl start nginx
```

#### Пример HTTPS-конфига nginx с security headers

```nginx
# Rate limiting zones
limit_req_zone $binary_remote_addr zone=webhook:10m rate=30r/s;
limit_req_zone $binary_remote_addr zone=monitor:10m rate=10r/s;

# HTTP → HTTPS redirect
server {
    listen 80;
    server_name bot.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name bot.example.com;

    ssl_certificate     /etc/ssl/bitrix-bot/cert.pem;
    ssl_certificate_key /etc/ssl/bitrix-bot/key.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305;
    ssl_prefer_server_ciphers on;
    ssl_session_timeout 1d;
    ssl_session_cache   shared:SSL:10m;
    ssl_session_tickets off;

    # Security headers
    add_header Strict-Transport-Security "max-age=63072000; includeSubDomains; preload" always;
    add_header X-Content-Type-Options    nosniff always;
    add_header X-Frame-Options           DENY always;
    add_header X-XSS-Protection          "1; mode=block" always;
    add_header Referrer-Policy           "strict-origin-when-cross-origin" always;
    add_header Permissions-Policy        "camera=(), microphone=(), geolocation=()" always;

    client_max_body_size 100m;

    location /bitrix/bot {
        limit_req zone=webhook burst=50 nodelay;
        proxy_pass http://127.0.0.1:8081/bitrix/bot;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /telegram/webhook {
        limit_req zone=webhook burst=50 nodelay;
        proxy_pass http://127.0.0.1:8090/telegram/webhook;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /monitor {
        # Ограничение по IP (замените на свои)
        allow 1.2.3.4;
        deny all;

        limit_req zone=monitor burst=20 nodelay;
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

    location / {
        return 444;
    }
}
```

После этого внешний webhook URL в Bitrix должен иметь вид:

```text
https://bot.example.com/bitrix/bot
```

А monitoring dashboard будет доступен по адресу:

```text
https://bot.example.com/monitor
```

Если Telegram переведён на webhook, внешний URL для Bot API будет таким:

```text
https://bot.example.com/telegram/webhook
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

## 8. Настройка systemd для Telegram mirror-процесса

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

Если включён `TELEGRAM_WEBHOOK_ENABLED=true`, `main.py` сам вызывает `setWebhook`, затем проверяет результат через `getWebhookInfo`. При `TELEGRAM_WEBHOOK_STRICT_VERIFY=true` процесс не продолжит работу, если Telegram вернёт URL, отличный от ожидаемого.

## 11. Полезные команды для сопровождения

### Проверка статусов всех сервисов

```bash
sudo systemctl status bitrix-bot.service bitrix-telegram-mirror.service bitrix-monitor.service
```

### Проверка Telegram webhook через monitoring API

После запуска откройте `/monitor` или запросите status API и убедитесь, что `telegram_webhook.verified=true`, а `actual_url` совпадает с `expected_url`.

### Проверка внутреннего Bitrix bridge через monitoring API

В том же `/monitor` проверьте карточку Bitrix bridge:

- `reachable=true` означает, что monitoring dashboard достучался до `http://127.0.0.1:8090/health`;
- `mirror_bridge_enabled=true` означает, что основной процесс mirror-service действительно поднят в bridge-режиме;
- `verified=true` означает, что bridge включён и основной процесс отвечает корректным health payload.

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
4. NGINX проксирует `/bitrix/bot`, `/monitor` и при необходимости `/telegram/webhook`;
5. в `.env` задан `MONITOR_PASSWORD`;
6. `MIRROR_STATE_DB_PATH` указывает на один и тот же SQLite-файл для mirror и monitor;
7. в Bitrix зарегистрирован webhook URL `https://bot.example.com/bitrix/bot`;
8. `BITRIX_WEBHOOK_TOKEN` задан и совпадает с `application_token` в Bitrix;
9. HTTPS активен с TLS 1.2+ и security headers;
10. Fail2ban работает: `sudo fail2ban-client status`;
11. UFW включен: `sudo ufw status`;
12. Сервисы работают под пользователем `bitrix-bot`, а не `root`;
13. `/monitor` доступен только с разрешённых IP.
14. при включённом bridge `http://127.0.0.1:8090/health` отвечает успешно;
15. при включённом Telegram webhook внешний URL `https://bot.example.com/telegram/webhook` опубликован через nginx.

## 13. Безопасность

### Переменные безопасности в `.env`

| Переменная | Описание | По умолчанию |
|---|---|---|
| `BITRIX_WEBHOOK_TOKEN` | Токен аутентификации webhook от Bitrix | (обязательный) |
| `MIRROR_INTERNAL_WEBHOOK_SECRET` | Секрет внутреннего bridge между `server-side/app.py` и `main.py` | (обязательный для bridge) |
| `TELEGRAM_WEBHOOK_SECRET` | Secret token для Telegram webhook | (обязательный для webhook mode) |
| `MAX_FILE_SIZE_BYTES` | Макс. размер файла для пересылки | `104857600` (100 МБ) |
| `FILE_CACHE_DIR` | Каталог кэша файлов | `/opt/bitrix-bot/file_cache` |
| `FILE_CACHE_MAX_BYTES` | Макс. общий размер кэша | `10737418240` (10 ГБ) |
| `DB_CLEANUP_MAX_AGE_SECONDS` | Время жизни записей в БД | `604800` (7 дней) |
| `BITRIX_LOG_PATH` | Путь к лог-файлу webhook | `server-side/bitrix.log` |

### Проверка состояния безопасности

```bash
# Статус Fail2ban
sudo fail2ban-client status
sudo fail2ban-client status nginx-limit-req

# Статус файрвола
sudo ufw status verbose

# Проверка сервисного пользователя
ps aux | grep bitrix-bot

# Проверка SSL
curl -I https://bot.example.com/health

# Проверка webhook-аутентификации (должен вернуть 403)
curl -X POST https://bot.example.com/bitrix/bot -d '{}'
```

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
