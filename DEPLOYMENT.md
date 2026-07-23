# Развёртывание BitrixMirroring

Проект использует только Bitrix Chatbot API 2.0 в режиме `supervisor + fetch`.
Bitrix-события получает основной процесс через `imbot.v2.Event.get`; отдельный
Bitrix webhook-сервис не используется.

## Сервисы

- `bitrix-telegram-mirror.service` — Telegram и Bitrix mirror.
- `bitrix-monitor.service` — monitoring dashboard.
- NGINX — внешний HTTPS reverse proxy для Telegram webhook и `/monitor`.

`server-side/app.py` и `server-side/bitrix-bot.service` удалены намеренно.

## Конфигурация

В `.env` обязательны:

```dotenv
TELEGRAM_BOT_TOKEN=...
BITRIX_WEBHOOK_BASE=https://company.bitrix24.ru/rest/1/webhook
BITRIX_BOT_ID=123456
BITRIX_BOT_CLIENT_ID=bot-token-from-imbot.v2.Bot.register
```

`BITRIX_BOT_CLIENT_ID` — это именно `botToken` Bitrix-бота, а не токен Telegram
и не OAuth client ID.

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

1. проверяет существующий v2-бот через `imbot.v2.Event.get`;
2. регистрирует новый через `imbot.v2.Bot.register` с `type=supervisor` и
   `eventMode=fetch`, если старый бот не найден;
3. сохраняет `BITRIX_BOT_ID` и `BITRIX_BOT_CLIENT_ID` в `.env`;
4. выводит эти значения в терминал и отправляет их администраторам Telegram;
5. устанавливает только два systemd-сервиса.

## Ручная регистрация

```json
{
  "fields": {
    "code": "tg_mirror_bot",
    "botToken": "generate-a-unique-token",
    "type": "supervisor",
    "eventMode": "fetch",
    "isHidden": false,
    "properties": {
      "name": "Telegram Mirror",
      "desc": "Зеркалирование чатов между Telegram и Битрикс24"
    }
  }
}
```

После ответа Bitrix сохраните ID и тот же `botToken` в `.env`.

## Обновление и удаление

```bash
sudo bash /opt/bitrix-bot/install.sh --update
sudo bash /opt/bitrix-bot/install.sh --uninstall
```

Перед удалением installer вызывает `imbot.v2.Bot.unregister` с `botId` и тем
же `botToken`, затем удаляет локальные сервисы и файлы.

## Проверка

```bash
sudo systemctl status bitrix-telegram-mirror.service bitrix-monitor.service
journalctl -u bitrix-telegram-mirror.service -f
```

В логах должен быть активен fetch-цикл. Legacy-методы `imbot.bot.*`,
`imbot.unregister`, `ONIMBOTMESSAGEADD` и `ONIMBOTJOINCHAT` проектом не
используются.
