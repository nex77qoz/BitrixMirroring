from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from mirror_service import MirrorService

logger = logging.getLogger("tg-bitrix-mirror")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return
    await update.effective_message.reply_text(
        "Бот запущен.\n"
        "Команда /whereami покажет chat_id текущего чата и thread_id темы."
    )


async def cmd_whereami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_chat:
        return
    msg = update.effective_message
    chat = update.effective_chat
    await msg.reply_text(
        "\n".join(
            [
                f"chat_id: {chat.id}",
                f"chat_type: {chat.type}",
                f"chat_title: {chat.title or '-'}",
                f"message_thread_id: {msg.message_thread_id or '-'}",
            ]
        )
    )


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    mirror: MirrorService = context.application.bot_data["mirror_service"]

    if not mirror.settings.sync_telegram_to_bitrix:
        return

    if message.from_user and message.from_user.is_bot:
        logger.debug("Ignoring Telegram bot message %s to avoid loops", message.message_id)
        return

    if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return

    if not mirror.is_allowed_chat(message):
        logger.debug("Ignoring message from chat_id=%s because it is not allowed", message.chat_id)
        return

    if any(
        [
            message.new_chat_members,
            message.left_chat_member,
            message.group_chat_created,
            message.supergroup_chat_created,
            message.delete_chat_photo,
            message.pinned_message,
            message.migrate_from_chat_id,
            message.migrate_to_chat_id,
        ]
    ):
        return

    if message.sticker:
        logger.debug("Ignoring Telegram sticker message %s", message.message_id)
        return

    await mirror.enqueue_telegram_message(message)


async def on_edited_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    mirror: MirrorService = context.application.bot_data["mirror_service"]

    if not mirror.settings.sync_telegram_to_bitrix:
        return

    if message.from_user and message.from_user.is_bot:
        logger.debug("Ignoring Telegram bot edit %s to avoid loops", message.message_id)
        return

    if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return

    if not mirror.is_allowed_chat(message):
        logger.debug("Ignoring edited message from chat_id=%s because it is not allowed", message.chat_id)
        return

    await mirror.sync_telegram_edit(message)


async def on_message_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reaction = update.message_reaction
    if not reaction:
        return

    mirror: MirrorService = context.application.bot_data["mirror_service"]

    if not mirror.settings.sync_telegram_to_bitrix:
        return

    if reaction.user and reaction.user.is_bot:
        logger.debug("Ignoring bot reaction on message %s", reaction.message_id)
        return

    if reaction.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return

    if mirror.get_mapping_for_telegram_chat(reaction.chat.id) is None:
        logger.debug("Ignoring reaction from chat_id=%s because it is not allowed", reaction.chat.id)
        return

    has_reactions = bool(reaction.new_reaction)
    await mirror.sync_telegram_reaction(reaction.chat.id, reaction.message_id, has_reactions)


# Telegram Bot API does not provide a universal deleted-message update for ordinary bot polling,
# so Telegram -> Bitrix delete cannot be implemented reliably here.

