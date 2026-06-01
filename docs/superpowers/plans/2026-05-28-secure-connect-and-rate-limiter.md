# Safe Connect and Rate Limiting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement secure self-service chat connections using short-lived tokens and robust, scalable per-channel rate limiting and polling.

**Architecture:** Use a new SQLite table `pending_connections` to securely store 10-minute alphanumeric tokens generated from Bitrix via `/tg_connect`. Implement isolated per-channel `asyncio.Queue` queues and background consumers with a 0.2s throttle (Leaky Bucket) to isolate and throttle traffic, and a centralized `PollScheduler` with a concurrency semaphore for scaling.

**Tech Stack:** Python 3.11, `asyncio`, `sqlite3`, `FastAPI` (FastAPI is already used for the webhook server).

---

### Task 1: Database Schema and Helpers

**Files:**
- Modify: `mirror_state_store.py` (Add pending connection methods and table initialization)
- Test: `tests/test_state_store.py` (Verify database methods for tokens)

- [ ] **Step 1: Write database verification and token tests**

Add these tests to `tests/test_state_store.py`:
```python
    async def test_pending_connections_flow(self):
        # 1. Generate token
        token = "testtoken123"
        expires_at = int(time.time()) + 600
        await self.store.save_pending_connection("chat123", token, expires_at)
        
        # 2. Verify valid token
        is_valid = await self.store.verify_and_consume_token("chat123", token)
        self.assertTrue(is_valid)
        
        # 3. Double consumption must fail
        is_valid_again = await self.store.verify_and_consume_token("chat123", token)
        self.assertFalse(is_valid_again)
        
        # 4. Expired token must fail
        expired_token = "expired456"
        await self.store.save_pending_connection("chat123", expired_token, int(time.time()) - 10)
        is_expired_valid = await self.store.verify_and_consume_token("chat123", expired_token)
        self.assertFalse(is_expired_valid)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_state_store.py -k test_pending_connections_flow`
Expected: FAIL due to missing methods and table.

- [ ] **Step 3: Write minimal implementation in `mirror_state_store.py`**

Modify `mirror_state_store.py` initialization and add helper methods:
```python
# In MirrorStateStore.__init__ (around creating tables):
            connection.execute("""
                CREATE TABLE IF NOT EXISTS pending_connections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bitrix_dialog_id TEXT NOT NULL,
                    token TEXT NOT NULL UNIQUE,
                    expires_at_unix INTEGER NOT NULL,
                    created_at_unix INTEGER NOT NULL
                )
            """)
            connection.execute("CREATE INDEX IF NOT EXISTS idx_pending_connections_token ON pending_connections(token)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_pending_connections_expires ON pending_connections(expires_at_unix)")
```
And add these methods:
```python
    async def save_pending_connection(self, bitrix_dialog_id: str, token: str, expires_at_unix: int) -> None:
        await asyncio.to_thread(self._save_pending_connection_sync, bitrix_dialog_id, token, expires_at_unix)

    def _save_pending_connection_sync(self, bitrix_dialog_id: str, token: str, expires_at_unix: int) -> None:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute("DELETE FROM pending_connections WHERE expires_at_unix < ?", (now,))
            connection.execute(
                "INSERT INTO pending_connections (bitrix_dialog_id, token, expires_at_unix, created_at_unix) VALUES (?, ?, ?, ?)",
                (bitrix_dialog_id, token, expires_at_unix, now),
            )
            connection.commit()

    async def verify_and_consume_token(self, bitrix_dialog_id: str, token: str) -> bool:
        return await asyncio.to_thread(self._verify_and_consume_token_sync, bitrix_dialog_id, token)

    def _verify_and_consume_token_sync(self, bitrix_dialog_id: str, token: str) -> bool:
        now = int(time.time())
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, bitrix_dialog_id FROM pending_connections WHERE token = ? AND expires_at_unix >= ?",
                (token, now),
            ).fetchone()
            if row is None:
                return False
            db_dialog_id = row[1]
            if db_dialog_id != bitrix_dialog_id:
                return False
            connection.execute("DELETE FROM pending_connections WHERE token = ?", (token,))
            connection.commit()
            return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_state_store.py -k test_pending_connections_flow`
