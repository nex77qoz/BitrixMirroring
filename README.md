# BitrixMirroring

Бот и набор сервисов для двустороннего зеркалирования сообщений между **Telegram-группами** и **Битрикс24-чатами**.

Проект решает практическую задачу: сотрудники могут общаться в привычном Telegram-чате, а сообщения, правки, реакции и часть вложений синхронизируются с соответствующим диалогом в Битрикс24. В обратную сторону сервис может работать как через polling, так и через webhook bridge для почти мгновенной доставки из Bitrix в Telegram.

## Что умеет проект

- зеркалировать сообщения **Telegram → Битрикс24**;
- зеркалировать сообщения **Битрикс24 → Telegram**;
- поддерживать **несколько связок чатов** одновременно;
- синхронизировать **редактирование сообщений** из Telegram в Битрикс;
- синхронизировать **реакции Telegram** в лайки Битрикс;
- передавать **вложения и изображения** между системами;
- хранить служебное состояние в **SQLite** без сохранения полного текста переписки;
- автоматически подавлять циклы и повторную доставку уже отражённых сообщений;
- управлять маппингами чатов через **веб-панель мониторинга**;
- отдельно запускать **webhook-обработчик Битрикс** и **dashboard мониторинга**.

## Состав репозитория

- `main.py` — точка входа основного Telegram-бота, который принимает апдейты через polling и запускает сервис зеркалирования.
- `mirror_service.py` — основная логика двусторонней синхронизации, очереди отправки, polling Битрикс и подавления дублей.
- `bitrix_client.py` — клиент для вызова Битрикс REST API, отправки сообщений, обновлений, лайков и загрузки файлов.
- `handlers.py` — обработчики Telegram-команд, обычных сообщений, редактирований и реакций.
- `mirror_state_store.py` — SQLite-хранилище курсоров, связей между сообщениями и состояния реакций.
- `settings.py` — загрузка и валидация конфигурации из переменных окружения.
- `server-side/app.py` — FastAPI webhook для событий Битрикс-бота.
- `server-side/monitor_app.py` — административная web UI для мониторинга и редактирования маппингов.
- `DEPLOYMENT.md` — подробная инструкция по установке на сервер с `systemd` и `nginx`.

## Архитектура

В типовой схеме используются три независимых процесса:

1. **Mirror service** — основной процесс, который:
  - получает апдейты Telegram через polling или webhook;
   - отправляет новые сообщения в Битрикс;
  - периодически опрашивает Битрикс и возвращает новые сообщения в Telegram;
  - при включённом bridge принимает внутренние Bitrix webhook-события и немедленно запускает sync для нужного dialog_id.
2. **Bitrix webhook service** — HTTP endpoint для событий Битрикс-бота.
3. **Monitoring dashboard** — web-интерфейс для просмотра состояния сервиса, логов и управления связкой чатов.

## Как это работает

### Telegram → Битрикс

Когда в разрешённой Telegram-группе приходит сообщение, бот:

1. проверяет, что чат разрешён в конфигурации;
2. отбрасывает служебные сообщения и сообщения других ботов;
3. формирует текст с метаданными:
   - название чата;
   - отправитель;
   - при необходимости — тема форума и информация об ответе;
4. отправляет сообщение или файл в соответствующий диалог Битрикс;
5. сохраняет связь между Telegram `message_id` и Битрикс `message_id` в SQLite.

### Битрикс → Telegram

Для каждого настроенного `bitrix_dialog_id` запускается отдельный polling loop:

1. сервис читает последний обработанный курсор;
2. забирает новые сообщения из Битрикс;
3. отфильтровывает служебные и уже зеркалированные сообщения;
4. пересылает текст или вложение в Telegram;
5. обновляет курсор и таблицу связей.

Если включён `BITRIX_WEBHOOK_BRIDGE_ENABLED=true`, входящий Bitrix webhook из `server-side/app.py` дополнительно будит синхронизацию немедленно через внутренний endpoint main-процесса. Это позволяет убрать типичную задержку в несколько секунд, не отказываясь от polling как от fallback-механизма.

### Редактирования и реакции

- Если пользователь редактирует сообщение в Telegram, сервис ищет связанную запись и обновляет исходное сообщение в Битрикс.
- Если пользователь добавляет/снимает реакцию в Telegram, сервис синхронизирует это как лайк/снятие лайка в Битрикс.
- Удаление сообщений **Telegram → Битрикс** универсально не поддерживается, потому что Telegram Bot API не даёт надёжного delete update в обычном polling-режиме.

## Требования

- Python **3.11+** рекомендуется;
- доступ к **Telegram Bot API**;
- доступ к **Битрикс24 REST API** через входящий webhook;
- для production-развёртывания: Linux-сервер, `systemd`, `nginx` и при необходимости TLS.

## Установка и быстрый старт

### Автоматическая установка на сервер (рекомендуется)

Склонируйте репозиторий и запустите установщик — он сам определит URL и ветку репозитория:

```bash
git clone https://github.com/nex77qoz/BitrixMirroring.git
cd BitrixMirroring
sudo bash install.sh
```

