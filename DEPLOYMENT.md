# Развёртывание BitrixMirroring

Проект использует Bitrix Chatbot API 2.0 (`imbot.v2.*`) в режиме
`supervisor + fetch` напрямую через REST коробочного Битрикс24 и **входящий
вебхук** администратора. Сторонние шлюзы (Vibe) не используются.
События бота основной процесс забирает через `imbot.v2.Event.get`;
отдельный Bitrix webhook-сервис не нужен.

## Топология: бот на отдельном VPS

Бот работает на VPS, где доступен Telegram, а Битрикс24 (коробка) стоит на
другом сервере. Все обращения к порталу идут через публичный HTTPS-адрес
(`BITRIX_WEBHOOK_URL=https://портал/rest/<id>/<код>/`; REST по `http` отвечает
`INVALID_REQUEST: Https required`):

- с VPS должен быть открыт исходящий доступ к порталу по 443; на портале
  `/rest/` не должен блокироваться WAF/allowlist для IP VPS;
- прокси (`ENABLE_SOCKS5_PROXY`) нужен только Telegram-части; Bitrix-запросы
  ходят напрямую, держите `ENABLE_SOCKS5_PROXY=false`, если Telegram с VPS доступен;
- события Bitrix → Telegram приходят **опросом** (`imbot.v2.Event.get`, fetch):
  это не требует входящих соединений на VPS и не зависит от облака Битрикс;
- ссылки на скачивание файлов (`/rest/download.json?token=…`) одноразовые и
  ведут на тот же портал; сервис запрашивает свежую ссылку на каждую попытку;
- секреты (`BITRIX_WEBHOOK_URL`, `BITRIX_BOT_TOKEN`) хранятся только в `.env` на VPS.

## Сервисы

- `bitrix-telegram-mirror.service` — Telegram и Bitrix mirror.
- `bitrix-monitor.service` — monitoring dashboard.
- NGINX — внешний HTTPS reverse proxy для Telegram webhook и `/monitor`.

`server-side/app.py` и `server-side/bitrix-bot.service` удалены намеренно.

## Конфигурация

В `.env` обязательны:

```dotenv
TELEGRAM_BOT_TOKEN=...
BITRIX_WEBHOOK_URL=https://portal.example.com/rest/1/abcdef0123456789/
BITRIX_BOT_TOKEN=...
BITRIX_BOT_ID=123
```

`BITRIX_WEBHOOK_URL` — входящий вебхук администратора портала
(«Разработчикам → Другое → Входящий вебхук») со скоупами `imbot`, `im`, `disk`.

`BITRIX_BOT_TOKEN` — секрет владения ботом: бот привязан к токену, с которым
зарегистрирован (`APP_ID = custom<token>`); с другим токеном API вернёт
`BOT_OWNERSHIP_ERROR`. Токен генерирует `register_bot.py`, его нужно сохранить.

`BITRIX_BOT_ID` — числовой ID бота из `imbot.v2.Bot.register`.

Дополнительно: `BITRIX_VERIFY_SSL=false` для самоподписанного сертификата,
`BITRIX_MIN_REQUEST_INTERVAL_SECONDS` (по умолчанию `0` — у коробки нет
облачного лимита 2 запроса/с).

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

1. проверяет вебхук (`scope`) и переданный `BITRIX_BOT_ID` (`imbot.v2.Bot.get`);
2. ищет бота с кодом `tg_mirror_bot_v2` под этим токеном (`imbot.v2.Bot.list`) и,
   если не найден, регистрирует нового через `imbot.v2.Bot.register` с
   `type=supervisor` и `eventMode=fetch`;
3. сохраняет `BITRIX_BOT_ID`, `BITRIX_BOT_TOKEN`, `BITRIX_WEBHOOK_URL` в `.env`;
4. выводит `bot_id` в терминал и отправляет администраторам Telegram;
5. устанавливает только два systemd-сервиса.

После установки добавьте нового бота-супервизора во все зеркалируемые чаты
Битрикс24 и перезапустите сервис.

## Ручная регистрация

Скриптом (формат stdout совместим с installer):

```bash
python3 server-side/register_bot.py "$BITRIX_WEBHOOK_URL" "" "Telegram Mirror V2"
```

Для повторного запуска с уже существующим ботом передайте `BITRIX_BOT_TOKEN`
в окружении. Скрипт печатает `bot_id` и `bot_token` — сохраните оба в `.env`.

До создания бота скрипт проверяет скоупы и сохраняет токен в файл восстановления
с правами `600`. Installer использует `/opt/bitrix-bot.credentials/registration.json`;
при ручном запуске файл находится в `~/.local/state/bitrix-mirror/` (путь выводится
в stderr). Повторный запуск с тем же вебхуком восстанавливает токен из этого файла.
Путь можно задать через `BITRIX_BOT_CREDENTIALS_FILE`. При ошибке автонастройки
installer останавливается; после устранения причины запустите его снова.
Если скрипт регистрации отсутствует, потребуется вручную указать и ID, и токен.

Для портала с самоподписанным сертификатом передайте `BITRIX_VERIFY_SSL=false`
в окружении скрипта регистрации или конфигурации installer. Installer сохраняет
это значение в `.env` для рабочего клиента.

## Лимиты файлов

Загрузка файла в Bitrix24 идёт через `imbot.v2.File.upload` (base64 в теле).
Лимит определяется `post_max_size`/`upload_max_filesize` PHP портала и
`BITRIX_MAX_UPLOAD_FILE_BYTES` (по умолчанию 30 МиБ). Файлы больше лимита
сервис пересылает текстом с пометкой.

## Обновление и удаление

```bash
sudo bash /opt/bitrix-bot/install.sh --update
sudo bash /opt/bitrix-bot/install.sh --uninstall
```

Перед удалением installer вызывает `imbot.v2.Bot.unregister`, затем удаляет
локальные сервисы и файлы; повторный вызов безопасен.
Если выбрано сохранение бота в Bitrix24, installer сначала экспортирует URL,
ID, токен и настройку TLS в `/opt/bitrix-bot.credentials/retained-bot.*.env`
с правами `600`. Каталог доступен только владельцу и остаётся после удаления.
При ошибке экспорта удаление останавливается. Сохраните этот файл для переустановки.

## Проверка

```bash
sudo systemctl status bitrix-telegram-mirror.service bitrix-monitor.service
journalctl -u bitrix-telegram-mirror.service -f
```

В логах должен быть активен fetch-цикл. Legacy-методы `imbot.bot.*`,
`imbot.unregister`, `ONIMBOTMESSAGEADD` и `ONIMBOTJOINCHAT` проектом не
используются.