Expected: PASS

- [ ] **Step 5: Commit**

Run:
```bash
git add mirror_state_store.py tests/test_state_store.py
git commit -m "feat: add SQLite pending_connections table and token verification helpers"
```

---

### Task 2: Bitrix Token Generation `/tg_connect`

**Files:**
- Modify: `server-side/app.py` (Add `/tg_connect` command handler)
- Test: Mock incoming HTTP requests to `/bitrix/bot` with `/tg_connect`

- [ ] **Step 1: Write test or manual verification verification steps**

We will mock the DB path in environment variables and verify token creation:
Verify that when `/tg_connect` is received, a token is generated, inserted into SQLite, and a prompt with `/connect <dialog_id> <token>` is sent back.

- [ ] **Step 2: Implement `/tg_connect` in `server-side/app.py`**

Import `sqlite3`, `time`, `secrets` in `server-side/app.py`:
```python
import sqlite3
import time
import secrets
```
Modify the `ONIMBOTMESSAGEADD` handler inside `bitrix_bot` in `server-side/app.py`:
```python
        elif event == "ONIMBOTMESSAGEADD" and dialog_id:
            text_lower = message_text.lower().strip()
            reply = None

            if text_lower in {"/start", "start"}:
                reply = "Привет. Я получил сообщение и могу отвечать в этот чат."
            elif text_lower in {"/ping", "ping"}:
                reply = "pong"
            elif text_lower.startswith("/tg_connect"):
                # 1. Generate secure token
                token = secrets.token_hex(4) # 8 alphanumeric chars
                expires_at = int(time.time()) + 600 # 10 mins
                db_raw = os.getenv("MIRROR_STATE_DB_PATH", "mirror_state.sqlite3")
                db_path = Path(db_raw) if Path(db_raw).is_absolute() else Path.cwd() / db_raw
                
                # 2. Write to SQLite
                try:
                    conn = sqlite3.connect(db_path, timeout=10)
                    with conn:
                        conn.execute("DELETE FROM pending_connections WHERE expires_at_unix < ?", (int(time.time()),))
                        conn.execute(
                            "INSERT INTO pending_connections (bitrix_dialog_id, token, expires_at_unix, created_at_unix) VALUES (?, ?, ?, ?)",
                            (dialog_id, token, expires_at, int(time.time())),
                        )
                    reply = (
                        f"🔑 Одноразовый токен сгенерирован.\n"
                        f"Отправьте следующую команду в вашей Telegram-группе в течение 10 минут:\n\n"
                        f"`/connect {dialog_id} {token}`"
                    )
                except Exception as e:
                    write_log("DB_ERROR", repr(e))
                    reply = "⚠️ Не удалось создать токен подключения. Попробуйте позже."

            if reply is not None:
                await send_bot_message(
                    dialog_id=dialog_id,
                    bot_id=bot_id,
                    message=reply
                )
```

- [ ] **Step 3: Verify execution and commit**

Run:
```bash
git add server-side/app.py
git commit -m "feat: implement /tg_connect token generation command in Bitrix webhook receiver"
```

---

### Task 3: Telegram `/connect` Command Token Support

**Files:**
- Modify: `handlers.py` (Allow `/connect <dialog_id> <token>` without admin check)
- Test: `tests/test_handlers.py` (Verify `/connect` with token flows)

- [ ] **Step 1: Write test for token-based /connect**

Modify `tests/test_handlers.py` to assert that:
- `/connect <dialog_id> <token>` bypasses administrative check and succeeds if token matches.
- `/connect <dialog_id>` still requires administrative check.

- [ ] **Step 2: Modify `cmd_connect` in `handlers.py`**

