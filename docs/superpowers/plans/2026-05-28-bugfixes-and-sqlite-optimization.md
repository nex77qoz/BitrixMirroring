# План реализации: Исправление багов и оптимизация SQLite

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Устранить обнаруженные логические баги в зеркалировании (реакции, зацикливание бота, размер очереди) и оптимизировать параллельную работу с SQLite базой данных во всех сервисах.

**Architecture:** Исправления вносятся точечно в `mirror_service.py` с покрытием юнит-тестами. Оптимизация SQLite достигается настройкой `timeout=30.0`, `journal_mode=WAL` и `synchronous=NORMAL` для каждого открываемого соединения во всех процессах.

**Tech Stack:** Python 3.11+, python-telegram-bot, sqlite3, unittest, FastAPI

---

### Task 1: Исправление бага с рассинхронизацией реакций Telegram → Битрикс

**Files:**
- Modify: `mirror_service.py`
- Test: `tests/test_mirror_service.py`

- [ ] **Step 1: Написать падающий юнит-тест**
  Добавить в класс `MirrorServiceTestCase` в файле `tests/test_mirror_service.py` новый тест `test_sync_telegram_reaction_removal_updates_state_correctly`, который проверяет, что при снятии реакции (`has_reactions=False`) в метод `update_reaction_state` передается `bitrix_liked_by_bot=False`, а не `True`:
  ```python
    async def test_sync_telegram_reaction_removal_updates_state_correctly(self) -> None:
        self.state_store.get_link_by_telegram_message.return_value = SimpleNamespace(
            bitrix_message_id=99,
            bitrix_liked_by_bot=True,
            last_seen_bitrix_likes="123",
        )
        await self.service.sync_telegram_reaction(-1001234567890, 100, False)
        self.bitrix.set_message_like.assert_awaited_once_with(99, liked=False)
        self.state_store.update_reaction_state.assert_awaited_once_with(
            bitrix_message_id=99,
            bitrix_liked_by_bot=False,
            last_seen_bitrix_likes="123",
        )
  ```

- [ ] **Step 2: Запустить тесты для подтверждения падения**
  Выполнить:
  ```bash
  .venv/bin/python -m unittest tests.test_mirror_service.MirrorServiceTestCase.test_sync_telegram_reaction_removal_updates_state_correctly -v
  ```
  Expected: FAIL (AssertionError: Expected False but got True)

- [ ] **Step 3: Написать минимальную реализацию**
  В файле `mirror_service.py` исправить хардкод `True` в вызове `update_reaction_state` в методе `sync_telegram_reaction` (строка 485+):
  ```python
            await self.bitrix.set_message_like(link.bitrix_message_id, liked=has_reactions)
            await self.state_store.update_reaction_state(
                bitrix_message_id=link.bitrix_message_id,
                bitrix_liked_by_bot=has_reactions,
                last_seen_bitrix_likes=link.last_seen_bitrix_likes,
            )
  ```

- [ ] **Step 4: Проверить успешное прохождение теста**
  Выполнить ту же команду:
  ```bash
  .venv/bin/python -m unittest tests.test_mirror_service.MirrorServiceTestCase.test_sync_telegram_reaction_removal_updates_state_correctly -v
  ```
  Expected: PASS

- [ ] **Step 5: Сделать коммит**
  ```bash
  git add mirror_service.py tests/test_mirror_service.py
  git commit -m "fix(mirror): correct reaction removal state sync to bitrix"
  ```

---

### Task 2: Защита от зацикливания сообщений самого бота в Битрикс

**Files:**
- Modify: `mirror_service.py`
- Test: `tests/test_mirror_service.py`

- [ ] **Step 1: Написать падающий юнит-тест**
  Добавить тест в `tests/test_mirror_service.py`, эмулирующий входящее сообщение из Битрикс, у которого `author_id` равен настроенному `bitrix_bot_id`. Метод `_should_forward_bitrix_message` должен вернуть `False`:
  ```python
    async def test_should_forward_bitrix_message_ignores_own_bot(self) -> None:
        # Устанавливаем bot_id в настройках сервиса
        self.service.settings = dataclasses.replace(self.service.settings, bitrix_bot_id=999)
        msg_from_bot = BitrixMessage(
            message_id=15,
            author_id=999,  # тот же ID
            text="hello from bot",
            file_ids=(),
            update_time_unix=None,
            reply_id=None,
            is_sticker=False,
            is_meeting=False,
            is_task=False,
            revision="rev1",
        )
        should_forward = await self.service._should_forward_bitrix_message("chat42", msg_from_bot)
        self.assertFalse(should_forward)
  ```
  *Примечание:* Импортируйте `dataclasses` в начало `tests/test_mirror_service.py` если он не импортирован.

- [ ] **Step 2: Запустить тест для подтверждения падения**
  Выполнить:
  ```bash
  .venv/bin/python -m unittest tests.test_mirror_service.MirrorServiceTestCase.test_should_forward_bitrix_message_ignores_own_bot -v
  ```
  Expected: FAIL или TypeError / AttributeError (так как метод вернет True)

- [ ] **Step 3: Написать минимальную реализацию**
  В файле `mirror_service.py` в начале метода `_should_forward_bitrix_message` (строка 732) добавить проверку:
  ```python
        if self.settings.bitrix_bot_id and bitrix_message.author_id == self.settings.bitrix_bot_id:
            logger.debug(
                "Ignoring Bitrix message %s from our own bot (author_id=%s)",
                bitrix_message.message_id,
                bitrix_message.author_id,
            )
            return False
```

