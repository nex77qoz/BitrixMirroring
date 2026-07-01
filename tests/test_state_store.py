from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing

from mirror_state_store import MirrorStateStore
from models import MirrorOrigin


class MirrorStateStoreTestCase(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tempdir.name, 'state.sqlite3')
        self.store = MirrorStateStore(self.db_path)
        await self.store.initialize()

    async def asyncTearDown(self) -> None:
        await asyncio.sleep(0.05)
        self.tempdir.cleanup()

    async def test_forwarding_enabled_roundtrip(self) -> None:
        self.assertTrue(await self.store.get_forwarding_enabled())
        await self.store.set_forwarding_enabled(False)
        self.assertFalse(await self.store.get_forwarding_enabled())
        await self.store.set_forwarding_enabled(True)
        self.assertTrue(await self.store.get_forwarding_enabled())

    async def test_upsert_replace_and_reaction_state(self) -> None:
        await self.store.upsert_link(telegram_chat_id=1, telegram_message_id=2, bitrix_message_id=3, origin=MirrorOrigin.TELEGRAM, telegram_message_date_unix=10, bitrix_author_id=20, last_seen_bitrix_revision='rev1', telegram_message_thread_id=300)
        await self.store.update_reaction_state(bitrix_message_id=3, bitrix_liked_by_bot=True, last_seen_bitrix_likes='1,2')
        link = await self.store.get_link_by_telegram_message(telegram_chat_id=1, telegram_message_id=2)
        self.assertIsNotNone(link)
        assert link is not None
        self.assertEqual(link.bitrix_message_id, 3)
        self.assertTrue(link.bitrix_liked_by_bot)
        self.assertEqual(link.last_seen_bitrix_likes, '1,2')
        self.assertEqual(link.telegram_message_thread_id, 300)
        await self.store.upsert_link(telegram_chat_id=4, telegram_message_id=5, bitrix_message_id=3, origin=MirrorOrigin.BITRIX, telegram_message_date_unix=None, bitrix_author_id=22, last_seen_bitrix_revision='rev2')
        replaced = await self.store.get_link_by_bitrix_message(bitrix_message_id=3)
        self.assertEqual((replaced.telegram_chat_id, replaced.telegram_message_id), (4, 5))

    async def test_cleanup_and_topic_names(self) -> None:
        await self.store.upsert_link(telegram_chat_id=1, telegram_message_id=2, bitrix_message_id=3, origin=MirrorOrigin.TELEGRAM, telegram_message_date_unix=None, bitrix_author_id=None, last_seen_bitrix_revision='rev')
        with closing(sqlite3.connect(self.db_path)) as connection:
            old_timestamp = int(time.time()) - 100
            connection.execute('UPDATE message_links SET created_at_unix = ?, updated_at_unix = ? WHERE bitrix_message_id = ?', (old_timestamp, old_timestamp, 3))
            connection.commit()
        deleted = await self.store.cleanup_old_links(max_age_seconds=1)
        self.assertEqual(deleted, 1)
        await self.store.save_topic_name(100, 200, 'Topic A')
        topics = await self.store.load_topic_names()
        self.assertEqual(topics[100, 200], 'Topic A')

    async def test_is_admin_returns_false_when_empty(self) -> None:
        result = await self.store.is_admin(999)
        self.assertFalse(result)

    async def test_is_admin_true_after_insert(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute('INSERT INTO telegram_admins (tg_user_id, added_at_unix) VALUES (42, 1000)')
        conn.commit()
        conn.close()
        self.assertTrue(await self.store.is_admin(42))
        self.assertFalse(await self.store.is_admin(99))

    async def test_load_all_chat_mappings_empty(self) -> None:
        mappings = await self.store.load_all_chat_mappings()
        self.assertEqual(mappings, ())

    async def test_add_and_load_chat_mapping(self) -> None:
        mapping_id = await self.store.add_chat_mapping(tg_chat_id=-100123, bitrix_dialog_id='chat999', topic_ids=[7, 8], label='test label')
        self.assertIsInstance(mapping_id, int)
        mappings = await self.store.load_all_chat_mappings()
        self.assertEqual(len(mappings), 1)
        m = mappings[0]
        self.assertEqual(m.tg_chat_id, -100123)
        self.assertEqual(m.bitrix_dialog_id, 'chat999')
        self.assertEqual(m.topic_ids, (7, 8))
        self.assertEqual(m.label, 'test label')

    async def test_add_chat_mapping_duplicate_bitrix_id_raises(self) -> None:
        await self.store.add_chat_mapping(-100123, 'chat999', [], 'first')
        with self.assertRaises(ValueError):
            await self.store.add_chat_mapping(-100456, 'chat999', [], 'second')

    async def test_remove_chat_mapping(self) -> None:
        mapping_id = await self.store.add_chat_mapping(-100123, 'chat999', [], '')
        removed = await self.store.remove_chat_mapping(mapping_id)
        self.assertTrue(removed)
        self.assertEqual(await self.store.load_all_chat_mappings(), ())

    async def test_remove_nonexistent_mapping_returns_false(self) -> None:
        removed = await self.store.remove_chat_mapping(9999)
        self.assertFalse(removed)

    async def test_pending_connections_flow(self):
        token = 'testtoken123'
        expires_at = int(time.time()) + 600
        await self.store.save_pending_connection('chat123', token, expires_at)
        is_valid = await self.store.verify_and_consume_token('chat123', token)
        self.assertTrue(is_valid)
        is_valid_again = await self.store.verify_and_consume_token('chat123', token)
        self.assertFalse(is_valid_again)
        expired_token = 'expired456'
        await self.store.save_pending_connection('chat123', expired_token, int(time.time()) - 10)
        is_expired_valid = await self.store.verify_and_consume_token('chat123', expired_token)
        self.assertFalse(is_expired_valid)

    async def test_cleanup_preserves_recently_updated_links(self) -> None:
        """Links updated recently must survive cleanup even if created long ago (#5)."""
        await self.store.upsert_link(telegram_chat_id=10, telegram_message_id=20, bitrix_message_id=100, origin=MirrorOrigin.BITRIX, telegram_message_date_unix=None, bitrix_author_id=7, last_seen_bitrix_revision='abc')
        old_created = int(time.time()) - 8 * 86400
        recent_update = int(time.time()) - 60
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute('UPDATE message_links SET created_at_unix = ?, updated_at_unix = ? WHERE bitrix_message_id = ?', (old_created, recent_update, 100))
            connection.commit()
        deleted = await self.store.cleanup_old_links(max_age_seconds=7 * 86400)
        self.assertEqual(deleted, 0, 'Recently updated link must survive cleanup')
        link = await self.store.get_link_by_bitrix_message(bitrix_message_id=100)
        self.assertIsNotNone(link, 'Recently updated link must still exist after cleanup')

    async def test_cleanup_removes_stale_updated_links(self) -> None:
        """Links with both old created_at and old updated_at must be deleted."""
        await self.store.upsert_link(telegram_chat_id=10, telegram_message_id=21, bitrix_message_id=101, origin=MirrorOrigin.TELEGRAM, telegram_message_date_unix=None, bitrix_author_id=None, last_seen_bitrix_revision='rev')
        old_stale = int(time.time()) - 8 * 86400
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute('UPDATE message_links SET created_at_unix = ?, updated_at_unix = ? WHERE bitrix_message_id = ?', (old_stale, old_stale, 101))
            connection.commit()
        deleted = await self.store.cleanup_old_links(max_age_seconds=7 * 86400)
        self.assertEqual(deleted, 1, 'Stale link must be deleted')
        link = await self.store.get_link_by_bitrix_message(bitrix_message_id=101)
        self.assertIsNone(link, 'Deleted link must be absent')

    async def test_concurrent_token_consumption_is_atomic(self) -> None:
        """Two simultaneous calls must return exactly one True."""
        token = 'racetoken'
        expires_at = int(time.time()) + 600
        await self.store.save_pending_connection('chat42', token, expires_at)
        results = await asyncio.gather(self.store.verify_and_consume_token('chat42', token), self.store.verify_and_consume_token('chat42', token))
        self.assertEqual(sum(results), 1, 'Exactly one concurrent call should consume the token')

    async def test_initialize_deduplicates_existing_mappings(self) -> None:
        db_path = os.path.join(self.tempdir.name, 'state_migration.sqlite3')
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute("\n                CREATE TABLE chat_mappings (\n                    id               INTEGER PRIMARY KEY AUTOINCREMENT,\n                    tg_chat_id       INTEGER NOT NULL,\n                    bitrix_dialog_id TEXT NOT NULL,\n                    label            TEXT DEFAULT '',\n                    created_at_unix  INTEGER NOT NULL,\n                    topic_ids        TEXT DEFAULT ''\n                )\n                ")
            conn.execute('INSERT INTO chat_mappings (tg_chat_id, bitrix_dialog_id, label, created_at_unix, topic_ids) VALUES (?, ?, ?, ?, ?)', (-100123, 'chat999', 'first', 1000, '1'))
            conn.execute('INSERT INTO chat_mappings (tg_chat_id, bitrix_dialog_id, label, created_at_unix, topic_ids) VALUES (?, ?, ?, ?, ?)', (-100123, 'chat999', 'second', 2000, '2'))
            conn.execute('INSERT INTO chat_mappings (tg_chat_id, bitrix_dialog_id, label, created_at_unix, topic_ids) VALUES (?, ?, ?, ?, ?)', (-100456, 'chat999', 'different_chat', 3000, '3'))
            conn.commit()
        store = MirrorStateStore(db_path)
        await store.initialize()
        mappings = await store.load_all_chat_mappings()
        self.assertEqual(len(mappings), 1)
        m = mappings[0]
        self.assertEqual(m.tg_chat_id, -100123)
        self.assertEqual(m.bitrix_dialog_id, 'chat999')
        self.assertEqual(m.topic_ids, (1, 2))
        self.assertEqual(m.label, 'first, second')
        with self.assertRaises(ValueError):
            await store.add_chat_mapping(-100456, 'chat999', [], 'third')

    async def test_bitrix_event_offset_is_scoped_by_bot_id(self) -> None:
        self.assertIsNone(await self.store.load_bitrix_event_offset(7))
        await self.store.save_bitrix_event_offset(7, 101)
        await self.store.save_bitrix_event_offset(8, 202)
        self.assertEqual(await self.store.load_bitrix_event_offset(7), 101)
        self.assertEqual(await self.store.load_bitrix_event_offset(8), 202)

    async def test_bitrix_event_offset_rejects_backwards_write(self) -> None:
        await self.store.save_bitrix_event_offset(7, 101)
        await self.store.save_bitrix_event_offset(7, 99)
        self.assertEqual(await self.store.load_bitrix_event_offset(7), 101)
