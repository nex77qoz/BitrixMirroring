from __future__ import annotations

import asyncio
import dataclasses
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.error import BadRequest

from mirror_service import MirrorService, _bbcode_to_html
from models import BitrixEventPage, BitrixMessage, MessageMirrorLink, MirrorOrigin
from tests.helpers import make_bitrix_event, make_mapping, make_message, make_settings


class MirrorServiceTestCase(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        mapping = make_mapping(topic_ids=(100, 200))
        settings = make_settings(chat_mappings=(mapping,))
        self.bitrix = AsyncMock()
        self.state_store = AsyncMock()
        self.state_store.get_link_by_bitrix_message.return_value = None
        self.service = MirrorService(settings, self.bitrix, self.state_store)

    async def test_resolve_mapping_prefers_matching_topic(self) -> None:
        mapping = self.service.resolve_mapping_for_chat_and_thread(-1001234567890, 200)
        self.assertIsNotNone(mapping)
        assert mapping is not None
        self.assertEqual(mapping.bitrix_dialog_id, 'chat42')

    async def test_get_chat_mappings_returns_current_mappings(self) -> None:
        mappings = self.service.get_chat_mappings()
        self.assertEqual(len(mappings), 1)
        self.assertEqual(mappings[0].bitrix_dialog_id, 'chat42')

    async def test_render_telegram_message_includes_topic_and_sender(self) -> None:
        self.service._topic_names[-1001234567890, 100] = 'Deploy'
        message = make_message(message_thread_id=100, text='hello')
        rendered = self.service.render_telegram_message(message)
        self.assertIn('#Deploy', rendered)
        self.assertIn('Alice Example', rendered)
        self.assertIn('hello', rendered)

    async def test_forwarding_disabled_rejects_new_work(self) -> None:
        await self.service.set_forwarding_enabled(False)
        self.service._application = SimpleNamespace()
        message = make_message()
        await self.service.enqueue_telegram_message(message)
        await self.service.sync_telegram_edit(message)
        await self.service.sync_telegram_reaction(message.chat_id, message.message_id, True)
        self.assertEqual(len(self.service._channel_queues), 0)
        self.bitrix.update_message.assert_not_awaited()
        self.bitrix.set_message_like.assert_not_awaited()
        self.state_store.set_forwarding_enabled.assert_awaited_once_with(False)

    async def test_sync_telegram_edit_uses_saved_link(self) -> None:
        self.state_store.get_link_by_telegram_message.return_value = SimpleNamespace(bitrix_message_id=99)
        message = make_message(text='edited text')
        await self.service.sync_telegram_edit(message)
        self.bitrix.update_message.assert_awaited_once()

    async def test_sync_telegram_reaction_updates_state(self) -> None:
        self.state_store.get_link_by_telegram_message.return_value = SimpleNamespace(bitrix_message_id=99, bitrix_liked_by_bot=False, last_seen_bitrix_likes='')
        await self.service.sync_telegram_reaction(-1001234567890, 100, True)
        self.bitrix.set_message_like.assert_awaited_once_with(99, liked=True)
        self.state_store.update_reaction_state.assert_awaited_once()

    async def test_sync_telegram_reaction_removal_updates_state_correctly(self) -> None:
        self.state_store.get_link_by_telegram_message.return_value = SimpleNamespace(bitrix_message_id=99, bitrix_liked_by_bot=True, last_seen_bitrix_likes='123')
        await self.service.sync_telegram_reaction(-1001234567890, 100, False)
        self.bitrix.set_message_like.assert_awaited_once_with(99, liked=False)
        self.state_store.update_reaction_state.assert_awaited_once_with(bitrix_message_id=99, bitrix_liked_by_bot=False, last_seen_bitrix_likes='123')

    async def test_bbcode_to_html_escapes_markup(self) -> None:
        converted = _bbcode_to_html('[b]Hi[/b] <script>')
        self.assertEqual(converted, '<b>Hi</b> &lt;script&gt;')

    async def test_bbcode_plain_url(self) -> None:
        url = 'https://www.technoavia.ru/closed-area/corp_universitet/siz_pechat'
        converted = _bbcode_to_html(f'[URL]{url}[/URL]')
        self.assertEqual(converted, f'<a href="{url}">{url}</a>')

    async def test_is_admin_delegates_to_state_store(self) -> None:
        self.state_store.is_admin = AsyncMock(return_value=True)
        result = await self.service.is_admin(42)
        self.assertTrue(result)
        self.state_store.is_admin.assert_awaited_once_with(42)

    async def test_reload_mappings_updates_lookup_tables(self) -> None:
        from tests.helpers import make_mapping
        new_mapping = make_mapping(mapping_id=99, tg_chat_id=-9999, bitrix_dialog_id='chat77')
        self.state_store.load_all_chat_mappings = AsyncMock(return_value=(new_mapping,))
        await self.service.reload_mappings()
        self.assertIsNotNone(self.service.get_mapping_for_bitrix_dialog('chat77'))
        self.assertIsNone(self.service.get_mapping_for_bitrix_dialog('chat42'))

    async def test_connect_mapping_adds_and_reloads(self) -> None:
        from tests.helpers import make_mapping
        new_mapping = make_mapping(mapping_id=10, tg_chat_id=-5555, bitrix_dialog_id='chatNEW')
        self.state_store.add_chat_mapping = AsyncMock(return_value=10)
        self.state_store.load_all_chat_mappings = AsyncMock(return_value=(new_mapping,))
        await self.service.connect_mapping(-5555, 'chatNEW', None, '')
        self.state_store.add_chat_mapping.assert_awaited_once_with(-5555, 'chatNEW', [], '')
        self.assertIsNotNone(self.service.get_mapping_for_bitrix_dialog('chatNEW'))

    async def test_connect_mapping_raises_on_existing_dialog_different_chat(self) -> None:
        with self.assertRaises(ValueError):
            await self.service.connect_mapping(-9999, 'chat42', None, '')

    async def test_connect_mapping_same_chat_adds_topic_to_existing(self) -> None:
        from tests.helpers import make_mapping
        updated = make_mapping(mapping_id=1, tg_chat_id=-1001234567890, bitrix_dialog_id='chat42', topic_ids=(100, 200, 55))
        self.state_store.update_chat_mapping_topic_ids = AsyncMock()
        self.state_store.load_all_chat_mappings = AsyncMock(return_value=(updated,))
        await self.service.connect_mapping(-1001234567890, 'chat42', 55, '')
        self.state_store.update_chat_mapping_topic_ids.assert_awaited_once_with(1, [100, 200, 55])
        self.state_store.add_chat_mapping.assert_not_awaited()

    async def test_disconnect_mapping_removes_and_reloads(self) -> None:
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
            with patch('asyncio.sleep', side_effect=fake_sleep):
                msg1 = make_message(chat_id=-1001234567890, message_id=1)
                msg2 = make_message(chat_id=-1001234567890, message_id=2)
                msg3 = make_message(chat_id=-1001234567890, message_id=3)
                await self.service.enqueue_telegram_message(msg1)
                await self.service.enqueue_telegram_message(msg2)
                await self.service.enqueue_telegram_message(msg3)
                queue = self.service._channel_queues[-1001234567890]
                for _ in range(20):
                    if queue.empty():
                        break
                    await asyncio.sleep(0)
                throttling_sleeps = [d for d in sleep_calls if d > 0]
                self.assertEqual(len(throttling_sleeps), 2)
                self.assertAlmostEqual(throttling_sleeps[0], 0.2)
                self.assertAlmostEqual(throttling_sleeps[1], 0.2)
            with patch('asyncio.create_task') as mock_create_task, patch.object(self.service, 'resolve_mapping_for_telegram_message', return_value=make_mapping()):
                mock_create_task.return_value = AsyncMock()
                chat_id_overflow = 9999
                max_size = self.service.settings.bitrix_send_queue_maxsize
                for i in range(max_size + 5):
                    msg = make_message(chat_id=chat_id_overflow, message_id=100 + i)
                    await self.service.enqueue_telegram_message(msg)
                self.assertEqual(self.service._channel_queues[chat_id_overflow].qsize(), max_size)
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
        new_mapping = make_mapping(mapping_id=99, tg_chat_id=-9999, bitrix_dialog_id='chat77')
        self.state_store.load_all_chat_mappings = AsyncMock(return_value=(new_mapping,))
        await self.service.reload_mappings()
        dummy_task.cancel.assert_called_once()
        self.assertNotIn(chat_id, self.service._channel_workers)
        self.assertNotIn(chat_id, self.service._channel_queues)

    async def test_fetcher_task_started_whenever_sync_is_enabled_even_with_zero_mappings(self) -> None:
        from tests.helpers import make_settings
        settings = make_settings(chat_mappings=())
        settings = dataclasses.replace(settings, sync_bitrix_to_telegram=True)
        service = MirrorService(settings, self.bitrix, self.state_store)
        app = SimpleNamespace()
        await service.start(app)
        self.assertIsNotNone(service._bitrix_event_task)
        self.assertFalse(service._bitrix_event_task.done())
        old_task = service._bitrix_event_task
        await service.reload_mappings()
        self.assertIs(service._bitrix_event_task, old_task)
        await service.stop()
        self.assertIsNone(service._bitrix_event_task)

    async def test_forwarding_disabled_acknowledges_events_without_telegram_side_effects(self) -> None:
        self.service._forwarding_enabled = False
        self.service._application = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        event = make_bitrix_event()
        await self.service._handle_bitrix_event(event)
        self.service._application.bot.send_message.assert_not_called()

    async def test_should_forward_bitrix_message_ignores_own_bot(self) -> None:
        self.service.settings = dataclasses.replace(self.service.settings, bitrix_bot_id=999)
        msg_from_bot = BitrixMessage(message_id=15, author_id=999, text='hello from bot', file_ids=(), update_time_unix=None, like_user_ids=(), reply_id=None, is_sticker=False, is_meeting=False, is_task=False)
        should_forward = await self.service._should_forward_bitrix_message('chat42', msg_from_bot)
        self.assertFalse(should_forward)

    async def test_should_forward_bitrix_message_ignores_tg_connect_command(self) -> None:
        msg = BitrixMessage(message_id=20, author_id=123, text='/tg_connect', file_ids=(), update_time_unix=None, like_user_ids=(), reply_id=None, is_sticker=False, is_meeting=False, is_task=False)
        should_forward = await self.service._should_forward_bitrix_message('chat42', msg)
        self.assertFalse(should_forward)

    async def test_should_forward_bitrix_message_ignores_tg_connect_with_args(self) -> None:
        msg = BitrixMessage(message_id=21, author_id=123, text='/tg_connect argument', file_ids=(), update_time_unix=None, like_user_ids=(), reply_id=None, is_sticker=False, is_meeting=False, is_task=False)
        should_forward = await self.service._should_forward_bitrix_message('chat42', msg)
        self.assertFalse(should_forward)

    async def test_enqueue_telegram_message_uses_configured_queue_maxsize(self) -> None:
        self.service.settings = dataclasses.replace(self.service.settings, bitrix_send_queue_maxsize=555)
        self.service._forwarding_enabled = True
        message = make_message(text='test queue size')
        await self.service.enqueue_telegram_message(message)
        queue = self.service._channel_queues[message.chat_id]
        self.assertEqual(queue.maxsize, 555)
        if message.chat_id in self.service._channel_workers:
            self.service._channel_workers[message.chat_id].cancel()

    async def test_per_channel_worker_drops_queued_messages_when_forwarding_disabled(self) -> None:
        """Worker must not send messages that were queued before forwarding was disabled."""
        mapping = self.service.settings.chat_mappings[0]
        chat_id = mapping.tg_chat_id
        queue: asyncio.Queue = asyncio.Queue()
        self.service._channel_queues[chat_id] = queue
        self.service._forwarding_enabled = False
        message = make_message(chat_id=chat_id)
        await queue.put(message)
        worker_task = asyncio.create_task(self.service._per_channel_worker(chat_id))
        await asyncio.sleep(0.05)
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)
        self.bitrix.send_message.assert_not_awaited()

    async def test_message_add_forwards_without_dialog_history_call(self) -> None:
        self.service._application = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(return_value=make_message())))
        await self.service._handle_bitrix_event(make_bitrix_event())
        self.service._application.bot.send_message.assert_awaited_once()
        self.state_store.upsert_link.assert_awaited_once()
        self.bitrix.get_recent_messages.assert_not_awaited()
        self.bitrix.get_messages_after.assert_not_awaited()

    async def test_message_add_is_idempotent_when_link_exists(self) -> None:
        self.state_store.get_link_by_bitrix_message.return_value = make_link(origin=MirrorOrigin.BITRIX)
        self.service._application = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        await self.service._handle_bitrix_event(make_bitrix_event())
        self.service._application.bot.send_message.assert_not_awaited()

    async def test_message_add_ignores_own_bot(self) -> None:
        self.service._application = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        await self.service._handle_bitrix_event(make_bitrix_event(author_id=7))
        self.service._application.bot.send_message.assert_not_awaited()

    async def test_message_add_ignores_unmapped_dialog(self) -> None:
        self.service._application = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        await self.service._handle_bitrix_event(make_bitrix_event(dialog_id='chat999'))
        self.service._application.bot.send_message.assert_not_awaited()

    async def test_tg_connect_saves_token_and_replies_in_bitrix(self) -> None:
        event = make_bitrix_event(text='/tg_connect')
        await self.service._handle_bitrix_event(event)
        self.state_store.save_pending_connection.assert_awaited_once()
        self.bitrix.send_message.assert_awaited_once()
        self.service._application = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        self.service._application.bot.send_message.assert_not_awaited()

    async def test_join_chat_sends_existing_greeting(self) -> None:
        event = make_bitrix_event('ONIMBOTV2JOINCHAT')
        event = dataclasses.replace(event, data={'bot': {'id': 7}, 'dialogId': 'chat42', 'chat': {'dialogId': 'chat42'}})
        await self.service._handle_bitrix_event(event)
        self.bitrix.send_message.assert_awaited_once()

    async def test_message_update_edits_linked_telegram_message(self) -> None:
        link = make_link(origin=MirrorOrigin.BITRIX)
        self.state_store.get_link_by_bitrix_message.return_value = link
        application = SimpleNamespace(bot=SimpleNamespace(edit_message_text=AsyncMock()))
        self.service._application = application
        await self.service._handle_bitrix_event(make_bitrix_event('ONIMBOTV2MESSAGEUPDATE', text='edited'))
        application.bot.edit_message_text.assert_awaited_once()
        self.state_store.upsert_link.assert_awaited_once()

    async def test_message_delete_removes_telegram_message_and_link(self) -> None:
        link = make_link(origin=MirrorOrigin.BITRIX)
        self.state_store.get_link_by_bitrix_message.return_value = link
        self.service._application = SimpleNamespace(bot=SimpleNamespace(delete_message=AsyncMock()))
        event = dataclasses.replace(make_bitrix_event('ONIMBOTV2MESSAGEDELETE'), data={'messageId': 789, 'chat': {'dialogId': 'chat42'}})
        await self.service._handle_bitrix_event(event)
        self.service._application.bot.delete_message.assert_awaited_once()
        self.state_store.delete_link_by_bitrix_message.assert_awaited_once_with(bitrix_message_id=789)

    async def test_message_delete_removes_link_when_telegram_message_not_found(self) -> None:
        link = make_link(origin=MirrorOrigin.BITRIX)
        self.state_store.get_link_by_bitrix_message.return_value = link
        self.service._application = SimpleNamespace(bot=SimpleNamespace(delete_message=AsyncMock(side_effect=BadRequest('Message to delete not found'))))
        event = dataclasses.replace(make_bitrix_event('ONIMBOTV2MESSAGEDELETE'), data={'messageId': 789, 'chat': {'dialogId': 'chat42'}})
        await self.service._handle_bitrix_event(event)
        self.service._application.bot.delete_message.assert_awaited_once()
        self.state_store.delete_link_by_bitrix_message.assert_awaited_once_with(bitrix_message_id=789)

    async def test_like_event_updates_telegram_and_reaction_state(self) -> None:
        link = make_link(last_seen_bitrix_likes='')
        self.state_store.get_link_by_bitrix_message.return_value = link
        self.service._application = SimpleNamespace(bot=SimpleNamespace(set_message_reaction=AsyncMock()))
        event = dataclasses.replace(make_bitrix_event('ONIMBOTV2REACTIONCHANGE'), data={'reaction': 'like', 'action': 'add', 'message': {'id': 789}, 'chat': {'dialogId': 'chat42'}, 'user': {'id': 41}})
        await self.service._handle_bitrix_event(event)
        self.service._application.bot.set_message_reaction.assert_awaited_once()
        self.state_store.update_reaction_state.assert_awaited_once_with(bitrix_message_id=789, bitrix_liked_by_bot=False, last_seen_bitrix_likes='41')

    async def test_message_add_resolves_reply_to_message_id(self) -> None:
        self.service._application = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(return_value=make_message())))
        reply_link = make_link(telegram_message_id=456, bitrix_message_id=700)
        self.state_store.get_link_by_bitrix_message.side_effect = lambda bitrix_message_id: reply_link if bitrix_message_id == 700 else None
        event = make_bitrix_event()
        event.data['message']['params']['REPLY_ID'] = '700'
        await self.service._handle_bitrix_event(event)
        self.service._application.bot.send_message.assert_awaited_once()
        kwargs = self.service._application.bot.send_message.await_args.kwargs
        self.assertEqual(kwargs.get('reply_to_message_id'), 456)

    async def test_message_add_delivers_photo_file(self) -> None:
        self.service._application = SimpleNamespace(bot=SimpleNamespace(send_photo=AsyncMock(return_value=make_message(photo=[SimpleNamespace(file_id='tgfile')]))))
        self.bitrix.download_file_by_id = AsyncMock(return_value=b'fake-image-bytes')
        event = make_bitrix_event()
        event.data['message']['params']['FILE_ID'] = ['9']
        event.data['files'] = {'9': {'ID': 9, 'original_name': 'pic.jpg', 'TYPE': 'file', 'MIME_TYPE': 'image/jpeg', 'DOWNLOAD_URL': 'https://example.com/pic.jpg', 'AUTHOR_ID': 41}}
        await self.service._handle_bitrix_event(event)
        self.bitrix.download_file_by_id.assert_awaited_once_with(9, fallback_url='https://example.com/pic.jpg')
        self.service._application.bot.send_photo.assert_awaited_once()

    async def test_fetch_cycle_advances_after_each_successful_event(self) -> None:
        self.state_store.load_bitrix_event_offset.return_value = 100
        self.bitrix.get_bot_events.return_value = BitrixEventPage(events=(make_bitrix_event(event_id=101),), next_offset=102, has_more=False)
        self.service._handle_bitrix_event = AsyncMock()
        await self.service._fetch_bitrix_events_once()
        self.state_store.save_bitrix_event_offset.assert_awaited_once_with(7, 102)

    async def test_fetch_cycle_does_not_advance_failed_event(self) -> None:
        self.state_store.load_bitrix_event_offset.return_value = 100
        self.bitrix.get_bot_events.return_value = BitrixEventPage(events=(make_bitrix_event(event_id=101),), next_offset=102, has_more=False)
        self.service._handle_bitrix_event = AsyncMock(side_effect=RuntimeError('Telegram down'))
        with self.assertRaises(RuntimeError):
            await self.service._fetch_bitrix_events_once()
        self.state_store.save_bitrix_event_offset.assert_not_awaited()

    async def test_fetch_cycle_sequentially_saves_event_offsets(self) -> None:
        self.state_store.load_bitrix_event_offset.return_value = 100
        self.bitrix.get_bot_events.return_value = BitrixEventPage(events=(make_bitrix_event(event_id=101), make_bitrix_event(event_id=102), make_bitrix_event(event_id=103)), next_offset=104, has_more=False)

        async def mock_handle(event):
            if event.event_id == 103:
                raise RuntimeError('Fail on third event')
        self.service._handle_bitrix_event = mock_handle
        with self.assertRaises(RuntimeError):
            await self.service._fetch_bitrix_events_once()
        self.assertEqual(self.state_store.save_bitrix_event_offset.call_count, 2)
        self.state_store.save_bitrix_event_offset.assert_has_awaits([unittest.mock.call(7, 102), unittest.mock.call(7, 103)])

def make_link(*, origin: MirrorOrigin=MirrorOrigin.BITRIX, last_seen_bitrix_likes: str='', bitrix_liked_by_bot: bool=False, telegram_chat_id: int=-1001234567890, telegram_message_id: int=200, bitrix_message_id: int=789) -> MessageMirrorLink:
    return MessageMirrorLink(telegram_chat_id=telegram_chat_id, telegram_message_id=telegram_message_id, bitrix_message_id=bitrix_message_id, origin=origin, telegram_message_date_unix=123456, bitrix_author_id=41, last_seen_bitrix_revision='rev', created_at_unix=123456, updated_at_unix=123456, bitrix_liked_by_bot=bitrix_liked_by_bot, last_seen_bitrix_likes=last_seen_bitrix_likes)