- [ ] **Step 4: Проверить успешное прохождение теста**
  Выполнить:
  ```bash
  .venv/bin/python -m unittest tests.test_mirror_service.MirrorServiceTestCase.test_should_forward_bitrix_message_ignores_own_bot -v
  ```
  Expected: PASS

- [ ] **Step 5: Сделать коммит**
  ```bash
  git add mirror_service.py tests/test_mirror_service.py
  git commit -m "fix(mirror): prevent feedback loops by ignoring bot's own bitrix messages"
  ```

---

### Task 3: Использование настройки `bitrix_send_queue_maxsize`

**Files:**
- Modify: `mirror_service.py`
- Test: `tests/test_mirror_service.py`

- [ ] **Step 1: Написать падающий юнит-тест**
  Добавить тест в `tests/test_mirror_service.py`, который проверяет, что размер созданной очереди `asyncio.Queue` соответствует `settings.bitrix_send_queue_maxsize` (например, 555), а не жестко закодированному значению 100:
  ```python
    async def test_enqueue_telegram_message_uses_configured_queue_maxsize(self) -> None:
        self.service.settings = dataclasses.replace(self.service.settings, bitrix_send_queue_maxsize=555)
        self.service._forwarding_enabled = True
        
        message = make_message(text="test queue size")
        await self.service.enqueue_telegram_message(message)
        
        # Получаем созданную очередь для чата
        queue = self.service._channel_queues[message.chat_id]
        self.assertEqual(queue.maxsize, 555)
        
        # Очищаем воркера для предотвращения утечки задач в тестах
        if message.chat_id in self.service._channel_workers:
            self.service._channel_workers[message.chat_id].cancel()
  ```

- [ ] **Step 2: Запустить тест для подтверждения падения**
  Выполнить:
  ```bash
  .venv/bin/python -m unittest tests.test_mirror_service.MirrorServiceTestCase.test_enqueue_telegram_message_uses_configured_queue_maxsize -v
  ```
  Expected: FAIL (AssertionError: 100 != 555)

- [ ] **Step 3: Написать минимальную реализацию**
  В файле `mirror_service.py` в методе `enqueue_telegram_message` заменить `maxsize=100` на `self.settings.bitrix_send_queue_maxsize`:
  ```python
        chat_id = message.chat_id
        if chat_id not in self._channel_queues:
            self._channel_queues[chat_id] = asyncio.Queue(maxsize=self.settings.bitrix_send_queue_maxsize)
            self._channel_workers[chat_id] = asyncio.create_task(
                self._per_channel_worker(chat_id),
                name=f"channel-worker-{chat_id}"
            )
  ```

- [ ] **Step 4: Проверить успешное прохождение теста**
  Выполнить:
  ```bash
  .venv/bin/python -m unittest tests.test_mirror_service.MirrorServiceTestCase.test_enqueue_telegram_message_uses_configured_queue_maxsize -v
  ```
  Expected: PASS

- [ ] **Step 5: Сделать коммит**
  ```bash
  git add mirror_service.py tests/test_mirror_service.py
  git commit -m "fix(mirror): respect configured bitrix_send_queue_maxsize"
  ```

---

### Task 4: Оптимизация SQLite в `mirror_state_store.py`

**Files:**
- Modify: `mirror_state_store.py`

- [ ] **Step 1: Обновить метод `_connect`**
  Модифицировать контекстный менеджер `_connect` в конце файла `mirror_state_store.py` (около строки 698), добавив `timeout=30.0` при подключении к SQLite, а также установку PRAGMA-настроек WAL-режима и нормальной синхронизации:
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

- [ ] **Step 2: Запустить полный набор тестов для подтверждения корректности**
  Выполнить:
  ```bash
  .venv/bin/python -m unittest discover -s tests -v
  ```
  Expected: Все 78+ тестов успешно выполняются (PASS).

- [ ] **Step 3: Сделать коммит**
  ```bash
  git add mirror_state_store.py
  git commit -m "perf(db): configure sqlite busy timeout, WAL mode and synchronous normal"
  ```

---

### Task 5: Оптимизация SQLite в FastAPI Webhook (`server-side/app.py`)

**Files:**
- Modify: `server-side/app.py`

- [ ] **Step 1: Обновить создание подключения SQLite**
  В файле `server-side/app.py` найти создание подключения `sqlite3.connect(db_path, timeout=10)` (около строки 308) и изменить его, добавив PRAGMA-настройки:
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

- [ ] **Step 2: Запустить тесты веб-сервера**
  Выполнить:
  ```bash
  .venv/bin/python -m unittest tests.test_server_app -v
  ```
  Expected: PASS

- [ ] **Step 3: Сделать коммит**
  ```bash
  git add server-side/app.py
  git commit -m "perf(webhook): add sqlite timeout and pragmas in app.py"
  ```

---

### Task 6: Оптимизация SQLite в Дашборде (`server-side/monitor_app.py`)

**Files:**
- Modify: `server-side/monitor_app.py`

- [ ] **Step 1: Обновить метод `_db_connect`**
  Модифицировать метод `_db_connect` в `server-side/monitor_app.py` (около строки 102):
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

- [ ] **Step 2: Запустить полный тестовый набор**
  Выполнить:
  ```bash
  .venv/bin/python -m unittest discover -s tests -v
  ```
  Expected: PASS (все тесты проходят без ошибок)

- [ ] **Step 3: Сделать коммит**
  ```bash
  git add server-side/monitor_app.py
  git commit -m "perf(monitor): add sqlite timeout and WAL/synchronous NORMAL pragmas in monitor_app.py"
  ```
