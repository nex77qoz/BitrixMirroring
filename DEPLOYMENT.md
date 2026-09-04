# Развёртывание BitrixMirroring

Проект использует Bitrix Chatbot API 2.0 в режиме `supervisor + fetch` через
**Vibe API (бот-платформу Битрикс24)** — `https://vibecode.bitrix24.tech/v1`.
Bitrix-события получает основной процесс через `GET /v1/bots/:botId/events`;
отдельный Bitrix webhook-сервис не используется. Токен чат-бота платформа
хранит сама — на стороне сервиса нужен только API-ключ.

## Сервисы

- `bitrix-telegram-mirror.service` — Telegram и Bitrix mirror.
- `bitrix-monitor.service` — monitoring dashboard.
- NGINX — внешний HTTPS reverse proxy для Telegram webhook и `/monitor`.

`server-side/app.py` и `server-side/bitrix-bot.service` удалены намеренно.

## Конфигурация

В `.env` обязательны:

```dotenv
TELEGRAM_BOT_TOKEN=...
VIBE_API_KEY=vibe_api_...
VIBE_BASE_URL=https://vibecode.bitrix24.tech/v1
BITRIX_BOT_ID=123456
```

`VIBE_API_KEY` — личный ключ с https://vibecode.bitrix24.tech/keys: режим
«чтение+запись», скоупы `imbot, disk`, владелец — пользователь с правом
администратора портала (регистрация бота выполняется от его лица).

`BITRIX_BOT_ID` — числовой ID бота, выданный `POST /v1/bots`. Бот привязан к
API-ключу, которым зарегистрирован; запросы с другого ключа получают
`403 BOT_ACCESS_DENIED`.

Для Telegram webhook дополнительно задаются `TELEGRAM_WEBHOOK_ENABLED=true`,
`TELEGRAM_WEBHOOK_PUBLIC_URL`, `TELEGRAM_WEBHOOK_PATH` и
`TELEGRAM_WEBHOOK_SECRET`. При `false` Telegram работает через polling.

## Установка

```bash
git clone https://github.com/nex77qoz/BitrixMirroring.git
cd BitrixMirroring
sudo bash install.sh
```

Installer автоматически:

1. проверяет переданный `BITRIX_BOT_ID` через `GET /v1/bots/:botId`;
2. ищет бота с кодом `tg_mirror_bot_v2` под этим ключом (`GET /v1/bots`) и,
   если не найден, регистрирует нового через `POST /v1/bots` с
   `type=supervisor` и `eventMode=fetch`;
3. сохраняет `BITRIX_BOT_ID` (и `VIBE_API_KEY`, `VIBE_BASE_URL`) в `.env`;
4. выводит `bot_id` в терминал и отправляет администраторам Telegram (без
   токена — платформа его не отдаёт);
5. устанавливает только два systemd-сервиса.

После установки добавьте нового бота-супервизора во все зеркалируемые чаты
Битрикс24 и перезапустите сервис.

## Ручная регистрация

Скриптом (формат stdout совместим с installer):

```bash
python3 server-side/register_bot.py "$VIBE_API_KEY" "" "Telegram Mirror"
```

или напрямую запросом к Vibe API:

```bash
curl -X POST https://vibecode.bitrix24.tech/v1/bots \
  -H "X-Api-Key: $VIBE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"code": "tg_mirror_bot_v2", "name": "Telegram Mirror", "type": "supervisor", "eventMode": "fetch"}'
```

Сохраните `data.botId` в `BITRIX_BOT_ID` в `.env`. Повторный вызов с тем же
кодом идемпотентен (`409 BOT_ALREADY_EXISTS` с `data.botId`).

## Лимиты файлов

Загрузка файла в Bitrix24 идёт через `POST /v1/bots/:botId/files` (base64 в
теле). Потолок тела запроса — 40 МиБ, то есть исходный файл ≈ до 30 МиБ
(`BITRIX_MAX_UPLOAD_FILE_BYTES=31457280` по умолчанию). Файлы больше лимита
сервис пересылает текстом с пометкой. Rate limit ключа — 10 запросов/с.

## Обновление и удаление

```bash
sudo bash /opt/bitrix-bot/install.sh --update
sudo bash /opt/bitrix-bot/install.sh --uninstall
```

Перед удалением installer вызывает `DELETE /v1/bots/:botId` с заголовком
`X-Api-Key`, затем удаляет локальные сервисы и файлы. Бот удаляется и из
Битрикс24, и из базы Вайбкод; повторный вызов безопасен.

## Проверка

```bash
sudo systemctl status bitrix-telegram-mirror.service bitrix-monitor.service
journalctl -u bitrix-telegram-mirror.service -f
```

В логах должен быть активен fetch-цикл. Legacy-методы `imbot.bot.*`,
`imbot.unregister`, `ONIMBOTMESSAGEADD` и `ONIMBOTJOINCHAT` проектом не
используются.
