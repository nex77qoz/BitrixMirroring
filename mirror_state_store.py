from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import ParamSpec, TypeVar

from models import MessageMirrorLink, MirrorOrigin
from settings import ChatMapping, _parse_topic_ids

logger = logging.getLogger('tg-bitrix-mirror')
_P = ParamSpec('_P')
_T = TypeVar('_T')


async def _run_sync(function: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs) -> _T:
    # ponytail: synchronous SQLite is sufficient for this low-throughput bot;
    # move to one dedicated DB worker if measured lock waits affect the event loop.
    return function(*args, **kwargs)

class MirrorStateStore:

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._conn_lock = threading.Lock()

    async def initialize(self) -> None:
        await _run_sync(self._initialize_sync)

    async def upsert_link(self, *, telegram_chat_id: int, telegram_message_id: int, bitrix_message_id: int, origin: MirrorOrigin, telegram_message_date_unix: int | None, bitrix_author_id: int | None, last_seen_bitrix_revision: str, telegram_message_thread_id: int | None=None) -> None:
        await _run_sync(self._upsert_link_sync, telegram_chat_id, telegram_message_id, bitrix_message_id, origin, telegram_message_date_unix, bitrix_author_id, last_seen_bitrix_revision, telegram_message_thread_id)

    async def get_link_by_telegram_message(self, *, telegram_chat_id: int, telegram_message_id: int) -> MessageMirrorLink | None:
        return await _run_sync(self._get_link_by_telegram_message_sync, telegram_chat_id, telegram_message_id)

    async def get_link_by_bitrix_message(self, *, bitrix_message_id: int) -> MessageMirrorLink | None:
        return await _run_sync(self._get_link_by_bitrix_message_sync, bitrix_message_id)

    async def delete_link_by_bitrix_message(self, *, bitrix_message_id: int) -> None:
        await _run_sync(self._delete_link_by_bitrix_message_sync, bitrix_message_id)

    async def delete_links_by_telegram_chat(self, *, telegram_chat_id: int) -> None:
        await _run_sync(self._delete_links_by_telegram_chat_sync, telegram_chat_id)

    async def update_reaction_state(self, *, bitrix_message_id: int, bitrix_liked_by_bot: bool, last_seen_bitrix_likes: str) -> None:
        await _run_sync(self._update_reaction_state_sync, bitrix_message_id, bitrix_liked_by_bot, last_seen_bitrix_likes)

    async def save_topic_name(self, tg_chat_id: int, topic_id: int, name: str) -> None:
        await _run_sync(self._save_topic_name_sync, tg_chat_id, topic_id, name)

    async def load_topic_names(self) -> dict[tuple[int, int], str]:
        return await _run_sync(self._load_topic_names_sync)

    async def get_forwarding_enabled(self) -> bool:
        return await _run_sync(self._get_forwarding_enabled_sync)

    async def set_forwarding_enabled(self, enabled: bool) -> None:
        await _run_sync(self._set_forwarding_enabled_sync, enabled)

    async def load_bitrix_event_offset(self, bot_id: int) -> int | None:
        return await _run_sync(self._load_bitrix_event_offset_sync, bot_id)

    async def save_bitrix_event_offset(self, bot_id: int, offset: int) -> None:
        await _run_sync(self._save_bitrix_event_offset_sync, bot_id, offset)

    async def cleanup_old_links(self, max_age_seconds: int=7 * 24 * 3600) -> int:
        """Delete message_links older than max_age_seconds. Returns count of deleted rows."""
        return await _run_sync(self._cleanup_old_links_sync, max_age_seconds)

    def _initialize_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            cursor_columns = {row[1] for row in connection.execute('PRAGMA table_info(cursor_state)').fetchall()}
            if not cursor_columns:
                connection.execute('\n                    CREATE TABLE cursor_state (\n                        bitrix_dialog_id TEXT PRIMARY KEY,\n                        last_seen_bitrix_message_id INTEGER\n                    )\n                    ')
            elif 'singleton_key' in cursor_columns:
                logger.warning('Migrating cursor_state from singleton to per-dialog schema')
                old_row = connection.execute('SELECT last_seen_bitrix_message_id FROM cursor_state WHERE singleton_key = 1').fetchone()
                old_cursor = old_row[0] if old_row and old_row[0] is not None else None
                connection.execute('DROP TABLE cursor_state')
                connection.execute('\n                    CREATE TABLE cursor_state (\n                        bitrix_dialog_id TEXT PRIMARY KEY,\n                        last_seen_bitrix_message_id INTEGER\n                    )\n                    ')
                if old_cursor is not None:
                    connection.execute("INSERT INTO cursor_state(bitrix_dialog_id, last_seen_bitrix_message_id) VALUES('__legacy__', ?)", (old_cursor,))
            existing_columns = {row[1] for row in connection.execute('PRAGMA table_info(message_links)').fetchall()}
            if not existing_columns:
                connection.execute("\n                    CREATE TABLE message_links (\n                        telegram_chat_id INTEGER NOT NULL,\n                        telegram_message_id INTEGER NOT NULL,\n                        bitrix_message_id INTEGER NOT NULL UNIQUE,\n                        origin TEXT NOT NULL,\n                        telegram_message_date_unix INTEGER,\n                        bitrix_author_id INTEGER,\n                        last_seen_bitrix_revision TEXT NOT NULL,\n                        created_at_unix INTEGER NOT NULL,\n                        updated_at_unix INTEGER NOT NULL,\n                        bitrix_liked_by_bot INTEGER DEFAULT 0,\n                        last_seen_bitrix_likes TEXT DEFAULT '',\n                        PRIMARY KEY (telegram_chat_id, telegram_message_id)\n                    )\n                    ")
            elif 'last_seen_bitrix_deleted' in existing_columns:
                logger.warning('Migrating SQLite schema: removing obsolete last_seen_bitrix_deleted column')
                connection.execute('ALTER TABLE message_links RENAME TO message_links_legacy')
                connection.execute("\n                    CREATE TABLE message_links (\n                        telegram_chat_id INTEGER NOT NULL,\n                        telegram_message_id INTEGER NOT NULL,\n                        bitrix_message_id INTEGER NOT NULL UNIQUE,\n                        origin TEXT NOT NULL,\n                        telegram_message_date_unix INTEGER,\n                        bitrix_author_id INTEGER,\n                        last_seen_bitrix_revision TEXT NOT NULL,\n                        created_at_unix INTEGER NOT NULL,\n                        updated_at_unix INTEGER NOT NULL,\n                        bitrix_liked_by_bot INTEGER DEFAULT 0,\n                        last_seen_bitrix_likes TEXT DEFAULT '',\n                        PRIMARY KEY (telegram_chat_id, telegram_message_id)\n                    )\n                    ")
                connection.execute('\n                    INSERT INTO message_links (\n                        telegram_chat_id,\n                        telegram_message_id,\n                        bitrix_message_id,\n                        origin,\n                        telegram_message_date_unix,\n                        bitrix_author_id,\n                        last_seen_bitrix_revision,\n                        created_at_unix,\n                        updated_at_unix\n                    )\n                    SELECT telegram_chat_id, telegram_message_id, bitrix_message_id, origin,\n                           telegram_message_date_unix, bitrix_author_id, last_seen_bitrix_revision,\n                           created_at_unix, updated_at_unix\n                    FROM message_links_legacy\n                    ')
                connection.execute('DROP TABLE message_links_legacy')
            current_columns = {row[1] for row in connection.execute('PRAGMA table_info(message_links)').fetchall()}
            if 'bitrix_liked_by_bot' not in current_columns:
                connection.execute('ALTER TABLE message_links ADD COLUMN bitrix_liked_by_bot INTEGER DEFAULT 0')
            if 'last_seen_bitrix_likes' not in current_columns:
                connection.execute("ALTER TABLE message_links ADD COLUMN last_seen_bitrix_likes TEXT DEFAULT ''")
            if 'telegram_message_thread_id' not in current_columns:
                connection.execute('ALTER TABLE message_links ADD COLUMN telegram_message_thread_id INTEGER')
            connection.execute('CREATE INDEX IF NOT EXISTS idx_message_links_updated_at ON message_links(updated_at_unix)')
            connection.execute('DROP INDEX IF EXISTS idx_message_links_bitrix_message_id')
            connection.execute("\n                CREATE TABLE IF NOT EXISTS chat_mappings (\n                    id               INTEGER PRIMARY KEY AUTOINCREMENT,\n                    tg_chat_id       INTEGER NOT NULL,\n                    bitrix_dialog_id TEXT NOT NULL,\n                    label            TEXT DEFAULT '',\n                    created_at_unix  INTEGER NOT NULL,\n                    topic_ids        TEXT DEFAULT ''\n                )\n                ")
            chat_mapping_columns = {row[1] for row in connection.execute('PRAGMA table_info(chat_mappings)').fetchall()}
            if 'id' not in chat_mapping_columns:
                logger.warning('Migrating SQLite schema: rebuilding chat_mappings for multi-mapping support')
                connection.execute('ALTER TABLE chat_mappings RENAME TO chat_mappings_legacy')
                connection.execute("\n                    CREATE TABLE chat_mappings (\n                        id               INTEGER PRIMARY KEY AUTOINCREMENT,\n                        tg_chat_id       INTEGER NOT NULL,\n                        bitrix_dialog_id TEXT NOT NULL,\n                        label            TEXT DEFAULT '',\n                        created_at_unix  INTEGER NOT NULL,\n                        topic_ids        TEXT DEFAULT ''\n                    )\n                    ")
                legacy_columns = {row[1] for row in connection.execute('PRAGMA table_info(chat_mappings_legacy)').fetchall()}
                topic_select = 'topic_ids' if 'topic_ids' in legacy_columns else "''"
                connection.execute(f'\n                    INSERT INTO chat_mappings (tg_chat_id, bitrix_dialog_id, label, created_at_unix, topic_ids)\n                    SELECT tg_chat_id, bitrix_dialog_id, label, created_at_unix, {topic_select}\n                    FROM chat_mappings_legacy\n                    ')
                connection.execute('DROP TABLE chat_mappings_legacy')
                chat_mapping_columns = {row[1] for row in connection.execute('PRAGMA table_info(chat_mappings)').fetchall()}
            if 'topic_ids' not in chat_mapping_columns:
                connection.execute("ALTER TABLE chat_mappings ADD COLUMN topic_ids TEXT DEFAULT ''")
            duplicates = connection.execute('SELECT bitrix_dialog_id FROM chat_mappings GROUP BY bitrix_dialog_id HAVING COUNT(*) > 1').fetchall()
            if duplicates:
                for dup_dialog_id, in duplicates:
                    rows = connection.execute('SELECT id, tg_chat_id, topic_ids, label FROM chat_mappings WHERE bitrix_dialog_id = ? ORDER BY id', (dup_dialog_id,)).fetchall()
                    if not rows:
                        continue
                    first_row_id = rows[0][0]
                    merged_tg_chat_id = rows[0][1]
                    all_topics = []
                    seen_topics = set()
                    labels = []
                    for row in rows:
                        row_id, tg_chat_id, topic_str, label_str = (row[0], row[1], row[2], row[3])
                        if tg_chat_id == merged_tg_chat_id:
                            topic_str = str(topic_str) if topic_str else ''
                            for t in topic_str.split(','):
                                t_clean = t.strip()
                                if t_clean.lstrip('-').isdigit():
                                    t_val = int(t_clean)
                                    if t_val not in seen_topics:
                                        seen_topics.add(t_val)
                                        all_topics.append(t_val)
                            label_str = str(label_str).strip() if label_str else ''
                            if label_str and label_str not in labels:
                                labels.append(label_str)
                        else:
                            logger.warning('Discarding duplicate mapping for bitrix_dialog_id %s in tg_chat_id %s (keeping tg_chat_id %s)', dup_dialog_id, tg_chat_id, merged_tg_chat_id)
                    merged_topics_str = ','.join(str(t) for t in all_topics)
                    merged_label = ', '.join(labels)
                    logger.warning('Merging legacy duplicates for bitrix_dialog_id %s in tg_chat_id %s: topic_ids=%s, label=%s', dup_dialog_id, merged_tg_chat_id, merged_topics_str, merged_label)
                    connection.execute('UPDATE chat_mappings SET topic_ids = ?, label = ? WHERE id = ?', (merged_topics_str, merged_label, first_row_id))
                    connection.execute('DELETE FROM chat_mappings WHERE bitrix_dialog_id = ? AND id != ?', (dup_dialog_id, first_row_id))
                connection.commit()
            connection.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_mappings_bitrix_dialog_id ON chat_mappings(bitrix_dialog_id)')
            connection.execute('\n                CREATE TABLE IF NOT EXISTS topic_names (\n                    tg_chat_id INTEGER NOT NULL,\n                    topic_id   INTEGER NOT NULL,\n                    name       TEXT NOT NULL,\n                    PRIMARY KEY (tg_chat_id, topic_id)\n                )\n                ')
            connection.execute('\n                CREATE TABLE IF NOT EXISTS runtime_settings (\n                    key        TEXT PRIMARY KEY,\n                    value      TEXT NOT NULL,\n                    updated_at_unix INTEGER NOT NULL\n                )\n                ')
            connection.execute('\n                CREATE TABLE IF NOT EXISTS telegram_admins (\n                    tg_user_id   INTEGER PRIMARY KEY,\n                    added_at_unix INTEGER NOT NULL\n                )\n                ')
            connection.execute('\n                CREATE TABLE IF NOT EXISTS pending_connections (\n                    id INTEGER PRIMARY KEY AUTOINCREMENT,\n                    bitrix_dialog_id TEXT NOT NULL,\n                    token TEXT NOT NULL UNIQUE,\n                    expires_at_unix INTEGER NOT NULL,\n                    created_at_unix INTEGER NOT NULL,\n                    chat_title TEXT DEFAULT \'\'\n                )\n            ')
            pending_columns = {row[1] for row in connection.execute('PRAGMA table_info(pending_connections)').fetchall()}
            if 'chat_title' not in pending_columns:
                connection.execute("ALTER TABLE pending_connections ADD COLUMN chat_title TEXT DEFAULT ''")
            connection.execute('CREATE INDEX IF NOT EXISTS idx_pending_connections_token ON pending_connections(token)')
            connection.execute('CREATE INDEX IF NOT EXISTS idx_pending_connections_expires ON pending_connections(expires_at_unix)')
            connection.commit()

    def _upsert_link_sync(self, telegram_chat_id: int, telegram_message_id: int, bitrix_message_id: int, origin: MirrorOrigin, telegram_message_date_unix: int | None, bitrix_author_id: int | None, last_seen_bitrix_revision: str, telegram_message_thread_id: int | None=None) -> None:
        now = int(time.time())
        with self._connect() as connection:
            existing_by_bitrix = connection.execute('SELECT telegram_chat_id, telegram_message_id FROM message_links WHERE bitrix_message_id = ?', (bitrix_message_id,)).fetchone()
            if existing_by_bitrix is not None and (int(existing_by_bitrix[0]) != telegram_chat_id or int(existing_by_bitrix[1]) != telegram_message_id):
                logger.warning('Replacing existing message link for bitrix_message_id=%s from telegram=(%s,%s) to telegram=(%s,%s)', bitrix_message_id, existing_by_bitrix[0], existing_by_bitrix[1], telegram_chat_id, telegram_message_id)
                connection.execute('DELETE FROM message_links WHERE bitrix_message_id = ?', (bitrix_message_id,))
            connection.execute('\n                INSERT INTO message_links (\n                    telegram_chat_id,\n                    telegram_message_id,\n                    bitrix_message_id,\n                    origin,\n                    telegram_message_date_unix,\n                    bitrix_author_id,\n                    last_seen_bitrix_revision,\n                    created_at_unix,\n                    updated_at_unix,\n                    telegram_message_thread_id\n                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                ON CONFLICT(telegram_chat_id, telegram_message_id) DO UPDATE SET\n                    bitrix_message_id = excluded.bitrix_message_id,\n                    origin = excluded.origin,\n                    telegram_message_date_unix = excluded.telegram_message_date_unix,\n                    bitrix_author_id = excluded.bitrix_author_id,\n                    last_seen_bitrix_revision = excluded.last_seen_bitrix_revision,\n                    updated_at_unix = excluded.updated_at_unix,\n                    telegram_message_thread_id = excluded.telegram_message_thread_id\n                ', (telegram_chat_id, telegram_message_id, bitrix_message_id, origin.value, telegram_message_date_unix, bitrix_author_id, last_seen_bitrix_revision, now, now, telegram_message_thread_id))
            connection.commit()

    def _get_link_by_telegram_message_sync(self, telegram_chat_id: int, telegram_message_id: int) -> MessageMirrorLink | None:
        with self._connect() as connection:
            row = connection.execute('\n                SELECT telegram_chat_id, telegram_message_id, bitrix_message_id, origin,\n                       telegram_message_date_unix, bitrix_author_id, last_seen_bitrix_revision,\n                       created_at_unix, updated_at_unix, bitrix_liked_by_bot, last_seen_bitrix_likes,\n                       telegram_message_thread_id\n                FROM message_links\n                WHERE telegram_chat_id = ? AND telegram_message_id = ?\n                ', (telegram_chat_id, telegram_message_id)).fetchone()
        return self._row_to_link(row)

    def _get_link_by_bitrix_message_sync(self, bitrix_message_id: int) -> MessageMirrorLink | None:
        with self._connect() as connection:
            row = connection.execute('\n                SELECT telegram_chat_id, telegram_message_id, bitrix_message_id, origin,\n                       telegram_message_date_unix, bitrix_author_id, last_seen_bitrix_revision,\n                       created_at_unix, updated_at_unix, bitrix_liked_by_bot, last_seen_bitrix_likes,\n                       telegram_message_thread_id\n                FROM message_links\n                WHERE bitrix_message_id = ?\n                ', (bitrix_message_id,)).fetchone()
        return self._row_to_link(row)

    def _delete_link_by_bitrix_message_sync(self, bitrix_message_id: int) -> None:
        with self._connect() as connection:
            connection.execute('DELETE FROM message_links WHERE bitrix_message_id = ?', (bitrix_message_id,))
            connection.commit()

    def _delete_links_by_telegram_chat_sync(self, telegram_chat_id: int) -> None:
        with self._connect() as connection:
            deleted = connection.execute('DELETE FROM message_links WHERE telegram_chat_id = ?', (telegram_chat_id,)).rowcount
            connection.commit()
        if deleted:
            logger.warning('Deleted %s stale message link(s) for migrated/obsolete telegram_chat_id=%s', deleted, telegram_chat_id)

    def _update_reaction_state_sync(self, bitrix_message_id: int, bitrix_liked_by_bot: bool, last_seen_bitrix_likes: str) -> None:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute('\n                UPDATE message_links\n                SET bitrix_liked_by_bot = ?,\n                    last_seen_bitrix_likes = ?,\n                    updated_at_unix = ?\n                WHERE bitrix_message_id = ?\n                ', (int(bitrix_liked_by_bot), last_seen_bitrix_likes, now, bitrix_message_id))
            connection.commit()

    def _cleanup_old_links_sync(self, max_age_seconds: int) -> int:
        cutoff = int(time.time()) - max_age_seconds
        with self._connect() as connection:
            cursor = connection.execute('DELETE FROM message_links WHERE updated_at_unix < ?', (cutoff,))
            deleted = cursor.rowcount
            connection.commit()
        if deleted:
            logger.info('Cleaned up %s old message link(s) older than %s seconds', deleted, max_age_seconds)
        return deleted

    def _row_to_link(self, row: sqlite3.Row | None) -> MessageMirrorLink | None:
        if row is None:
            return None
        return MessageMirrorLink(telegram_chat_id=int(row[0]), telegram_message_id=int(row[1]), bitrix_message_id=int(row[2]), origin=MirrorOrigin(str(row[3])), telegram_message_date_unix=int(row[4]) if row[4] is not None else None, bitrix_author_id=int(row[5]) if row[5] is not None else None, last_seen_bitrix_revision=str(row[6]), created_at_unix=int(row[7]), updated_at_unix=int(row[8]), bitrix_liked_by_bot=bool(row[9]) if row[9] is not None else False, last_seen_bitrix_likes=str(row[10]) if row[10] is not None else '', telegram_message_thread_id=int(row[11]) if row[11] is not None else None)

    def _save_topic_name_sync(self, tg_chat_id: int, topic_id: int, name: str) -> None:
        with self._connect() as connection:
            connection.execute('\n                INSERT INTO topic_names (tg_chat_id, topic_id, name)\n                VALUES (?, ?, ?)\n                ON CONFLICT(tg_chat_id, topic_id) DO UPDATE SET name = excluded.name\n                ', (tg_chat_id, topic_id, name))
            connection.commit()

    def _load_topic_names_sync(self) -> dict[tuple[int, int], str]:
        with self._connect() as connection:
            rows = connection.execute('SELECT tg_chat_id, topic_id, name FROM topic_names').fetchall()
        return {(int(row[0]), int(row[1])): str(row[2]) for row in rows}

    def _get_forwarding_enabled_sync(self) -> bool:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM runtime_settings WHERE key = 'forwarding_enabled'").fetchone()
        if row is None:
            return True
        return str(row[0]).strip().lower() not in {'0', 'false', 'no', 'off'}

    def _set_forwarding_enabled_sync(self, enabled: bool) -> None:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute("\n                INSERT INTO runtime_settings(key, value, updated_at_unix)\n                VALUES('forwarding_enabled', ?, ?)\n                ON CONFLICT(key) DO UPDATE SET\n                    value = excluded.value,\n                    updated_at_unix = excluded.updated_at_unix\n                ", ('1' if enabled else '0', now))
            connection.commit()

    def _load_bitrix_event_offset_sync(self, bot_id: int) -> int | None:
        key = f'bitrix_event_offset:{bot_id}'
        with self._connect() as connection:
            row = connection.execute('SELECT value FROM runtime_settings WHERE key = ?', (key,)).fetchone()
        if row is None:
            return None
        val = str(row[0]).strip()
        return int(val) if val.isdigit() else None

    def _save_bitrix_event_offset_sync(self, bot_id: int, offset: int) -> None:
        key = f'bitrix_event_offset:{bot_id}'
        now = int(time.time())
        with self._connect() as connection:
            row = connection.execute('SELECT value FROM runtime_settings WHERE key = ?', (key,)).fetchone()
            current = int(row[0]) if row and str(row[0]).isdigit() else None
            if current is not None and offset < current:
                return
            connection.execute('\n                INSERT INTO runtime_settings(key, value, updated_at_unix)\n                VALUES(?, ?, ?)\n                ON CONFLICT(key) DO UPDATE SET\n                    value = excluded.value,\n                    updated_at_unix = excluded.updated_at_unix\n                ', (key, str(offset), now))
            connection.commit()

    async def is_admin(self, tg_user_id: int) -> bool:
        return await _run_sync(self._is_admin_sync, tg_user_id)

    def _is_admin_sync(self, tg_user_id: int) -> bool:
        with self._connect() as connection:
            row = connection.execute('SELECT 1 FROM telegram_admins WHERE tg_user_id = ?', (tg_user_id,)).fetchone()
        return row is not None

    async def save_pending_connection(self, bitrix_dialog_id: str, token: str, expires_at_unix: int, chat_title: str = "") -> None:
        await _run_sync(self._save_pending_connection_sync, bitrix_dialog_id, token, expires_at_unix, chat_title)

    def _save_pending_connection_sync(self, bitrix_dialog_id: str, token: str, expires_at_unix: int, chat_title: str) -> None:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute('DELETE FROM pending_connections WHERE expires_at_unix < ?', (now,))
            connection.execute('INSERT INTO pending_connections (bitrix_dialog_id, token, expires_at_unix, created_at_unix, chat_title) VALUES (?, ?, ?, ?, ?)', (bitrix_dialog_id, token, expires_at_unix, now, chat_title))
            connection.commit()

    async def verify_and_consume_token(self, bitrix_dialog_id: str, token: str) -> str | None:
        return await _run_sync(self._verify_and_consume_token_sync, bitrix_dialog_id, token)

    def _verify_and_consume_token_sync(self, bitrix_dialog_id: str, token: str) -> str | None:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute('BEGIN EXCLUSIVE')
            row = connection.execute('SELECT bitrix_dialog_id, chat_title FROM pending_connections WHERE token = ? AND expires_at_unix >= ?', (token, now)).fetchone()
            if row is None:
                connection.rollback()
                return None
            if row[0] != bitrix_dialog_id:
                connection.rollback()
                return None
            chat_title = str(row[1] or "")
            connection.execute('DELETE FROM pending_connections WHERE token = ?', (token,))
            connection.commit()
            return chat_title

    async def load_all_chat_mappings(self) -> tuple[ChatMapping, ...]:
        return await _run_sync(self._load_all_chat_mappings_sync)

    def _load_all_chat_mappings_sync(self) -> tuple[ChatMapping, ...]:
        with self._connect() as connection:
            try:
                rows = connection.execute('SELECT id, tg_chat_id, bitrix_dialog_id, topic_ids, label FROM chat_mappings ORDER BY created_at_unix, id').fetchall()
            except sqlite3.OperationalError:
                return ()
        return tuple(ChatMapping(mapping_id=int(row[0]), tg_chat_id=int(row[1]), bitrix_dialog_id=str(row[2]), topic_ids=_parse_topic_ids(str(row[3]) if row[3] else ''), label=str(row[4]) if row[4] is not None else '') for row in rows)

    async def add_chat_mapping(self, tg_chat_id: int, bitrix_dialog_id: str, topic_ids: list[int], label: str) -> int:
        return await _run_sync(self._add_chat_mapping_sync, tg_chat_id, bitrix_dialog_id, topic_ids, label)

    def _add_chat_mapping_sync(self, tg_chat_id: int, bitrix_dialog_id: str, topic_ids: list[int], label: str) -> int:
        topic_ids_str = ','.join(str(t) for t in topic_ids)
        now = int(time.time())
        with self._connect() as connection:
            try:
                cursor = connection.execute('INSERT INTO chat_mappings (tg_chat_id, bitrix_dialog_id, label, created_at_unix, topic_ids) VALUES (?,?,?,?,?)', (tg_chat_id, bitrix_dialog_id, label, now, topic_ids_str))
                connection.commit()
                last_id = cursor.lastrowid
                if last_id is None:
                    raise RuntimeError('Failed to get lastrowid after INSERT into chat_mappings')
                return last_id
            except sqlite3.IntegrityError as exc:
                raise ValueError(f'Bitrix dialog {bitrix_dialog_id} уже привязан к другому маппингу') from exc

    async def update_chat_mapping_topic_ids(self, mapping_id: int, topic_ids: list[int]) -> None:
        await _run_sync(self._update_chat_mapping_topic_ids_sync, mapping_id, topic_ids)

    def _update_chat_mapping_topic_ids_sync(self, mapping_id: int, topic_ids: list[int]) -> None:
        topic_ids_str = ','.join(str(t) for t in topic_ids)
        with self._connect() as connection:
            connection.execute('UPDATE chat_mappings SET topic_ids = ? WHERE id = ?', (topic_ids_str, mapping_id))
            connection.commit()

    async def remove_chat_mapping(self, mapping_id: int) -> bool:
        return await _run_sync(self._remove_chat_mapping_sync, mapping_id)

    def _remove_chat_mapping_sync(self, mapping_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute('DELETE FROM chat_mappings WHERE id = ?', (mapping_id,))
            connection.commit()
        return cursor.rowcount > 0

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if self._conn is None:
            self._conn = sqlite3.connect(self._path, timeout=30.0, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute('PRAGMA journal_mode=WAL')
            self._conn.execute('PRAGMA synchronous=NORMAL')
        with self._conn_lock:
            yield self._conn