Replace `cmd_connect` logic in `handlers.py`:
```python
async def cmd_connect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return

    mirror: MirrorService = context.application.bot_data["mirror_service"]
    args = context.args or []
    
    if len(args) == 2:
        # Token-based flow for self-service connection (no admin check required!)
        bitrix_dialog_id = args[0].strip()
        token = args[1].strip()
        
        if not _BITRIX_ID_RE.match(bitrix_dialog_id):
            sent = await msg.reply_text("Неверный формат Bitrix Chat ID.")
            _bot_reply_ids.setdefault(chat.id, []).append(sent.message_id)
            return

        is_valid = await mirror.state_store.verify_and_consume_token(bitrix_dialog_id, token)
        if not is_valid:
            sent = await msg.reply_text("⚠️ Неверный, использованный или просроченный токен подключения.")
            _bot_reply_ids.setdefault(chat.id, []).append(sent.message_id)
            return
            
    elif len(args) == 1:
        # Traditional admin-only connection flow
        if not await _check_admin(update, mirror):
            return
        bitrix_dialog_id = args[0].strip()
        if not _BITRIX_ID_RE.match(bitrix_dialog_id):
            sent = await msg.reply_text("Неверный формат Bitrix Chat ID.")
            _bot_reply_ids.setdefault(chat.id, []).append(sent.message_id)
            return
    else:
        sent = await msg.reply_text(
            "Использование:\n"
            "Самостоятельно: `/connect <BitrixChatId> <Token>`\n"
            "Для администраторов: `/connect <BitrixChatId>`"
        )
        _bot_reply_ids.setdefault(chat.id, []).append(sent.message_id)
        return

    is_forum = getattr(chat, "is_forum", False)
    topic_id = msg.message_thread_id if is_forum else None
    try:
        await mirror.connect_mapping(chat.id, bitrix_dialog_id, topic_id, "")
    except ValueError as exc:
        sent = await msg.reply_text(f"⚠️ {exc}")
        _bot_reply_ids.setdefault(chat.id, []).append(sent.message_id)
        return

    thread_info = f" (ветка #{topic_id})" if topic_id else ""
    sent = await msg.reply_text(f"✅ Связка установлена: {bitrix_dialog_id} ↔ этот чат{thread_info}")
    _bot_reply_ids.setdefault(chat.id, []).append(sent.message_id)
```

- [ ] **Step 3: Run handlers tests and commit**

Run: `pytest tests/test_handlers.py`
Expected: PASS
Run:
```bash
git add handlers.py
git commit -m "feat: allow self-service Telegram /connect using secure tokens"
```

---

### Task 4: Per-Channel Queues and Throttling Worker

**Files:**
- Modify: `mirror_service.py` (Implement isolated per-channel queues and consumers)
- Test: `tests/test_mirror_service.py` (Verify throttling and drops)

- [ ] **Step 1: Write test for per-channel queue throttle**

Add a test case in `tests/test_mirror_service.py` that verifies:
- Pushing multiple messages sequentially is throttled to 0.2s intervals.
- Overflowing 100 messages drops subsequent messages for that channel without affecting other channels.

- [ ] **Step 2: Implement isolated queues in `mirror_service.py`**

Modify `mirror_service.py` properties in `__init__`:
```python
        # Remove self._send_queue
        # Add per-channel structures:
        self._channel_queues: dict[int, asyncio.Queue[Message]] = {}
        self._channel_workers: dict[int, asyncio.Task] = {}
        self._channel_locks: dict[int, asyncio.Lock] = {}
```

