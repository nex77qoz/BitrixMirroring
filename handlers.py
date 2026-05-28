from __future__ import annotations

import asyncio
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from mirror_service import MirrorService

logger = logging.getLogger("tg-bitrix-mirror")

_bot_reply_ids: dict[int, list[int]] = {}

_ADMIN_CALLBACK_PREFIX = "admin:"
_SERVICE_NAMES = ("bitrix-bot", "bitrix-monitor", "bitrix-telegram-mirror")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return
    if update.effective_chat and update.effective_chat.type == ChatType.PRIVATE:
        mirror: MirrorService = context.application.bot_data["mirror_service"]
        if await _check_admin(update, mirror):
            await _reply_admin_panel(update.effective_message, mirror)
            return
        await update.effective_message.reply_text("Нет доступа.")
        return
    sent = await update.effective_message.reply_text(
        "Бот запущен.\n"
        "Команда /whereami покажет chat_id текущего чата и thread_id темы."
    )
    _bot_reply_ids.setdefault(update.effective_chat.id, []).append(sent.message_id)


async def on_private_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat or chat.type != ChatType.PRIVATE:
        return

    mirror: MirrorService = context.application.bot_data["mirror_service"]
    if not await _check_admin(update, mirror):
        await msg.reply_text("Нет доступа.")
        return
    await _reply_admin_panel(msg, mirror)


async def on_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data or not query.data.startswith(_ADMIN_CALLBACK_PREFIX):
        return

    mirror: MirrorService = context.application.bot_data["mirror_service"]
    if not await _check_admin(update, mirror):
        await query.answer("Нет доступа", show_alert=True)
        return

    await query.answer()
    action = query.data.removeprefix(_ADMIN_CALLBACK_PREFIX)

    if action == "mappings":
        await mirror.reload_mappings()
        await query.edit_message_text(
            _render_mappings(mirror),
            reply_markup=_admin_panel_markup(mirror),
        )
        return

    if action == "forwarding:off":
        await mirror.set_forwarding_enabled(False)
        await query.edit_message_text(
            "Пересылка остановлена.",
            reply_markup=_admin_panel_markup(mirror),
        )
        return

    if action == "forwarding:on":
        await mirror.set_forwarding_enabled(True)
        await query.edit_message_text(
            "Пересылка включена.",
            reply_markup=_admin_panel_markup(mirror),
        )
        return

    if action == "restart":
        await query.edit_message_text("Перезагрузка служб запущена.")
        _schedule_service_restart(context)


async def _reply_admin_panel(message, mirror: MirrorService) -> None:
    status = "включена" if mirror.is_forwarding_enabled() else "остановлена"
    await message.reply_text(
        f"Админ-панель. Пересылка: {status}.",
        reply_markup=_admin_panel_markup(mirror),
    )


def _admin_panel_markup(mirror: MirrorService) -> InlineKeyboardMarkup:
    forwarding_enabled = mirror.is_forwarding_enabled()
    forwarding_text = "Остановить пересылку" if forwarding_enabled else "Включить пересылку"
    forwarding_action = "admin:forwarding:off" if forwarding_enabled else "admin:forwarding:on"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Проверить маппинги", callback_data="admin:mappings")],
            [InlineKeyboardButton(forwarding_text, callback_data=forwarding_action)],
            [InlineKeyboardButton("Перезагрузить службы", callback_data="admin:restart")],
        ]
    )


def _render_mappings(mirror: MirrorService) -> str:
    mappings = mirror.get_chat_mappings()
    if not mappings:
        return "Маппинги не настроены."

    lines = ["Текущие маппинги:"]
    for mapping in mappings:
        topics = ", ".join(str(topic_id) for topic_id in sorted(mapping.topic_ids)) or "все"
        label = f" ({mapping.label})" if mapping.label else ""
        lines.append(
            f"#{mapping.mapping_id}{label}: TG {mapping.tg_chat_id} topics {topics} -> Bitrix {mapping.bitrix_dialog_id}"
        )
    return "\n".join(lines)


def _schedule_service_restart(context: ContextTypes.DEFAULT_TYPE) -> None:
    task = _delayed_restart_bot_services()
    create_task = getattr(context.application, "create_task", None)
    if callable(create_task):
        create_task(task)
    else:
        asyncio.create_task(task, name="restart-bot-services")


async def _delayed_restart_bot_services() -> None:
    await asyncio.sleep(1)
    await _restart_bot_services()


