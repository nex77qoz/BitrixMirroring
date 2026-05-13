# TgSettings — Design Spec

**Date:** 2026-05-13  
**Branch:** TgSettings

---

## Overview

Three related features:

1. **Admin setup in installer** — `install.sh` collects Telegram user IDs of admins and seeds the database.
2. **`/connect <BitrixChatId>`** — Telegram bot command that links the current chat/topic to a Bitrix dialog. Live-activates immediately (no restart). Admin-only.
3. **`/disconnect`** — Removes the mapping for the current chat/topic. Live. Admin-only.
4. **Admin list in dashboard** — `monitor_app` shows and manages the admin list.

---

## Data Model

### New table: `telegram_admins`

```sql
CREATE TABLE IF NOT EXISTS telegram_admins (
    tg_user_id   INTEGER PRIMARY KEY,
    added_at_unix INTEGER NOT NULL
);
```

Created in:
- `MirrorStateStore._initialize_sync()` (main service)
- `monitor_app._ensure_chat_mappings_table()` (dashboard)

No other schema changes. Existing `chat_mappings` table is reused by `/connect`.

---

## Components

### 1. `install.sh`

**`step_collect_config`** — new optional prompt after monitor password:

```
Введите Telegram ID администраторов бота (через запятую).
Администраторы могут использовать команды /connect и /disconnect.
(Enter, чтобы пропустить — добавьте позже через панель мониторинга)
```

Stores result in `TG_ADMIN_IDS` bash variable (not written to .env).

**New step `step_setup_admins`** — called after `step_chat_mapping`:
- Parses `TG_ADMIN_IDS` as comma-separated integers
- Inserts valid IDs into `telegram_admins` via `sqlite3`
- Warns if none provided

---

### 2. `mirror_state_store.py`

New async methods:

| Method | Description |
|--------|-------------|
| `is_admin(tg_user_id: int) -> bool` | True if user ID is in `telegram_admins` |
| `load_all_chat_mappings() -> tuple[ChatMapping, ...]` | Re-read all mappings from DB |
| `add_chat_mapping(tg_chat_id, bitrix_dialog_id, topic_ids, label) -> int` | INSERT, returns new `id` |
| `remove_chat_mapping(mapping_id: int) -> bool` | DELETE by id, returns True if deleted |

Synchronous counterparts follow existing `_*_sync` pattern.

---

### 3. `mirror_service.py`

New methods:

```python
async def is_admin(self, tg_user_id: int) -> bool:
    return await self.state_store.is_admin(tg_user_id)

async def reload_mappings(self) -> None:
    """Re-read chat_mappings from DB; updates _tg_to_mappings and _bitrix_to_mapping in-place."""
    mappings = await self.state_store.load_all_chat_mappings()
    self._tg_to_mappings = {}
    for m in mappings:
        self._tg_to_mappings.setdefault(m.tg_chat_id, []).append(m)
    self._bitrix_to_mapping = {m.bitrix_dialog_id: m for m in mappings}

async def connect_mapping(
    self, tg_chat_id: int, bitrix_dialog_id: str,
    topic_id: Optional[int], label: str
) -> None:
    """Add mapping to DB and reload. Raises ValueError on conflict."""
    topic_ids = [topic_id] if topic_id is not None else []
    # conflict check: same bitrix_dialog_id already mapped?
    if bitrix_dialog_id in self._bitrix_to_mapping:
        raise ValueError(f"Bitrix dialog {bitrix_dialog_id} already linked")
    await self.state_store.add_chat_mapping(tg_chat_id, bitrix_dialog_id, topic_ids, label)
    await self.reload_mappings()

async def disconnect_mapping(self, tg_chat_id: int, topic_id: Optional[int]) -> bool:
    """Remove mapping for this chat+topic from DB and reload. Returns True if found."""
    mapping = self.resolve_mapping_for_chat_and_thread(tg_chat_id, topic_id)
    if mapping is None:
        return False
    await self.state_store.remove_chat_mapping(mapping.mapping_id)
    await self.reload_mappings()
    return True
```

`reload_mappings` is thread-safe for reads (GIL + dict swap). No external lock needed for this use-case.

---

### 4. `handlers.py`

#### `cmd_connect`

```
/connect <BitrixChatId>
```

Flow:
1. Check `update.effective_user` exists and is admin (`mirror.is_admin(user_id)`)
2. Parse `context.args` — require exactly one argument matching `^(chat\d+|sg\d+|\d+)$`
3. Extract `message.chat_id` and `message.message_thread_id` (None if not in a topic)
4. Call `mirror.connect_mapping(chat_id, bitrix_dialog_id, topic_id, label="")`
5. On success: reply with "✅ Связка установлена: `{bitrix_dialog_id}` ↔ этот чат/ветка"
6. On `ValueError` (conflict): reply with error description

Only works in GROUP/SUPERGROUP (same guard as `on_message`).

#### `cmd_disconnect`

Flow:
1. Admin check
2. Extract `chat_id`, `thread_id`
3. Call `mirror.disconnect_mapping(chat_id, thread_id)`
4. On True: "✅ Связка удалена"
5. On False: "⚠️ Связка для этого чата/ветки не найдена"

#### Admin check helper

```python
async def _check_admin(update: Update, mirror: MirrorService) -> bool:
    user = update.effective_user
    if not user:
        return False
    return await mirror.is_admin(user.id)
```

---

### 5. `main.py`

Register handlers in `_build_application`:

```python
from handlers import cmd_connect, cmd_disconnect
application.add_handler(CommandHandler("connect", cmd_connect))
application.add_handler(CommandHandler("disconnect", cmd_disconnect))
```

---

### 6. `monitor_app.py`

#### New API endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/monitor/api/admins` | List all admin IDs |
| POST | `/monitor/api/admins` | Add admin `{tg_user_id: int}` |
| DELETE | `/monitor/api/admins/{tg_user_id}` | Remove admin |

#### Dashboard section

New section "Администраторы Telegram" (between Forwarding and Services):
- Table: `TG User ID` | `Добавлен` | `Удалить`
- Add form: integer input + "Добавить" button
- Note: "Эти пользователи могут использовать /connect и /disconnect в Telegram"

---

## Error Handling

- `/connect` without args → usage hint
- `/connect` with invalid Bitrix ID format → format error
- `/connect` when mapping already exists → conflict message
- `/connect`/`/disconnect` from non-admin → silently ignore (no reply to reduce spam)
- `/disconnect` when no mapping → "связка не найдена"
- DB errors in hot reload → logged, mapping state unchanged

---

## Out of Scope

- Adding/removing admins via bot commands (dashboard only)
- Editing an existing mapping via `/connect` (must `/disconnect` first)
- `/connect` in private chats or channels
