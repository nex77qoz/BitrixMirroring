from __future__ import annotations

import asyncio
import dataclasses
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from mirror_service import MirrorService, _bbcode_to_html
from models import BitrixDialogSnapshot, BitrixMessage, CursorState, MirrorOrigin
from tests.helpers import make_mapping, make_message, make_settings


class MirrorServiceTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        mapping = make_mapping(topic_ids=(100, 200))
        settings = make_settings(chat_mappings=(mapping,))
        self.bitrix = AsyncMock()
        self.state_store = AsyncMock()
        self.service = MirrorService(settings, self.bitrix, self.state_store)

    async def test_resolve_mapping_prefers_matching_topic(self) -> None:
        mapping = self.service.resolve_mapping_for_chat_and_thread(-1001234567890, 200)
        self.assertIsNotNone(mapping)
        assert mapping is not None
        self.assertEqual(mapping.bitrix_dialog_id, "chat42")

    async def test_get_chat_mappings_returns_current_mappings(self) -> None:
        mappings = self.service.get_chat_mappings()
        self.assertEqual(len(mappings), 1)
        self.assertEqual(mappings[0].bitrix_dialog_id, "chat42")

    async def test_render_telegram_message_includes_topic_and_sender(self) -> None:
        self.service._topic_names[(-1001234567890, 100)] = "Deploy"
        message = make_message(message_thread_id=100, text="hello")
        rendered = self.service.render_telegram_message(message)
        self.assertIn("#Deploy", rendered)
        self.assertIn("Alice Example", rendered)
        self.assertIn("hello", rendered)

    async def test_schedule_bitrix_dialog_sync_creates_single_task(self) -> None:
        gate = asyncio.Event()

        async def fake_sync(application, mapping, *, trigger: str) -> None:
            await gate.wait()

        self.service._application = SimpleNamespace()
        self.service._sync_bitrix_dialog = fake_sync  # type: ignore[method-assign]

        accepted1 = await self.service.schedule_bitrix_dialog_sync("chat42", trigger="webhook", message_id=7, reply_id=8)
        accepted2 = await self.service.schedule_bitrix_dialog_sync("chat42", trigger="webhook")
        self.assertTrue(accepted1)
        self.assertTrue(accepted2)
        self.assertEqual(self.service._webhook_reply_cache[7], 8)
        self.assertEqual(len(self.service._bitrix_on_demand_tasks), 1)

        gate.set()
        await asyncio.gather(*self.service._bitrix_on_demand_tasks.values())
        await asyncio.sleep(0)
        self.assertEqual(self.service._bitrix_on_demand_tasks, {})

    async def test_webhook_primes_missing_cursor_from_message_id(self) -> None:
        seen_cursor: list[int | None] = []

        async def fake_sync(application, mapping, *, trigger: str) -> None:
            seen_cursor.append(self.service._last_seen_bitrix_message_ids.get(mapping.bitrix_dialog_id))

        self.service._application = SimpleNamespace()
        self.service._sync_bitrix_dialog = fake_sync  # type: ignore[method-assign]

        accepted = await self.service.schedule_bitrix_dialog_sync(
            "chat42",
            trigger="webhook",
            message_id=50,
        )

        self.assertTrue(accepted)
        await asyncio.gather(*self.service._bitrix_on_demand_tasks.values())
        await asyncio.sleep(0)
        self.assertEqual(seen_cursor, [49])
        self.state_store.save_cursor.assert_awaited_with(
            "chat42",
            CursorState(last_seen_bitrix_message_id=49),
        )

    async def test_webhook_uses_persisted_cursor_when_memory_empty(self) -> None:
        seen_cursor: list[int | None] = []

        async def fake_sync(application, mapping, *, trigger: str) -> None:
            seen_cursor.append(self.service._last_seen_bitrix_message_ids.get(mapping.bitrix_dialog_id))

        self.state_store.load_cursor.return_value = CursorState(last_seen_bitrix_message_id=80)
        self.service._application = SimpleNamespace()
        self.service._sync_bitrix_dialog = fake_sync  # type: ignore[method-assign]

        accepted = await self.service.schedule_bitrix_dialog_sync(
            "chat42",
            trigger="webhook",
            message_id=50,
        )

        self.assertTrue(accepted)
        await asyncio.gather(*self.service._bitrix_on_demand_tasks.values())
        await asyncio.sleep(0)
        self.assertEqual(seen_cursor, [80])
        self.state_store.save_cursor.assert_not_awaited()

    async def test_forwarding_disabled_rejects_new_work(self) -> None:
        await self.service.set_forwarding_enabled(False)
        self.service._application = SimpleNamespace()

        message = make_message()
        await self.service.enqueue_telegram_message(message)
        accepted = await self.service.schedule_bitrix_dialog_sync("chat42", trigger="webhook")
        await self.service.sync_telegram_edit(message)
        await self.service.sync_telegram_reaction(message.chat_id, message.message_id, True)

        self.assertFalse(accepted)
        self.assertEqual(len(self.service._channel_queues), 0)
        self.bitrix.update_message.assert_not_awaited()
        self.bitrix.set_message_like.assert_not_awaited()
        self.state_store.set_forwarding_enabled.assert_awaited_once_with(False)

    async def test_disabled_bitrix_sync_advances_cursor_without_forwarding(self) -> None:
        mapping = self.service.settings.chat_mappings[0]
        await self.service.set_forwarding_enabled(False)
        self.bitrix.get_latest_message_id.return_value = 42
        application = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))

        await self.service._sync_bitrix_messages(application, mapping)

        application.bot.send_message.assert_not_awaited()
        self.bitrix.get_recent_messages.assert_not_awaited()
        self.bitrix.get_messages_after.assert_not_awaited()
        self.state_store.save_cursor.assert_awaited_with(
            mapping.bitrix_dialog_id,
            CursorState(last_seen_bitrix_message_id=42),
        )

    async def test_sync_telegram_edit_uses_saved_link(self) -> None:
        self.state_store.get_link_by_telegram_message.return_value = SimpleNamespace(bitrix_message_id=99)
        message = make_message(text="edited text")
        await self.service.sync_telegram_edit(message)
        self.bitrix.update_message.assert_awaited_once()

    async def test_sync_telegram_reaction_updates_state(self) -> None:
        self.state_store.get_link_by_telegram_message.return_value = SimpleNamespace(
            bitrix_message_id=99,
            bitrix_liked_by_bot=False,
            last_seen_bitrix_likes="",
        )
        await self.service.sync_telegram_reaction(-1001234567890, 100, True)
        self.bitrix.set_message_like.assert_awaited_once_with(99, liked=True)
        self.state_store.update_reaction_state.assert_awaited_once()

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

    async def test_suppressed_telegram_origin_bitrix_message_advances_cursor(self) -> None:
        mapping = self.service.settings.chat_mappings[0]
        self.service._last_seen_bitrix_message_ids[mapping.bitrix_dialog_id] = 5
        self.bitrix.get_recent_messages.return_value = BitrixDialogSnapshot(
            messages=[],
            users_by_id={},
            files_by_id={},
        )
        self.bitrix.get_messages_after.return_value = BitrixDialogSnapshot(
            messages=[
                BitrixMessage(
                    message_id=10,
                    author_id=123,
                    text="already mirrored from telegram",
                    file_ids=(),
                    update_time_unix=None,
                    like_user_ids=(),
                )
            ],
            users_by_id={},
            files_by_id={},
        )
        self.state_store.get_link_by_bitrix_message.return_value = SimpleNamespace(
            origin=MirrorOrigin.TELEGRAM,
            telegram_message_id=777,
        )
        application = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))

        await self.service._sync_bitrix_messages(application, mapping)

        application.bot.send_message.assert_not_awaited()
        self.state_store.save_cursor.assert_awaited_with(
            mapping.bitrix_dialog_id,
            CursorState(last_seen_bitrix_message_id=10),
        )
        self.assertEqual(self.service._last_seen_bitrix_message_ids[mapping.bitrix_dialog_id], 10)

    async def test_persist_cursor_does_not_move_backwards(self) -> None:
        self.service._last_seen_bitrix_message_ids["chat42"] = 20

        await self.service._persist_cursor("chat42", 10)

        self.state_store.save_cursor.assert_awaited_once_with(
            "chat42",
            CursorState(last_seen_bitrix_message_id=20),
        )
        self.assertEqual(self.service._last_seen_bitrix_message_ids["chat42"], 20)

    async def test_bbcode_to_html_escapes_markup(self) -> None:
        converted = _bbcode_to_html("[b]Hi[/b] <script>")
        self.assertEqual(converted, "<b>Hi</b> &lt;script&gt;")

    async def test_is_admin_delegates_to_state_store(self) -> None:
        self.state_store.is_admin = AsyncMock(return_value=True)
        result = await self.service.is_admin(42)
        self.assertTrue(result)
        self.state_store.is_admin.assert_awaited_once_with(42)

    async def test_reload_mappings_updates_lookup_tables(self) -> None:
        from tests.helpers import make_mapping
        new_mapping = make_mapping(mapping_id=99, tg_chat_id=-9999, bitrix_dialog_id="chat77")
        self.state_store.load_all_chat_mappings = AsyncMock(return_value=(new_mapping,))
        await self.service.reload_mappings()
        self.assertIsNotNone(self.service.get_mapping_for_bitrix_dialog("chat77"))
        self.assertIsNone(self.service.get_mapping_for_bitrix_dialog("chat42"))

    async def test_connect_mapping_adds_and_reloads(self) -> None:
        from tests.helpers import make_mapping
        new_mapping = make_mapping(mapping_id=10, tg_chat_id=-5555, bitrix_dialog_id="chatNEW")
        self.state_store.add_chat_mapping = AsyncMock(return_value=10)
        self.state_store.load_all_chat_mappings = AsyncMock(return_value=(new_mapping,))
        await self.service.connect_mapping(-5555, "chatNEW", None, "")
        self.state_store.add_chat_mapping.assert_awaited_once_with(-5555, "chatNEW", [], "")
        self.assertIsNotNone(self.service.get_mapping_for_bitrix_dialog("chatNEW"))

    async def test_connect_mapping_raises_on_existing_dialog_different_chat(self) -> None:
        with self.assertRaises(ValueError):
            await self.service.connect_mapping(-9999, "chat42", None, "")

    async def test_connect_mapping_same_chat_adds_topic_to_existing(self) -> None:
        from tests.helpers import make_mapping
        updated = make_mapping(mapping_id=1, tg_chat_id=-1001234567890, bitrix_dialog_id="chat42", topic_ids=(100, 200, 55))
        self.state_store.update_chat_mapping_topic_ids = AsyncMock()
        self.state_store.load_all_chat_mappings = AsyncMock(return_value=(updated,))
        await self.service.connect_mapping(-1001234567890, "chat42", 55, "")
        self.state_store.update_chat_mapping_topic_ids.assert_awaited_once_with(1, [100, 200, 55])
        self.state_store.add_chat_mapping.assert_not_awaited()

    async def test_disconnect_mapping_removes_and_reloads(self) -> None:
        from tests.helpers import make_mapping
        self.state_store.remove_chat_mapping = AsyncMock(return_value=True)
        self.state_store.load_all_chat_mappings = AsyncMock(return_value=())
        removed = await self.service.disconnect_mapping(-1001234567890, None)
        self.assertTrue(removed)
        self.state_store.remove_chat_mapping.assert_awaited_once_with(1)

    async def test_disconnect_mapping_returns_false_when_not_found(self) -> None:
        removed = await self.service.disconnect_mapping(-9999999, None)
        self.assertFalse(removed)

    async def test_per_channel_queue_throttling_and_overflow(self) -> None:
        from unittest.mock import patch
        
        loop = asyncio.get_event_loop()
        original_time = loop.time
        original_sleep = asyncio.sleep
        
        loop_time = 10.0
        def get_time():
            return loop_time
        
        loop.time = get_time
        
        sleep_calls = []
        async def fake_sleep(delay):
            nonlocal loop_time
            sleep_calls.append(delay)
            loop_time += delay
            await original_sleep(0)

        self.service._forwarding_enabled = True
        self.service._stop_event.clear()

        try:
            with patch("asyncio.sleep", side_effect=fake_sleep):
                # 1. Test throttling on a single channel
                msg1 = make_message(chat_id=-1001234567890, message_id=1)
                msg2 = make_message(chat_id=-1001234567890, message_id=2)
                msg3 = make_message(chat_id=-1001234567890, message_id=3)
                
                await self.service.enqueue_telegram_message(msg1)
                await self.service.enqueue_telegram_message(msg2)
                await self.service.enqueue_telegram_message(msg3)

                queue = self.service._channel_queues[-1001234567890]
                
                # Let loop process
                for _ in range(20):
                    if queue.empty():
                        break
                    await asyncio.sleep(0)
                
                throttling_sleeps = [d for d in sleep_calls if d > 0]
                self.assertEqual(len(throttling_sleeps), 2)
                self.assertAlmostEqual(throttling_sleeps[0], 0.2)
                self.assertAlmostEqual(throttling_sleeps[1], 0.2)

            # 2. Test overflow: overflowing configured maxsize messages drops subsequent messages
            # Patch asyncio.create_task to avoid running background workers for these overflow tests
            with patch("asyncio.create_task") as mock_create_task, \
                 patch.object(self.service, "resolve_mapping_for_telegram_message", return_value=make_mapping()):
                mock_create_task.return_value = AsyncMock()
                
                chat_id_overflow = 9999
                max_size = self.service.settings.bitrix_send_queue_maxsize
                for i in range(max_size + 5):
                    msg = make_message(chat_id=chat_id_overflow, message_id=100 + i)
                    await self.service.enqueue_telegram_message(msg)
                
                # Channel 1 queue has max size, and is full. Next 5 messages are dropped.
                self.assertEqual(self.service._channel_queues[chat_id_overflow].qsize(), max_size)
                
                # Other channel is unaffected
                chat_id_other = 8888
                msg_other = make_message(chat_id=chat_id_other, message_id=9999)
                await self.service.enqueue_telegram_message(msg_other)
                self.assertEqual(self.service._channel_queues[chat_id_other].qsize(), 1)
        finally:
            loop.time = original_time
            self.service._channel_workers.pop(9999, None)
            self.service._channel_workers.pop(8888, None)
            await self.service.stop()

    async def test_reload_mappings_cancels_obsolete_workers(self) -> None:
        from tests.helpers import make_mapping
        chat_id = -1001234567890
        dummy_task = AsyncMock(spec=asyncio.Task)
        self.service._channel_workers[chat_id] = dummy_task
        self.service._channel_queues[chat_id] = asyncio.Queue()

        new_mapping = make_mapping(mapping_id=99, tg_chat_id=-9999, bitrix_dialog_id="chat77")
        self.state_store.load_all_chat_mappings = AsyncMock(return_value=(new_mapping,))
        
        await self.service.reload_mappings()
        
        dummy_task.cancel.assert_called_once()
        self.assertNotIn(chat_id, self.service._channel_workers)
        self.assertNotIn(chat_id, self.service._channel_queues)

    async def test_poll_scheduler_concurrency(self) -> None:
        from tests.helpers import make_mapping, make_settings
        # Create 10 mappings
        mappings = [
            make_mapping(mapping_id=i, tg_chat_id=-100123456000 - i, bitrix_dialog_id=f"chat{i}")
            for i in range(10)
        ]
        settings = make_settings(chat_mappings=tuple(mappings))
        import dataclasses
        settings = dataclasses.replace(settings, sync_bitrix_to_telegram=True)
        
        # Instantiate service
        service = MirrorService(settings, self.bitrix, self.state_store)
        
        # Track concurrency
        active_concurrency = 0
        max_concurrency = 0
        completed_polls = 0
        concurrency_lock = asyncio.Lock()
        done_event = asyncio.Event()

        async def fake_sync_dialog(application, mapping, *, trigger):
            nonlocal active_concurrency, max_concurrency, completed_polls
            async with concurrency_lock:
                active_concurrency += 1
                if active_concurrency > max_concurrency:
                    max_concurrency = active_concurrency
            
            # Sleep a bit to force concurrent execution
            await asyncio.sleep(0.01)

            async with concurrency_lock:
                active_concurrency -= 1
                completed_polls += 1
                if completed_polls == 10:
                    done_event.set()

        async def fake_initialize_cursor(mapping):
            service._last_seen_bitrix_message_ids[mapping.bitrix_dialog_id] = 1

        service._sync_bitrix_dialog = fake_sync_dialog  # type: ignore[method-assign]
        service._initialize_bitrix_cursor = fake_initialize_cursor  # type: ignore[method-assign]

        # Start polling
        app = SimpleNamespace()
        await service.start_bitrix_polling(app)  # type: ignore[arg-type]

        # Wait for all 10 to finish
        try:
            await asyncio.wait_for(done_event.wait(), timeout=2.0)
        finally:
            await service.stop()

        # Verify no more than 5 ran concurrently
        self.assertEqual(completed_polls, 10)
        self.assertEqual(max_concurrency, 5)

    async def test_reload_mappings_starts_and_stops_scheduler_dynamically(self) -> None:
        from tests.helpers import make_mapping, make_settings
        # 1. Start with zero mappings
        settings = make_settings(chat_mappings=())
        import dataclasses
        settings = dataclasses.replace(settings, sync_bitrix_to_telegram=True)
        
        service = MirrorService(settings, self.bitrix, self.state_store)
        app = SimpleNamespace()
        
        # Start service
        await service.start(app)  # type: ignore[arg-type]
        
        # Verify scheduler task is None because mappings was empty
        self.assertIsNone(service._scheduler_task)
        
        # Mock load_all_chat_mappings to return a mapping
        new_mapping = make_mapping(mapping_id=1, tg_chat_id=-1001234567890, bitrix_dialog_id="chat42")
        self.state_store.load_all_chat_mappings = AsyncMock(return_value=(new_mapping,))
        
        # Reload mappings (simulates /connect)
        await service.reload_mappings()
        
        # Verify scheduler task has been started dynamically!
        self.assertIsNotNone(service._scheduler_task)
        self.assertFalse(service._scheduler_task.done())
        
        # Now change load_all_chat_mappings to return empty tuple (simulates disconnecting all mappings)
        self.state_store.load_all_chat_mappings = AsyncMock(return_value=())
        
        # Reload mappings again
        await service.reload_mappings()
        
        # Verify scheduler task has been cancelled and cleaned up to None!
        self.assertIsNone(service._scheduler_task)
        
        # Cleanup
        await service.stop()

    async def test_should_forward_bitrix_message_ignores_own_bot(self) -> None:
        self.service.settings = dataclasses.replace(self.service.settings, bitrix_bot_id=999)
        msg_from_bot = BitrixMessage(
            message_id=15,
            author_id=999,
            text="hello from bot",
            file_ids=(),
            update_time_unix=None,
            like_user_ids=(),
            reply_id=None,
            is_sticker=False,
            is_meeting=False,
            is_task=False,
        )
        should_forward = await self.service._should_forward_bitrix_message("chat42", msg_from_bot)
        self.assertFalse(should_forward)

    async def test_enqueue_telegram_message_uses_configured_queue_maxsize(self) -> None:
        self.service.settings = dataclasses.replace(self.service.settings, bitrix_send_queue_maxsize=555)
        self.service._forwarding_enabled = True
        
        message = make_message(text="test queue size")
        await self.service.enqueue_telegram_message(message)
        
        # Get the created queue for the chat
        queue = self.service._channel_queues[message.chat_id]
        self.assertEqual(queue.maxsize, 555)
        
        # Cancel the worker task to prevent background task leaks in tests
        if message.chat_id in self.service._channel_workers:
            self.service._channel_workers[message.chat_id].cancel()