Скрипт интерактивно соберёт конфигурацию, установит python-зависимости, настроит systemd-сервисы, nginx и SSL-сертификат (Let's Encrypt).

Для обновления уже установленного бота достаточно выполнить `git pull` в установленном каталоге через флаг `--update`:

```bash
sudo bash /opt/bitrix-bot/install.sh --update
```

Удаление:

```bash
sudo bash /opt/bitrix-bot/install.sh --uninstall
```

Подробная инструкция по серверному развёртыванию: [`DEPLOYMENT.md`](./DEPLOYMENT.md).

---

### Ручная установка (локальная разработка)

#### 1. Клонирование репозитория

```bash
git clone https://github.com/nex77qoz/BitrixMirroring.git
cd BitrixMirroring
```

#### 2. Создание виртуального окружения

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

#### 3. Настройка переменных окружения

Скопируйте шаблон:

```bash
cp env.example .env
```

Минимально нужно заполнить:

- `TELEGRAM_BOT_TOKEN`
- `BITRIX_WEBHOOK_BASE`
- хотя бы одну связку чатов:
  - `CHAT_MAPPING_1`, `CHAT_MAPPING_2`, ...
  - или `CHAT_MAPPINGS`
  - или legacy-пару `BITRIX_DIALOG_ID` + `ALLOWED_TELEGRAM_CHAT_ID`

Если хотите отправлять сообщения в Битрикс **от имени зарегистрированного чат-бота**, дополнительно настройте:

- `BITRIX_BOT_CLIENT_ID`
- при необходимости `BITRIX_BOT_ID`

#### 4. Запуск основного mirror-сервиса

```bash
python main.py
```

#### 5. Проверка Telegram-команд

После запуска бот отвечает на:

- `/start` — проверка, что бот запущен;
- `/whereami` — показывает `chat_id`, тип чата, название и `message_thread_id`.

## Запуск дополнительных сервисов

### Битрикс webhook сервис

```bash
uvicorn server-side.app:app --host 127.0.0.1 --port 8081
```

Доступные маршруты:

- `GET /health`
- `POST /bitrix/bot`

Этот сервис полезен, если вы используете Bitrix bot events и хотите принимать webhook-события отдельно от основного polling-процесса. При настройке `MIRROR_INTERNAL_BASE_URL` и `MIRROR_INTERNAL_WEBHOOK_SECRET` он может не просто логировать события, а сразу пробрасывать их в основной mirror-service.

### Дэшборд мониторинг

```bash
uvicorn server-side.monitor_app:app --host 127.0.0.1 --port 8082
```

Для dashboard важно задать пароль:

```bash
export MONITOR_PASSWORD='change-me'
```

Дополнительно можно задать:

- `MONITOR_USERNAME` — логин для Basic Auth, по умолчанию `admin`;
- `MIRROR_STATE_DB_PATH` — путь к общей SQLite-базе состояния.

## Основные переменные окружения

Ниже перечислены самые важные настройки. Полный шаблон смотрите в `env.example`.

### Обязательные

- `TELEGRAM_BOT_TOKEN` — токен Telegram-бота.
- `BITRIX_WEBHOOK_BASE` — базовый URL входящего Битрикс webhook.
- одна из схем маппинга чатов:
  - `CHAT_MAPPING_1=-1001234567890:chat2941`
  - `CHAT_MAPPING_2=-1001234567891:chat2942`
  - или `CHAT_MAPPINGS=[...]`

### Отправка в Битрикс от имени чат-бота

- `BITRIX_BOT_ID`
- `BITRIX_BOT_CLIENT_ID`

### Форматирование сообщений

- `PREFIX_WITH_CHAT_TITLE=true`
- `PREFIX_WITH_SENDER=true`
- `PREFIX_WITH_TIMESTAMP=true`
- `BITRIX_DISABLE_LINK_PREVIEW=true`

### Направления синхронизации

- `SYNC_TELEGRAM_TO_BITRIX=true`
- `SYNC_BITRIX_TO_TELEGRAM=true`

### Сеть и таймауты

- `REQUEST_TIMEOUT_SECONDS=20`
- `ENABLE_SOCKS5_PROXY=false`
- `SOCKS5_PROXY_URL=`

### Polling и устойчивость к ошибкам

- `BITRIX_POLL_INTERVAL_SECONDS=5`
- `BITRIX_RETRY_ATTEMPTS=4`
- `BITRIX_RETRY_BASE_DELAY_SECONDS=1`
- `BITRIX_RETRY_MAX_DELAY_SECONDS=15`
- `BITRIX_POLL_ERROR_BACKOFF_SECONDS=2`
- `BITRIX_POLL_MAX_BACKOFF_SECONDS=30`

### Webhook bridge и Telegram webhook

- `BITRIX_WEBHOOK_BRIDGE_ENABLED=true` — включает внутренний bridge Bitrix webhook -> main.py.
- `MIRROR_HTTP_HOST=127.0.0.1`
- `MIRROR_HTTP_PORT=8090`
- `MIRROR_INTERNAL_EVENT_PATH=/internal/bitrix/event`
- `MIRROR_INTERNAL_WEBHOOK_SECRET=...`
- `TELEGRAM_WEBHOOK_ENABLED=true` — переводит Telegram-бот на webhook вместо polling.
- `TELEGRAM_WEBHOOK_PUBLIC_URL=https://bot.example.com`
- `TELEGRAM_WEBHOOK_PATH=/telegram/webhook`
- `TELEGRAM_WEBHOOK_SECRET=...`
- `TELEGRAM_WEBHOOK_DROP_PENDING_UPDATES=true`
- `TELEGRAM_WEBHOOK_STRICT_VERIFY=true` — после `setWebhook` бот сверяет реальный `getWebhookInfo().url` с ожидаемым URL и поднимает ошибку при mismatch.

Monitoring dashboard также показывает:

- отдельную карточку Telegram webhook: expected URL, actual URL, pending updates и последнюю ошибку Telegram API;
- отдельную карточку Bitrix bridge: доступность main health endpoint, целевой internal event URL и факт того, что bridge действительно включён в основном mirror-процессе.

### Производительность

- `BITRIX_USER_CACHE_TTL_SECONDS=300`
- `BITRIX_MAX_CONCURRENT_REQUESTS=5`
- `BITRIX_SEND_QUEUE_MAXSIZE=1000`
- `BITRIX_SEND_WORKERS=2`
- `BITRIX_RESCAN_RECENT_MESSAGES_LIMIT=100`

### Состояние и база

- `BITRIX_CURSOR_STATE_PATH=bitrix_cursor_state.json`
- `MIRROR_STATE_DB_PATH=mirror_state.sqlite3`

## Настройка маппингов чатов

Поддерживаются три режима конфигурации:

### Вариант 1 — рекомендуемый: `CHAT_MAPPING_N`

```env
CHAT_MAPPING_1=-1001234567890:chat2941
CHAT_MAPPING_2=-1001234567891:chat2942
```

### Вариант 2 — JSON-массив

```env
CHAT_MAPPINGS=[{"tg_chat_id": -1001234567890, "bitrix_dialog_id": "chat2941"}]
```

### Вариант 3 — legacy single mapping

```env
BITRIX_DIALOG_ID=chat2941
ALLOWED_TELEGRAM_CHAT_ID=-1001234567890
```

Кроме переменных окружения, дополнительные mappings могут храниться в SQLite и редактироваться через monitoring dashboard.

## Где хранятся данные

Проект хранит служебное состояние в SQLite-файле `MIRROR_STATE_DB_PATH`, включая:

- связи между сообщениями Telegram и Битрикс;
- курсоры последнего обработанного `message_id` Bitrix;
- состояние реакций/лайков;
- дополнительные mappings чатов, добавленные через dashboard.

Это позволяет:

- корректно зеркалировать редактирования;
- избегать дублей;
- восстанавливаться после перезапуска;
- не держать полные тексты сообщений в собственной базе как обязательный источник данных.

## Ограничения и особенности

- Поддерживаются только **группы** и **супергруппы** Telegram.
- Сообщения от Telegram-ботов игнорируются, чтобы избежать зацикливания.
- Часть служебных Telegram-событий не зеркалируется.
- Для Битрикс → Telegram используется polling, поэтому доставка не мгновенная, а с интервалом `BITRIX_POLL_INTERVAL_SECONDS`.
- При большом количестве чатов важно корректно настроить `BITRIX_MAX_CONCURRENT_REQUESTS` и число воркеров.

## Production deployment

Для серверной установки, `systemd` unit-файлов, настройки `nginx`, TLS и webhook URL используйте отдельную инструкцию:

- [`DEPLOYMENT.md`](./DEPLOYMENT.md)

В каталоге `server-side/` уже лежат готовые шаблоны для production:

- `bitrix-bot.service`
- `bitrix-telegram-mirror.service`
- `bitrix-monitor.service`
- пример env-файла
- конфигурация `nginx`

## Диагностика и отладка

Полезные действия при первом запуске:

1. отправьте `/whereami` в нужный Telegram-чат и проверьте `chat_id`;
2. убедитесь, что `bitrix_dialog_id` указан в корректном формате (`chat2941`, `sg123` и т. п.);
3. проверьте, что SQLite-файл создаётся и доступен на запись;
4. временно установите `LOG_LEVEL=DEBUG`, если нужна подробная трассировка;
5. проверьте доступность Битрикс webhook и корректность прав у входящего webhook.

## Команды разработки

Установка зависимостей:

```bash
pip install -r requirements.txt
```

Запуск основного сервиса:

```bash
python main.py
```

Запуск webhook:

```bash
uvicorn server-side.app:app --reload --port 8081
```

Запуск dashboard:

```bash
uvicorn server-side.monitor_app:app --reload --port 8082
```

## Tests

The repository includes a `unittest` test suite in `tests/`.

Run all tests:

```bash
python -m unittest discover -s tests -v
```

Run a single module:

```bash
python -m unittest tests.test_main_http -v
```