async def _restart_bot_services() -> None:
    for service_name in _SERVICE_NAMES:
        process = await asyncio.create_subprocess_exec(
            "sudo",
            "-n",
            "systemctl",
            "restart",
            service_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            details = (stderr or stdout).decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Failed to restart {service_name}: {details}")


async def cmd_whereami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_chat:
        return
    msg = update.effective_message
    chat = update.effective_chat
    sent = await msg.reply_text(
        "\n".join(
            [
                f"chat_id: {chat.id}",
                f"chat_type: {chat.type}",
                f"chat_title: {chat.title or '-'}",
                f"message_thread_id: {msg.message_thread_id or '-'}",
            ]
        )
    )
    _bot_reply_ids.setdefault(chat.id, []).append(sent.message_id)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    mirror: MirrorService = context.application.bot_data["mirror_service"]

    if not mirror.settings.sync_telegram_to_bitrix:
        return

    if message.from_user and message.from_user.is_bot:
        is_anonymous_admin = (
            message.from_user.username == "GroupAnonymousBot" or
            (message.sender_chat and message.sender_chat.id == message.chat_id)
        )
        if not is_anonymous_admin:
            logger.debug("Ignoring Telegram bot message %s to avoid loops", message.message_id)
            return

    if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return

    if not mirror.is_allowed_chat(message):
        logger.debug("Ignoring message from chat_id=%s because it is not allowed", message.chat_id)
        return

    # Opportunistically track topic names
    if message.message_thread_id:
        topic_name = None
        if message.forum_topic_created and message.forum_topic_created.name:
            topic_name = message.forum_topic_created.name
        elif message.forum_topic_edited and message.forum_topic_edited.name:
            topic_name = message.forum_topic_edited.name
        elif message.reply_to_message and message.reply_to_message.forum_topic_created:
            topic_name = message.reply_to_message.forum_topic_created.name
            
        if topic_name:
            mirror.cache_topic_name(message.chat_id, message.message_thread_id, topic_name)


    if not mirror.is_allowed_topic(message):
        logger.debug(
            "Ignoring message from chat_id=%s thread_id=%s (topic not in allowed list)",
            message.chat_id, message.message_thread_id,
        )
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
            message.forum_topic_created,
            message.forum_topic_edited,
            message.forum_topic_closed,
            message.forum_topic_reopened,
        ]
    ):
        return

    if message.sticker:
        logger.debug("Ignoring Telegram sticker message %s", message.message_id)
        return

    if any(
        [
            message.contact,
            message.poll,
            message.location,
            message.venue,
            message.voice,
            message.video_note,
            getattr(message, "checklist", None),  # Telegram task lists (Bot API 9+)
        ]
    ):
        logger.debug("Ignoring unsupported message type for Telegram message %s", message.message_id)
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
        is_anonymous_admin = (
            message.from_user.username == "GroupAnonymousBot" or
            (message.sender_chat and message.sender_chat.id == message.chat_id)
        )
        if not is_anonymous_admin:
            logger.debug("Ignoring Telegram bot edit %s to avoid loops", message.message_id)
            return

    if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return

    if not mirror.is_allowed_chat(message):
        logger.debug("Ignoring edited message from chat_id=%s because it is not allowed", message.chat_id)
        return

    if not mirror.is_allowed_topic(message):
        logger.debug(
            "Ignoring edited message from chat_id=%s thread_id=%s (topic not in allowed list)",
            message.chat_id, message.message_thread_id,
        )
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

_BITRIX_ID_RE = re.compile(r"^(chat\d+|sg\d+|\d+)$")


async def _check_admin(update: Update, mirror: "MirrorService") -> bool:
    user = update.effective_user
    if not user:
        return False
    return await mirror.is_admin(user.id)


async def cmd_connect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return

    mirror: MirrorService = context.application.bot_data["mirror_service"]
    args = context.args or []
    
    if len(args) == 2:
        # Token-based flow for self-service connection (no admin check required!)
        bitrix_dialog_id = args[0].strip()
        token = args[1].strip()
        
        if not _BITRIX_ID_RE.match(bitrix_dialog_id):
            sent = await msg.reply_text("Неверный формат Bitrix Chat ID.")
            _bot_reply_ids.setdefault(chat.id, []).append(sent.message_id)
            return

        is_valid = await mirror.state_store.verify_and_consume_token(bitrix_dialog_id, token)
        if not is_valid:
            sent = await msg.reply_text("⚠️ Неверный, использованный или просроченный токен подключения.")
            _bot_reply_ids.setdefault(chat.id, []).append(sent.message_id)
            return
            
    elif len(args) == 1:
        # Traditional admin-only connection flow
        if not await _check_admin(update, mirror):
            return
        bitrix_dialog_id = args[0].strip()
        if not _BITRIX_ID_RE.match(bitrix_dialog_id):
            sent = await msg.reply_text("Неверный формат Bitrix Chat ID.")
            _bot_reply_ids.setdefault(chat.id, []).append(sent.message_id)
            return
    else:
        sent = await msg.reply_text(
            "Использование:\n"
            "Самостоятельно: `/connect <BitrixChatId> <Token>`\n"
            "Для администраторов: `/connect <BitrixChatId>`"
        )
        _bot_reply_ids.setdefault(chat.id, []).append(sent.message_id)
        return

    is_forum = getattr(chat, "is_forum", False)
    topic_id = msg.message_thread_id if is_forum else None
    try:
        await mirror.connect_mapping(chat.id, bitrix_dialog_id, topic_id, "")
    except ValueError as exc:
        sent = await msg.reply_text(f"⚠️ {exc}")
        _bot_reply_ids.setdefault(chat.id, []).append(sent.message_id)
        return

    thread_info = f" (ветка #{topic_id})" if topic_id else ""
    sent = await msg.reply_text(f"✅ Связка установлена: {bitrix_dialog_id} ↔ этот чат{thread_info}")
    _bot_reply_ids.setdefault(chat.id, []).append(sent.message_id)


async def cmd_disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return

    mirror: MirrorService = context.application.bot_data["mirror_service"]
    if not await _check_admin(update, mirror):
        return

    is_forum = getattr(chat, "is_forum", False)
    topic_id = msg.message_thread_id if is_forum else None
    removed = await mirror.disconnect_mapping(chat.id, topic_id)
    if removed:
        thread_info = f" (ветка #{topic_id})" if topic_id else ""
        sent = await msg.reply_text(f"✅ Связка удалена для этого чата{thread_info}.")
    else:
        sent = await msg.reply_text("⚠️ Связка для этого чата/ветки не найдена.")
    _bot_reply_ids.setdefault(chat.id, []).append(sent.message_id)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return

    mirror: MirrorService = context.application.bot_data["mirror_service"]
    if not await _check_admin(update, mirror):
        return

    ids = _bot_reply_ids.pop(chat.id, [])
    for mid in ids:
        try:
            await context.bot.delete_message(chat_id=chat.id, message_id=mid)
        except Exception:
            pass
    try:
        await msg.delete()
    except Exception:
        pass
