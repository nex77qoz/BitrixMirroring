from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

from models import CursorState, MessageMirrorLink, MirrorOrigin

logger = logging.getLogger("tg-bitrix-mirror")


class MirrorStateStore:
    def __init__(self, path: str) -> None:
        self._path = Path(path)

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    async def load_cursor(self, bitrix_dialog_id: str) -> CursorState:
        return await asyncio.to_thread(self._load_cursor_sync, bitrix_dialog_id)

    async def save_cursor(self, bitrix_dialog_id: str, state: CursorState) -> None:
        await asyncio.to_thread(self._save_cursor_sync, bitrix_dialog_id, state)

    async def upsert_link(
        self,
        *,
        telegram_chat_id: int,
        telegram_message_id: int,
        bitrix_message_id: int,
        origin: MirrorOrigin,
        telegram_message_date_unix: Optional[int],
        bitrix_author_id: Optional[int],
        last_seen_bitrix_revision: str,
    ) -> None:
        await asyncio.to_thread(
            self._upsert_link_sync,
            telegram_chat_id,
            telegram_message_id,
            bitrix_message_id,
            origin,
            telegram_message_date_unix,
            bitrix_author_id,
            last_seen_bitrix_revision,
        )

    async def get_link_by_telegram_message(
        self,
        *,
        telegram_chat_id: int,
        telegram_message_id: int,
    ) -> Optional[MessageMirrorLink]:
        return await asyncio.to_thread(
            self._get_link_by_telegram_message_sync,
            telegram_chat_id,
            telegram_message_id,
        )

    async def get_link_by_bitrix_message(self, *, bitrix_message_id: int) -> Optional[MessageMirrorLink]:
        return await asyncio.to_thread(self._get_link_by_bitrix_message_sync, bitrix_message_id)

    async def delete_link_by_bitrix_message(self, *, bitrix_message_id: int) -> None:
        await asyncio.to_thread(self._delete_link_by_bitrix_message_sync, bitrix_message_id)

    async def delete_links_by_telegram_chat(self, *, telegram_chat_id: int) -> None:
        await asyncio.to_thread(self._delete_links_by_telegram_chat_sync, telegram_chat_id)

    async def update_reaction_state(
        self,
        *,
        bitrix_message_id: int,
        bitrix_liked_by_bot: bool,
        last_seen_bitrix_likes: str,
    ) -> None:
        await asyncio.to_thread(
            self._update_reaction_state_sync,
            bitrix_message_id,
            bitrix_liked_by_bot,
            last_seen_bitrix_likes,
        )

    async def cleanup_old_links(self, max_age_seconds: int = 7 * 24 * 3600) -> int:
        """Delete message_links older than max_age_seconds. Returns count of deleted rows."""
        return await asyncio.to_thread(self._cleanup_old_links_sync, max_age_seconds)

    def _initialize_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")

            # --- cursor_state table (per-dialog) ---
            cursor_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(cursor_state)").fetchall()
            }
            if not cursor_columns:
                connection.execute(
                    """
                    CREATE TABLE cursor_state (
                        bitrix_dialog_id TEXT PRIMARY KEY,
                        last_seen_bitrix_message_id INTEGER
                    )
                    """
                )
            elif "singleton_key" in cursor_columns:
                # Migrate from old singleton cursor to per-dialog cursor
                logger.warning("Migrating cursor_state from singleton to per-dialog schema")
                old_row = connection.execute(
                    "SELECT last_seen_bitrix_message_id FROM cursor_state WHERE singleton_key = 1"
                ).fetchone()
                old_cursor = old_row[0] if old_row and old_row[0] is not None else None
                connection.execute("DROP TABLE cursor_state")
                connection.execute(
                    """
                    CREATE TABLE cursor_state (
                        bitrix_dialog_id TEXT PRIMARY KEY,
                        last_seen_bitrix_message_id INTEGER
                    )
                    """
                )
                # The old cursor will be adopted by the first mapping during startup
                if old_cursor is not None:
                    connection.execute(
                        "INSERT INTO cursor_state(bitrix_dialog_id, last_seen_bitrix_message_id) VALUES('__legacy__', ?)",
                        (old_cursor,),
                    )

            existing_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(message_links)").fetchall()
            }
            if not existing_columns:
                connection.execute(
                    """
                    CREATE TABLE message_links (
                        telegram_chat_id INTEGER NOT NULL,
                        telegram_message_id INTEGER NOT NULL,
                        bitrix_message_id INTEGER NOT NULL UNIQUE,
                        origin TEXT NOT NULL,
                        telegram_message_date_unix INTEGER,
                        bitrix_author_id INTEGER,
                        last_seen_bitrix_revision TEXT NOT NULL,
                        created_at_unix INTEGER NOT NULL,
                        updated_at_unix INTEGER NOT NULL,
                        bitrix_liked_by_bot INTEGER DEFAULT 0,
                        last_seen_bitrix_likes TEXT DEFAULT '',
                        PRIMARY KEY (telegram_chat_id, telegram_message_id)
                    )
                    """
                )
            elif "last_seen_bitrix_deleted" in existing_columns:
                logger.warning("Migrating SQLite schema: removing obsolete last_seen_bitrix_deleted column")
                connection.execute("ALTER TABLE message_links RENAME TO message_links_legacy")
                connection.execute(
                    """
                    CREATE TABLE message_links (
                        telegram_chat_id INTEGER NOT NULL,
                        telegram_message_id INTEGER NOT NULL,
                        bitrix_message_id INTEGER NOT NULL UNIQUE,
                        origin TEXT NOT NULL,
                        telegram_message_date_unix INTEGER,
                        bitrix_author_id INTEGER,
                        last_seen_bitrix_revision TEXT NOT NULL,
                        created_at_unix INTEGER NOT NULL,
                        updated_at_unix INTEGER NOT NULL,
                        bitrix_liked_by_bot INTEGER DEFAULT 0,
                        last_seen_bitrix_likes TEXT DEFAULT '',
                        PRIMARY KEY (telegram_chat_id, telegram_message_id)
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO message_links (
                        telegram_chat_id,
                        telegram_message_id,
                        bitrix_message_id,
                        origin,
                        telegram_message_date_unix,
                        bitrix_author_id,
                        last_seen_bitrix_revision,
                        created_at_unix,
                        updated_at_unix
                    )
                    SELECT telegram_chat_id, telegram_message_id, bitrix_message_id, origin,
                           telegram_message_date_unix, bitrix_author_id, last_seen_bitrix_revision,
                           created_at_unix, updated_at_unix
                    FROM message_links_legacy
                    """
                )
                connection.execute("DROP TABLE message_links_legacy")

            current_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(message_links)").fetchall()
            }
            if "bitrix_liked_by_bot" not in current_columns:
                connection.execute("ALTER TABLE message_links ADD COLUMN bitrix_liked_by_bot INTEGER DEFAULT 0")
            if "last_seen_bitrix_likes" not in current_columns:
                connection.execute("ALTER TABLE message_links ADD COLUMN last_seen_bitrix_likes TEXT DEFAULT ''")

            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_message_links_bitrix_message_id ON message_links(bitrix_message_id)"
            )

            # chat_mappings table — managed by the monitoring web dashboard
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_mappings (
                    tg_chat_id       INTEGER PRIMARY KEY,
                    bitrix_dialog_id TEXT NOT NULL,
                    label            TEXT DEFAULT '',
                    created_at_unix  INTEGER NOT NULL
                )
                """
            )
            chat_mapping_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(chat_mappings)").fetchall()
            }
            if "topic_ids" not in chat_mapping_columns:
                connection.execute("ALTER TABLE chat_mappings ADD COLUMN topic_ids TEXT DEFAULT ''")

            connection.commit()

    def _load_cursor_sync(self, bitrix_dialog_id: str) -> CursorState:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT last_seen_bitrix_message_id FROM cursor_state WHERE bitrix_dialog_id = ?",
                (bitrix_dialog_id,),
            ).fetchone()
            if row and isinstance(row[0], int):
                return CursorState(last_seen_bitrix_message_id=row[0])
            # Try adopting the legacy cursor (from migration of old singleton schema)
            legacy_row = connection.execute(
                "SELECT last_seen_bitrix_message_id FROM cursor_state WHERE bitrix_dialog_id = '__legacy__'"
            ).fetchone()
            if legacy_row and isinstance(legacy_row[0], int):
                cursor_value = legacy_row[0]
                connection.execute("DELETE FROM cursor_state WHERE bitrix_dialog_id = '__legacy__'")
                connection.execute(
                    "INSERT INTO cursor_state(bitrix_dialog_id, last_seen_bitrix_message_id) VALUES(?, ?)",
                    (bitrix_dialog_id, cursor_value),
                )
                connection.commit()
                logger.info("Adopted legacy cursor %s for dialog %s", cursor_value, bitrix_dialog_id)
                return CursorState(last_seen_bitrix_message_id=cursor_value)
        return CursorState(last_seen_bitrix_message_id=None)

    def _save_cursor_sync(self, bitrix_dialog_id: str, state: CursorState) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO cursor_state(bitrix_dialog_id, last_seen_bitrix_message_id)
                VALUES(?, ?)
                ON CONFLICT(bitrix_dialog_id) DO UPDATE SET
                    last_seen_bitrix_message_id = excluded.last_seen_bitrix_message_id
                """,
                (bitrix_dialog_id, state.last_seen_bitrix_message_id),
            )
            connection.commit()

    def _upsert_link_sync(
        self,
        telegram_chat_id: int,
        telegram_message_id: int,
        bitrix_message_id: int,
        origin: MirrorOrigin,
        telegram_message_date_unix: Optional[int],
        bitrix_author_id: Optional[int],
        last_seen_bitrix_revision: str,
    ) -> None:
        now = int(time.time())
        with self._connect() as connection:
            existing_by_bitrix = connection.execute(
                "SELECT telegram_chat_id, telegram_message_id FROM message_links WHERE bitrix_message_id = ?",
                (bitrix_message_id,),
            ).fetchone()

            if existing_by_bitrix is not None and (
                int(existing_by_bitrix[0]) != telegram_chat_id or int(existing_by_bitrix[1]) != telegram_message_id
            ):
                logger.warning(
                    "Replacing existing message link for bitrix_message_id=%s from telegram=(%s,%s) to telegram=(%s,%s)",
                    bitrix_message_id,
                    existing_by_bitrix[0],
                    existing_by_bitrix[1],
                    telegram_chat_id,
                    telegram_message_id,
                )
                connection.execute("DELETE FROM message_links WHERE bitrix_message_id = ?", (bitrix_message_id,))

            connection.execute(
                """
                INSERT INTO message_links (
                    telegram_chat_id,
                    telegram_message_id,
                    bitrix_message_id,
                    origin,
                    telegram_message_date_unix,
                    bitrix_author_id,
                    last_seen_bitrix_revision,
                    created_at_unix,
                    updated_at_unix
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_chat_id, telegram_message_id) DO UPDATE SET
                    bitrix_message_id = excluded.bitrix_message_id,
                    origin = excluded.origin,
                    telegram_message_date_unix = excluded.telegram_message_date_unix,
                    bitrix_author_id = excluded.bitrix_author_id,
                    last_seen_bitrix_revision = excluded.last_seen_bitrix_revision,
                    updated_at_unix = excluded.updated_at_unix
                """,
                (
                    telegram_chat_id,
                    telegram_message_id,
                    bitrix_message_id,
                    origin.value,
                    telegram_message_date_unix,
                    bitrix_author_id,
                    last_seen_bitrix_revision,
                    now,
                    now,
                ),
            )
            connection.commit()

    def _get_link_by_telegram_message_sync(
        self,
        telegram_chat_id: int,
        telegram_message_id: int,
    ) -> Optional[MessageMirrorLink]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT telegram_chat_id, telegram_message_id, bitrix_message_id, origin,
                       telegram_message_date_unix, bitrix_author_id, last_seen_bitrix_revision,
                       created_at_unix, updated_at_unix, bitrix_liked_by_bot, last_seen_bitrix_likes
                FROM message_links
                WHERE telegram_chat_id = ? AND telegram_message_id = ?
                """,
                (telegram_chat_id, telegram_message_id),
            ).fetchone()
        return self._row_to_link(row)

    def _get_link_by_bitrix_message_sync(self, bitrix_message_id: int) -> Optional[MessageMirrorLink]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT telegram_chat_id, telegram_message_id, bitrix_message_id, origin,
                       telegram_message_date_unix, bitrix_author_id, last_seen_bitrix_revision,
                       created_at_unix, updated_at_unix, bitrix_liked_by_bot, last_seen_bitrix_likes
                FROM message_links
                WHERE bitrix_message_id = ?
                """,
                (bitrix_message_id,),
            ).fetchone()
        return self._row_to_link(row)

    def _delete_link_by_bitrix_message_sync(self, bitrix_message_id: int) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM message_links WHERE bitrix_message_id = ?", (bitrix_message_id,))
            connection.commit()

    def _delete_links_by_telegram_chat_sync(self, telegram_chat_id: int) -> None:
        with self._connect() as connection:
            deleted = connection.execute(
                "DELETE FROM message_links WHERE telegram_chat_id = ?",
                (telegram_chat_id,),
            ).rowcount
            connection.commit()
        if deleted:
            logger.warning(
                "Deleted %s stale message link(s) for migrated/obsolete telegram_chat_id=%s",
                deleted,
                telegram_chat_id,
            )

    def _update_reaction_state_sync(
        self,
        bitrix_message_id: int,
        bitrix_liked_by_bot: bool,
        last_seen_bitrix_likes: str,
    ) -> None:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE message_links
                SET bitrix_liked_by_bot = ?,
                    last_seen_bitrix_likes = ?,
                    updated_at_unix = ?
                WHERE bitrix_message_id = ?
                """,
                (int(bitrix_liked_by_bot), last_seen_bitrix_likes, now, bitrix_message_id),
            )
            connection.commit()

    def _cleanup_old_links_sync(self, max_age_seconds: int) -> int:
        cutoff = int(time.time()) - max_age_seconds
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM message_links WHERE created_at_unix < ?",
                (cutoff,),
            )
            deleted = cursor.rowcount
            connection.commit()
        if deleted:
            logger.info("Cleaned up %s old message link(s) older than %s seconds", deleted, max_age_seconds)
        return deleted

    def _row_to_link(self, row: Optional[sqlite3.Row]) -> Optional[MessageMirrorLink]:
        if row is None:
            return None
        return MessageMirrorLink(
            telegram_chat_id=int(row[0]),
            telegram_message_id=int(row[1]),
            bitrix_message_id=int(row[2]),
            origin=MirrorOrigin(str(row[3])),
            telegram_message_date_unix=int(row[4]) if row[4] is not None else None,
            bitrix_author_id=int(row[5]) if row[5] is not None else None,
            last_seen_bitrix_revision=str(row[6]),
            created_at_unix=int(row[7]),
            updated_at_unix=int(row[8]),
            bitrix_liked_by_bot=bool(row[9]) if row[9] is not None else False,
            last_seen_bitrix_likes=str(row[10]) if row[10] is not None else "",
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        return connection
