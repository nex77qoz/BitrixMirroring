# Дизайн-спецификация: Исправление багов и оптимизация SQLite

Этот документ описывает техническое решение для исправления обнаруженных багов и повышения отказоустойчивости/производительности SQLite базы данных в проекте двустороннего зеркалирования **BitrixMirroring**.

---

## 1. Цели и задачи
- **Исправление багов:**
  1. Устранение рассинхронизации реакций Telegram → Битрикс24 (исправление хардкода `bitrix_liked_by_bot=True` при снятии реакции).
  2. Предотвращение зацикливания зеркалирования собственных сообщений бота из Битрикс24 путем проверки `bitrix_bot_id`.
  3. Корректное использование настроенного в конфигурации размера очереди (`bitrix_send_queue_maxsize`) вместо хардкода `100`.
- **Оптимизация работы с базой данных:**
  - Устранение рисков возникновения взаимных блокировок SQLite (`database is locked`) при параллельной работе Telegram-бота, FastAPI вебхука Битрикс и дашборда мониторинга за счет тюнинга подключений во всех трех процессах.

---

## 2. Детальный дизайн изменений

### Раздел 2.1: Логические исправления в `mirror_service.py`

#### А. Синхронизация реакций
В методе `sync_telegram_reaction` (строка 459) исправим сохранение состояния лайка в БД, заменив хардкод `True` на фактическое значение переменной `has_reactions`.

```diff
             await self.bitrix.set_message_like(link.bitrix_message_id, liked=has_reactions)
             await self.state_store.update_reaction_state(
                 bitrix_message_id=link.bitrix_message_id,
-                bitrix_liked_by_bot=True,
+                bitrix_liked_by_bot=has_reactions,
                 last_seen_bitrix_likes=link.last_seen_bitrix_likes,
             )
```

#### Б. Игнорирование сообщений от самого бота в Битрикс24
В методе `_should_forward_bitrix_message` (около строки 732) добавим проверку `bitrix_message.author_id` с использованием `self.settings.bitrix_bot_id`, чтобы бот никогда не зеркалировал собственные сообщения обратно в Telegram, предотвращая потенциальный бесконечный цикл.

```python
        if self.settings.bitrix_bot_id and bitrix_message.author_id == self.settings.bitrix_bot_id:
            logger.debug(
                "Ignoring Bitrix message %s from our own bot (author_id=%s)",
                bitrix_message.message_id,
                bitrix_message.author_id,
            )
            return False
```

#### В. Использование настройки размера очереди отправки
В методе `enqueue_telegram_message` (строка 336) заменим жестко закодированный размер очереди `100` на параметр из настроек `self.settings.bitrix_send_queue_maxsize`.

```diff
         chat_id = message.chat_id
         if chat_id not in self._channel_queues:
-            self._channel_queues[chat_id] = asyncio.Queue(maxsize=100)
+            self._channel_queues[chat_id] = asyncio.Queue(maxsize=self.settings.bitrix_send_queue_maxsize)
             self._channel_workers[chat_id] = asyncio.create_task(
```

---

### Раздел 2.2: Оптимизация SQLite соединений во всех процессах

Для устранения блокировок SQLite мы добавим busy timeout (30 секунд) ко всем точкам подключения и гарантируем перевод базы в WAL-режим с быстрыми коммитами (`synchronous=NORMAL`) для каждого процесса.

#### А. Хранилище состояния (`mirror_state_store.py`)
Обновим контекстный менеджер `_connect` в `mirror_state_store.py`:

```python
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            yield connection
        finally:
            connection.close()
```

#### Б. FastAPI Webhook Битрикс (`server-side/app.py`)
Оптимизируем блок генерации токена `/tg_connect` в `server-side/app.py` для использования WAL-прагм и таймаута:

```python
                try:
                    conn = sqlite3.connect(db_path, timeout=30.0)
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA synchronous=NORMAL")
                    with conn:
                        conn.execute("DELETE FROM pending_connections WHERE expires_at_unix < ?", (int(time.time()),))
                        conn.execute(
                            "INSERT INTO pending_connections (bitrix_dialog_id, token, expires_at_unix, created_at_unix) VALUES (?, ?, ?, ?)",
                            (dialog_id, token, expires_at, int(time.time())),
                        )
```

#### В. Панель мониторинга (`server-side/monitor_app.py`)
Обновим метод подключения к базе `_db_connect` в `server-side/monitor_app.py`:

```python
def _db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.OperationalError:
        pass
    return conn
```

---

## 3. План тестирования и валидации
1. **Существующие тесты:** Убедиться в успешном прохождении всех 78 существующих unittests после внесения изменений с помощью команды:
   ```bash
   .venv/bin/python -m unittest discover -s tests -v
   ```
2. **Новые тесты:** Добавить unittests в тестовые классы для проверки:
   - Бага с реакцией: проверка корректного обновления поля `bitrix_liked_by_bot` в базе при снятии лайка (`has_reactions=False`).
   - Защиты от зацикливания: проверка того, что сообщения с `author_id == bitrix_bot_id` отбрасываются методом `_should_forward_bitrix_message`.
