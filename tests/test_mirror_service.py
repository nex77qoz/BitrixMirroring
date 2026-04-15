from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from mirror_service import MirrorService, _bbcode_to_html
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

    async def test_render_telegram_message_includes_topic_and_sender(self) -> None:
        self.service._topic_names[(-1001234567890, 100)] = "Deploy"
        message = make_message(message_thread_id=100, text="hello")
        rendered = self.service.render_telegram_message(message)
        self.assertIn("Deploy", rendered)
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

    async def test_bbcode_to_html_escapes_markup(self) -> None:
        converted = _bbcode_to_html("[b]Hi[/b] <script>")
        self.assertEqual(converted, "<b>Hi</b> &lt;script&gt;")

