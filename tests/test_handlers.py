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
        self.mirror.is_admin = AsyncMock(return_value=True)
        self.context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"mirror_service": self.mirror}),
            args=[],
        )

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

    async def test_cmd_connect_adds_mapping_no_thread(self) -> None:
        from handlers import cmd_connect
        msg = SimpleNamespace(
            chat_id=-100123,
            message_thread_id=None,
            reply_text=AsyncMock(),
            chat=SimpleNamespace(id=-100123, type="supergroup"),
        )
        update = SimpleNamespace(
            effective_message=msg,
            effective_chat=SimpleNamespace(id=-100123, type="supergroup"),
            effective_user=SimpleNamespace(id=777),
        )
        self.context.args = ["chat42"]
        await cmd_connect(update, self.context)
        self.mirror.connect_mapping.assert_awaited_once_with(-100123, "chat42", None, "")
        msg.reply_text.assert_awaited_once()

    async def test_cmd_connect_non_admin_silently_ignored(self) -> None:
        from handlers import cmd_connect
        self.mirror.is_admin = AsyncMock(return_value=False)
        msg = SimpleNamespace(
            chat_id=-100123,
            message_thread_id=None,
            reply_text=AsyncMock(),
            chat=SimpleNamespace(id=-100123, type="supergroup"),
        )
        update = SimpleNamespace(
            effective_message=msg,
            effective_chat=SimpleNamespace(id=-100123, type="supergroup"),
            effective_user=SimpleNamespace(id=999),
        )
        self.context.args = ["chat42"]
        await cmd_connect(update, self.context)
        self.mirror.connect_mapping.assert_not_called()
        msg.reply_text.assert_not_called()

    async def test_cmd_connect_invalid_format_replies_error(self) -> None:
        from handlers import cmd_connect
        msg = SimpleNamespace(
            chat_id=-100123,
            message_thread_id=None,
            reply_text=AsyncMock(),
            chat=SimpleNamespace(id=-100123, type="supergroup"),
        )
        update = SimpleNamespace(
            effective_message=msg,
            effective_chat=SimpleNamespace(id=-100123, type="supergroup"),
            effective_user=SimpleNamespace(id=777),
        )
        self.context.args = ["invalid!!"]
        await cmd_connect(update, self.context)
        self.mirror.connect_mapping.assert_not_called()
        msg.reply_text.assert_awaited_once()

    async def test_cmd_connect_with_thread_id(self) -> None:
        from handlers import cmd_connect
        msg = SimpleNamespace(
            chat_id=-100123,
            message_thread_id=55,
            reply_text=AsyncMock(),
            chat=SimpleNamespace(id=-100123, type="supergroup"),
        )
        update = SimpleNamespace(
            effective_message=msg,
            effective_chat=SimpleNamespace(id=-100123, type="supergroup"),
            effective_user=SimpleNamespace(id=777),
        )
        self.context.args = ["sg99"]
        await cmd_connect(update, self.context)
        self.mirror.connect_mapping.assert_awaited_once_with(-100123, "sg99", 55, "")

    async def test_cmd_disconnect_removes_mapping(self) -> None:
        from handlers import cmd_disconnect
        self.mirror.disconnect_mapping = AsyncMock(return_value=True)
        msg = SimpleNamespace(
            chat_id=-100123,
            message_thread_id=None,
            reply_text=AsyncMock(),
            chat=SimpleNamespace(id=-100123, type="supergroup"),
        )
        update = SimpleNamespace(
            effective_message=msg,
            effective_chat=SimpleNamespace(id=-100123, type="supergroup"),
            effective_user=SimpleNamespace(id=777),
        )
        await cmd_disconnect(update, self.context)
        self.mirror.disconnect_mapping.assert_awaited_once_with(-100123, None)
        msg.reply_text.assert_awaited_once()

    async def test_cmd_disconnect_not_found_replies_warning(self) -> None:
        from handlers import cmd_disconnect
        self.mirror.disconnect_mapping = AsyncMock(return_value=False)
        msg = SimpleNamespace(
            chat_id=-100123,
            message_thread_id=None,
            reply_text=AsyncMock(),
            chat=SimpleNamespace(id=-100123, type="supergroup"),
        )
        update = SimpleNamespace(
            effective_message=msg,
            effective_chat=SimpleNamespace(id=-100123, type="supergroup"),
            effective_user=SimpleNamespace(id=777),
        )
        await cmd_disconnect(update, self.context)
        msg.reply_text.assert_awaited_once()
        call_args = msg.reply_text.call_args[0][0]
        self.assertIn("не найдена", call_args)