Modify `enqueue_telegram_message` and create task helpers:
```python
    async def enqueue_telegram_message(self, message: Message) -> None:
        if not self._forwarding_enabled:
            logger.info("Dropping Telegram message %s because forwarding is disabled", message.message_id)
            return

        chat_id = message.chat_id
        if chat_id not in self._channel_queues:
            self._channel_queues[chat_id] = asyncio.Queue(maxsize=100)
            self._channel_workers[chat_id] = asyncio.create_task(
                self._per_channel_worker(chat_id),
                name=f"channel-worker-{chat_id}"
            )

        queue = self._channel_queues[chat_id]
        if queue.full():
            logger.warning("Queue full for chat_id=%s, dropping message=%s", chat_id, message.message_id)
            return
            
        queue.put_nowait(message)

    async def _per_channel_worker(self, chat_id: int) -> None:
        queue = self._channel_queues[chat_id]
        min_interval = 0.2 # 5 messages per second
        last_send_time = 0.0

        while not self._stop_event.is_set():
            try:
                message = await queue.get()
            except asyncio.CancelledError:
                break

            try:
                now = asyncio.get_event_loop().time()
                elapsed = now - last_send_time
                if elapsed < min_interval:
                    await asyncio.sleep(min_interval - elapsed)
                    now = asyncio.get_event_loop().time()
                last_send_time = now

                mapping = self.resolve_mapping_for_telegram_message(message)
                if mapping is None:
                    continue

                dialog_id = mapping.bitrix_dialog_id
                reply_bitrix_id: Optional[int] = None
                if message.reply_to_message:
                    is_topic_header_reply = (
                        message.message_thread_id is not None
                        and message.reply_to_message.message_id == message.message_thread_id
                    )
                    if not is_topic_header_reply:
                        reply_link = await self.state_store.get_link_by_telegram_message(
                            telegram_chat_id=message.chat_id,
                            telegram_message_id=message.reply_to_message.message_id,
                        )
                        if reply_link is not None:
                            reply_bitrix_id = reply_link.bitrix_message_id

                if self._has_uploadable_file(message):
                    bitrix_message_id = await self._forward_telegram_file_to_bitrix(message, dialog_id=dialog_id, reply_id=reply_bitrix_id)
                else:
                    bitrix_message_id = await self.bitrix.send_message(
                        self.render_telegram_message(message), dialog_id=dialog_id, reply_id=reply_bitrix_id,
                    )

                await self.state_store.upsert_link(
                    telegram_chat_id=message.chat_id,
                    telegram_message_id=message.message_id,
                    bitrix_message_id=bitrix_message_id,
                    origin=MirrorOrigin.TELEGRAM,
                    telegram_message_date_unix=int(message.date.timestamp()) if message.date else None,
                    bitrix_author_id=None,
                    last_seen_bitrix_revision="telegram-origin",
                    telegram_message_thread_id=message.message_thread_id,
                )
            except Exception:
                logger.exception("Failed to mirror Telegram message in chat %s", chat_id)
            finally:
                queue.task_done()
```

- [ ] **Step 3: Run mirror tests and commit**

Run: `pytest tests/test_mirror_service.py`
Expected: PASS
Run:
```bash
git add mirror_service.py
git commit -m "feat: implement per-channel asyncio queues and throttled worker threads"
```

---

### Task 5: Centralized Scalable PollScheduler

**Files:**
- Modify: `mirror_service.py` (Add centralized `PollScheduler` task with concurrency semaphore)
- Test: `tests/test_mirror_service.py` (Verify sequential polling and semaphore locks)

- [ ] **Step 1: Write test for PollScheduler sequential execution**

Create integration test in `tests/test_mirror_service.py` checking that no more than 5 polling loops run concurrently under peak mappings.

- [ ] **Step 2: Replace individual loops with central scheduler in `mirror_service.py`**

Define `PollScheduler` task structure and semaphore:
```python
    async def start_bitrix_polling(self, application: Application) -> None:
        if not self.settings.sync_bitrix_to_telegram:
            return
        if not self.settings.chat_mappings:
            return
            
        self._poll_semaphore = asyncio.Semaphore(5)
        self._scheduler_task = asyncio.create_task(
            self._poll_scheduler_loop(application),
            name="bitrix-poll-scheduler"
        )

    async def _poll_scheduler_loop(self, application: Application) -> None:
        while not self._stop_event.is_set():
            try:
                mappings = list(self.settings.chat_mappings)
                tasks = []
                for mapping in mappings:
                    # Stagger and check via semaphore
                    task = asyncio.create_task(self._throttled_poll(application, mapping))
                    tasks.append(task)
                
                await asyncio.gather(*tasks, return_exceptions=True)
            except Exception:
                logger.exception("Error in central poll scheduler")
            await asyncio.sleep(self.settings.bitrix_poll_interval_seconds)

    async def _throttled_poll(self, application: Application, mapping: ChatMapping) -> None:
        async with self._poll_semaphore:
            await self._sync_bitrix_messages_for_mapping(application, mapping)
```

- [ ] **Step 3: Run all tests and verify all code compiles and passes**

Run: `pytest`
Expected: PASS (All test suites pass).

- [ ] **Step 4: Commit and finalize**

Run:
```bash
git add mirror_service.py
git commit -m "feat: replace multiple poll loops with a centralized scalable PollScheduler using concurrency Semaphore"
```
