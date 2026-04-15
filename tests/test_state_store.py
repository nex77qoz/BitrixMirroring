from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
import asyncio
from contextlib import closing

from mirror_state_store import MirrorStateStore
from models import CursorState, MirrorOrigin


class MirrorStateStoreTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tempdir.name, "state.sqlite3")
        self.store = MirrorStateStore(self.db_path)
        await self.store.initialize()

    async def asyncTearDown(self) -> None:
        await asyncio.sleep(0.05)
        self.tempdir.cleanup()

    async def test_cursor_roundtrip(self) -> None:
        await self.store.save_cursor("chat42", CursorState(last_seen_bitrix_message_id=99))
        state = await self.store.load_cursor("chat42")
        self.assertEqual(state.last_seen_bitrix_message_id, 99)

    async def test_upsert_replace_and_reaction_state(self) -> None:
        await self.store.upsert_link(
            telegram_chat_id=1,
            telegram_message_id=2,
            bitrix_message_id=3,
            origin=MirrorOrigin.TELEGRAM,
            telegram_message_date_unix=10,
            bitrix_author_id=20,
            last_seen_bitrix_revision="rev1",
            telegram_message_thread_id=300,
        )
        await self.store.update_reaction_state(
            bitrix_message_id=3,
            bitrix_liked_by_bot=True,
            last_seen_bitrix_likes="1,2",
        )
        link = await self.store.get_link_by_telegram_message(telegram_chat_id=1, telegram_message_id=2)
        self.assertIsNotNone(link)
        assert link is not None
        self.assertEqual(link.bitrix_message_id, 3)
        self.assertTrue(link.bitrix_liked_by_bot)
        self.assertEqual(link.last_seen_bitrix_likes, "1,2")
        self.assertEqual(link.telegram_message_thread_id, 300)

        await self.store.upsert_link(
            telegram_chat_id=4,
            telegram_message_id=5,
            bitrix_message_id=3,
            origin=MirrorOrigin.BITRIX,
            telegram_message_date_unix=None,
            bitrix_author_id=22,
            last_seen_bitrix_revision="rev2",
        )
        replaced = await self.store.get_link_by_bitrix_message(bitrix_message_id=3)
        self.assertEqual((replaced.telegram_chat_id, replaced.telegram_message_id), (4, 5))

    async def test_cleanup_and_topic_names(self) -> None:
        await self.store.upsert_link(
            telegram_chat_id=1,
            telegram_message_id=2,
            bitrix_message_id=3,
            origin=MirrorOrigin.TELEGRAM,
            telegram_message_date_unix=None,
            bitrix_author_id=None,
            last_seen_bitrix_revision="rev",
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            old_timestamp = int(time.time()) - 100
            connection.execute(
                "UPDATE message_links SET created_at_unix = ?, updated_at_unix = ? WHERE bitrix_message_id = ?",
                (old_timestamp, old_timestamp, 3),
            )
            connection.commit()
        deleted = await self.store.cleanup_old_links(max_age_seconds=1)
        self.assertEqual(deleted, 1)

        await self.store.save_topic_name(100, 200, "Topic A")
        topics = await self.store.load_topic_names()
        self.assertEqual(topics[(100, 200)], "Topic A")
