from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from handlers import on_edited_message, on_message, on_message_reaction
from tests.helpers import make_message


class HandlersTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.mirror = AsyncMock()
        self.mirror.settings.sync_telegram_to_bitrix = True
        self.mirror.is_allowed_chat = Mock(return_value=True)
        self.mirror.is_allowed_topic = Mock(return_value=True)
        self.mirror.get_mapping_for_telegram_chat = Mock(return_value=object())
        self.context = SimpleNamespace(application=SimpleNamespace(bot_data={"mirror_service": self.mirror}))

    async def test_on_message_enqueues_supported_group_message(self) -> None:
        update = SimpleNamespace(effective_message=make_message())
        await on_message(update, self.context)
        self.mirror.enqueue_telegram_message.assert_awaited_once()

    async def test_on_message_ignores_non_group_message(self) -> None:
        update = SimpleNamespace(effective_message=make_message(chat=SimpleNamespace(id=1, type="private", title=None)))
        await on_message(update, self.context)
        self.mirror.enqueue_telegram_message.assert_not_called()

    async def test_on_edited_message_syncs_allowed_message(self) -> None:
        update = SimpleNamespace(effective_message=make_message())
        await on_edited_message(update, self.context)
        self.mirror.sync_telegram_edit.assert_awaited_once()

    async def test_on_message_reaction_syncs_like_state(self) -> None:
        reaction = SimpleNamespace(
            user=SimpleNamespace(is_bot=False),
            chat=SimpleNamespace(id=-1001234567890, type="supergroup"),
            message_id=100,
            new_reaction=["like"],
        )
        update = SimpleNamespace(message_reaction=reaction)
        await on_message_reaction(update, self.context)
        self.mirror.sync_telegram_reaction.assert_awaited_once_with(-1001234567890, 100, True)
