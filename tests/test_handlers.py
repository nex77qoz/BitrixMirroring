from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from handlers import cmd_start, on_admin_callback, on_edited_message, on_message, on_message_reaction, on_private_admin_message
from tests.helpers import make_message


class HandlersTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.mirror = AsyncMock()
        self.mirror.settings.sync_telegram_to_bitrix = True
        self.mirror.is_allowed_chat = Mock(return_value=True)
        self.mirror.is_allowed_topic = Mock(return_value=True)
        self.mirror.get_mapping_for_telegram_chat = Mock(return_value=object())
        self.mirror.is_admin = AsyncMock(return_value=True)
        self.mirror.is_forwarding_enabled = Mock(return_value=True)
        self.mirror.get_chat_mappings = Mock(return_value=())
        def close_created_coroutine(coro):
            coro.close()
            return object()
        self.context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={"mirror_service": self.mirror},
                create_task=Mock(side_effect=close_created_coroutine),
            ),
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

    async def test_cmd_start_shows_admin_panel_in_private_chat(self) -> None:
        msg = make_message(chat=SimpleNamespace(id=1, type="private", title=None))
        msg.reply_text = AsyncMock()
        update = SimpleNamespace(
            effective_message=msg,
            effective_chat=msg.chat,
            effective_user=SimpleNamespace(id=777),
        )

        await cmd_start(update, self.context)

        msg.reply_text.assert_awaited_once()
        kwargs = msg.reply_text.call_args.kwargs
        self.assertIsNotNone(kwargs.get("reply_markup"))
        button_texts = [
            button.text
            for row in kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        self.assertIn("Проверить маппинги", button_texts)
        self.assertIn("Остановить пересылку", button_texts)
        self.assertIn("Перезагрузить службы", button_texts)

    async def test_private_admin_message_shows_panel(self) -> None:
        msg = make_message(chat=SimpleNamespace(id=1, type="private", title=None))
        msg.reply_text = AsyncMock()
        update = SimpleNamespace(
            effective_message=msg,
            effective_chat=msg.chat,
            effective_user=SimpleNamespace(id=777),
        )

        await on_private_admin_message(update, self.context)

        msg.reply_text.assert_awaited_once()
        self.assertIsNotNone(msg.reply_text.call_args.kwargs.get("reply_markup"))

    async def test_private_non_admin_message_replies_denied(self) -> None:
        self.mirror.is_admin = AsyncMock(return_value=False)
        msg = make_message(chat=SimpleNamespace(id=1, type="private", title=None))
        msg.reply_text = AsyncMock()
        update = SimpleNamespace(
            effective_message=msg,
            effective_chat=msg.chat,
            effective_user=SimpleNamespace(id=999),
        )

        await on_private_admin_message(update, self.context)

        msg.reply_text.assert_awaited_once()
        self.assertIn("Нет доступа", msg.reply_text.call_args.args[0])

    async def test_admin_callback_lists_mappings(self) -> None:
        mapping = SimpleNamespace(
            mapping_id=1,
            tg_chat_id=-100123,
            bitrix_dialog_id="chat42",
            topic_ids=frozenset({55, 66}),
            label="",
        )
        self.mirror.get_chat_mappings = Mock(return_value=(mapping,))
        query = SimpleNamespace(
            data="admin:mappings",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            from_user=SimpleNamespace(id=777),
        )
        update = SimpleNamespace(callback_query=query, effective_user=query.from_user)

        await on_admin_callback(update, self.context)

        self.mirror.reload_mappings.assert_awaited_once()
        query.answer.assert_awaited_once()
        query.edit_message_text.assert_awaited_once()
        text = query.edit_message_text.call_args.args[0]
        self.assertIn("chat42", text)
        self.assertIn("-100123", text)
        self.assertIn("55, 66", text)

    async def test_admin_callback_stops_forwarding(self) -> None:
        query = SimpleNamespace(
            data="admin:forwarding:off",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            from_user=SimpleNamespace(id=777),
        )
        update = SimpleNamespace(callback_query=query, effective_user=query.from_user)

        await on_admin_callback(update, self.context)

        self.mirror.set_forwarding_enabled.assert_awaited_once_with(False)
        query.edit_message_text.assert_awaited_once()
        self.assertIn("остановлена", query.edit_message_text.call_args.args[0])

    async def test_admin_callback_schedules_service_restart(self) -> None:
        query = SimpleNamespace(
            data="admin:restart",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            from_user=SimpleNamespace(id=777),
        )
        update = SimpleNamespace(callback_query=query, effective_user=query.from_user)

        await on_admin_callback(update, self.context)

        query.edit_message_text.assert_awaited_once()
        self.context.application.create_task.assert_called_once()

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
            chat=SimpleNamespace(id=-100123, type="supergroup", is_forum=True),
        )
        update = SimpleNamespace(
            effective_message=msg,
            effective_chat=SimpleNamespace(id=-100123, type="supergroup", is_forum=True),
            effective_user=SimpleNamespace(id=777),
        )
        self.context.args = ["sg99"]
        await cmd_connect(update, self.context)
        self.mirror.connect_mapping.assert_awaited_once_with(-100123, "sg99", 55, "")

    async def test_cmd_connect_token_success_bypasses_admin(self) -> None:
        from handlers import cmd_connect
        self.mirror.is_admin = AsyncMock(return_value=False)
        self.mirror.state_store.verify_and_consume_token = AsyncMock(return_value=True)
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
        self.context.args = ["chat42", "valid_token_123"]
        await cmd_connect(update, self.context)
        
        self.mirror.state_store.verify_and_consume_token.assert_awaited_once_with("chat42", "valid_token_123")
        self.mirror.connect_mapping.assert_awaited_once_with(-100123, "chat42", None, "")
        msg.reply_text.assert_awaited_once()
        self.mirror.is_admin.assert_not_called()

    async def test_cmd_connect_token_invalid_fails(self) -> None:
        from handlers import cmd_connect
        self.mirror.is_admin = AsyncMock(return_value=False)
        self.mirror.state_store.verify_and_consume_token = AsyncMock(return_value=False)
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
        self.context.args = ["chat42", "invalid_token"]
        await cmd_connect(update, self.context)
        
        self.mirror.state_store.verify_and_consume_token.assert_awaited_once_with("chat42", "invalid_token")
        self.mirror.connect_mapping.assert_not_called()
        msg.reply_text.assert_awaited_once()
        reply_arg = msg.reply_text.call_args[0][0]
        self.assertIn("токен подключения", reply_arg)
        self.mirror.is_admin.assert_not_called()

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
