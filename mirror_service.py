from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import html
import logging
import re
from io import BytesIO
from pathlib import Path
from typing import Any, cast

from telegram import Message, ReactionTypeEmoji
from telegram.error import BadRequest, ChatMigrated
from telegram.ext import Application

from bitrix_client import BitrixClient
from mirror_state_store import MirrorStateStore
from models import (
    BitrixBotEvent,
    BitrixDialogSnapshot,
    BitrixFile,
    BitrixMessage,
    BitrixUser,
    MessageMirrorLink,
    MirrorOrigin,
)
from settings import ChatMapping, Settings

logger = logging.getLogger('tg-bitrix-mirror')
_BITRIX_MESSAGE_LIMIT = 20_000
_TELEGRAM_MESSAGE_LIMIT = 4_096
_TELEGRAM_CAPTION_LIMIT = 1_024
_BBCODE_PATTERNS: list[tuple[re.Pattern[str], str]] = [(re.compile('\\[b\\](.*?)\\[/b\\]', re.DOTALL | re.IGNORECASE), '<b>\\1</b>'), (re.compile('\\[i\\](.*?)\\[/i\\]', re.DOTALL | re.IGNORECASE), '<i>\\1</i>'), (re.compile('\\[u\\](.*?)\\[/u\\]', re.DOTALL | re.IGNORECASE), '<u>\\1</u>'), (re.compile('\\[s\\](.*?)\\[/s\\]', re.DOTALL | re.IGNORECASE), '<s>\\1</s>'), (re.compile('\\[code\\](.*?)\\[/code\\]', re.DOTALL | re.IGNORECASE), '<code>\\1</code>'), (re.compile('\\[quote\\](.*?)\\[/quote\\]', re.DOTALL | re.IGNORECASE), '<blockquote>\\1</blockquote>'), (re.compile('\\[url\\](.*?)\\[/url\\]', re.DOTALL | re.IGNORECASE), '<a href="\\1">\\1</a>'), (re.compile('\\[url=([^\\]]+)\\](.*?)\\[/url\\]', re.DOTALL | re.IGNORECASE), '<a href="\\1">\\2</a>'), (re.compile('\\[color=[^\\]]+\\](.*?)\\[/color\\]', re.DOTALL | re.IGNORECASE), '\\1')]

def _bbcode_to_html(text: str) -> str:
    """Convert Bitrix BBCode markup to Telegram HTML markup.

    Safely HTML-escapes the raw text first so that any ``<``, ``>``, or ``&``
    characters in the message body do not break Telegram's HTML parser.  The
    BBCode square-bracket tags survive ``html.escape`` untouched and are then
    replaced with the corresponding HTML tags.
    """
    escaped = html.escape(text)
    for pattern, replacement in _BBCODE_PATTERNS:
        escaped = pattern.sub(replacement, escaped)
    return escaped

class MirrorService:

    def __init__(self, settings: Settings, bitrix: BitrixClient, state_store: MirrorStateStore) -> None:
        self.settings = settings
        self.bitrix = bitrix
        self.state_store = state_store
        self._last_seen_bitrix_message_ids: dict[str, int | None] = {}
        self._bitrix_poll_tasks: list[asyncio.Task[None]] = []
        self._stop_event = asyncio.Event()
        self._cursor_locks: dict[str, asyncio.Lock] = {}
        self._channel_queues: dict[int, asyncio.Queue[Message]] = {}
        self._channel_workers: dict[int, asyncio.Task] = {}
        self._tg_to_mappings: dict[int, list[ChatMapping]] = {}
        for mapping in settings.chat_mappings:
            self._tg_to_mappings.setdefault(mapping.tg_chat_id, []).append(mapping)
        self._bitrix_to_mapping: dict[str, ChatMapping] = {m.bitrix_dialog_id: m for m in settings.chat_mappings}
        self._cleanup_task: asyncio.Task[None] | None = None
        self._application: Application | None = None
        self._bitrix_sync_locks: dict[str, asyncio.Lock] = {}
        self._bitrix_on_demand_tasks: dict[str, asyncio.Task[None]] = {}
        self._webhook_reply_cache: dict[int, int] = {}
        self._topic_names: dict[tuple[int, int], str] = {}
        self._forward_attempts: dict[int, int] = {}
        self._tg_forward_dead_letters: dict[int, int] = {}
        self._poll_error_count: int = 0
        self._forwarding_enabled = True
        self._scheduler_task: asyncio.Task | None = None
        self._bitrix_event_task: asyncio.Task | None = None
        self._bitrix_event_offset: int | None = None
        self._poll_semaphore: asyncio.Semaphore | None = None

    def get_mapping_for_telegram_chat(self, tg_chat_id: int) -> ChatMapping | None:
        mappings = self._tg_to_mappings.get(tg_chat_id)
        return mappings[0] if mappings else None

    def get_mappings_for_telegram_chat(self, tg_chat_id: int) -> tuple[ChatMapping, ...]:
        return tuple(self._tg_to_mappings.get(tg_chat_id, ()))

    def get_mapping_for_bitrix_dialog(self, dialog_id: str) -> ChatMapping | None:
        return self._bitrix_to_mapping.get(dialog_id)

    def get_chat_mappings(self) -> tuple[ChatMapping, ...]:
        return tuple(mapping for mappings in self._tg_to_mappings.values() for mapping in mappings)

    def resolve_mapping_for_telegram_message(self, message: Message) -> ChatMapping | None:
        is_forum = getattr(message.chat, 'is_forum', False)
        thread_id = message.message_thread_id if is_forum else None
        return self.resolve_mapping_for_chat_and_thread(message.chat_id, thread_id)

    def resolve_mapping_for_chat_and_thread(self, tg_chat_id: int, message_thread_id: int | None) -> ChatMapping | None:
        mappings = self._tg_to_mappings.get(tg_chat_id)
        if not mappings:
            return None
        topic_matches = [mapping for mapping in mappings if mapping.topic_ids and message_thread_id in mapping.topic_ids]
        if len(topic_matches) == 1:
            return topic_matches[0]
        if len(topic_matches) > 1:
            logger.warning('Multiple topic mappings matched tg_chat_id=%s thread_id=%s; using first mapping_id=%s', tg_chat_id, message_thread_id, topic_matches[0].mapping_id)
            return topic_matches[0]
        if message_thread_id is None:
            multi_topic_mappings = [mapping for mapping in mappings if len(mapping.topic_ids) > 1]
            if len(multi_topic_mappings) == 1:
                return multi_topic_mappings[0]
            if len(multi_topic_mappings) > 1:
                logger.warning('Main feed is ambiguous for tg_chat_id=%s: multiple many-topics mappings found (%s); dropping message', tg_chat_id, ', '.join(str(mapping.mapping_id) for mapping in multi_topic_mappings))
                return None
        catch_all_mappings = [mapping for mapping in mappings if not mapping.topic_ids]
        if len(catch_all_mappings) == 1:
            return catch_all_mappings[0]
        if len(catch_all_mappings) > 1:
            logger.warning('Multiple catch-all mappings matched tg_chat_id=%s; using first mapping_id=%s', tg_chat_id, catch_all_mappings[0].mapping_id)
            return catch_all_mappings[0]
        return None

    def _is_multi_topic_mode(self, mapping: ChatMapping) -> bool:
        """True when multiple Telegram topics map to a single Bitrix dialog."""
        return len(mapping.topic_ids) != 1

    def cache_topic_name(self, tg_chat_id: int, topic_id: int, name: str) -> None:
        self._topic_names[tg_chat_id, topic_id] = name
        asyncio.create_task(self.state_store.save_topic_name(tg_chat_id, topic_id, name), name=f'save-topic-name-{tg_chat_id}-{topic_id}')

    def is_allowed_chat(self, message: Message) -> bool:
        return message.chat_id in self._tg_to_mappings

    def is_allowed_topic(self, message: Message) -> bool:
        """Return True if this message's forum topic is permitted by the mapping.

        If the mapping has no topic_ids (empty frozenset), all topics are allowed.
        Otherwise only messages whose message_thread_id is in topic_ids pass.
        Messages without a thread (regular groups) are always allowed.
        """
        return self.resolve_mapping_for_telegram_message(message) is not None

    def is_forwarding_enabled(self) -> bool:
        return self._forwarding_enabled

    async def set_forwarding_enabled(self, enabled: bool) -> bool:
        self._forwarding_enabled = enabled
        await self.state_store.set_forwarding_enabled(enabled)
        logger.warning('Message forwarding %s via runtime control', 'enabled' if enabled else 'disabled')
        return self._forwarding_enabled

    async def is_admin(self, tg_user_id: int) -> bool:
        return await self.state_store.is_admin(tg_user_id)

    async def reload_mappings(self) -> None:
        mappings = await self.state_store.load_all_chat_mappings()
        self.settings = dataclasses.replace(self.settings, chat_mappings=mappings)
        new_tg: dict[int, list[ChatMapping]] = {}
        for m in mappings:
            new_tg.setdefault(m.tg_chat_id, []).append(m)
        self._tg_to_mappings = new_tg
        self._bitrix_to_mapping = {m.bitrix_dialog_id: m for m in mappings}
        active_chat_ids = {m.tg_chat_id for m in mappings}
        for chat_id in list(self._channel_workers.keys()):
            if chat_id not in active_chat_ids:
                task = self._channel_workers.pop(chat_id)
                task.cancel()
                self._channel_queues.pop(chat_id, None)

    async def connect_mapping(self, tg_chat_id: int, bitrix_dialog_id: str, topic_id: int | None, label: str) -> None:
        existing = self._bitrix_to_mapping.get(bitrix_dialog_id)
        if existing is not None and existing.tg_chat_id != tg_chat_id:
            raise ValueError(f'Bitrix dialog {bitrix_dialog_id} уже привязан к другому чату Telegram')
        same_chat = next((m for m in self._tg_to_mappings.get(tg_chat_id, []) if m.bitrix_dialog_id == bitrix_dialog_id), None)
        try:
            if same_chat is not None and topic_id is not None:
                current = list(same_chat.topic_ids)
                if topic_id not in current:
                    current.append(topic_id)
                    await self.state_store.update_chat_mapping_topic_ids(same_chat.mapping_id, current)
            elif same_chat is None:
                topic_ids = [topic_id] if topic_id is not None else []
                await self.state_store.add_chat_mapping(tg_chat_id, bitrix_dialog_id, topic_ids, label)
        finally:
            await self.reload_mappings()

    async def disconnect_mapping(self, tg_chat_id: int, topic_id: int | None) -> bool:
        mapping = self.resolve_mapping_for_chat_and_thread(tg_chat_id, topic_id)
        if mapping is None:
            return False
        await self.state_store.remove_chat_mapping(mapping.mapping_id)
        await self.reload_mappings()
        return True

    def render_telegram_message(self, message: Message) -> str:
        lines: list[str] = []
        mapping = self.resolve_mapping_for_telegram_message(message)
        is_forum = getattr(message.chat, 'is_forum', False)
        if mapping is not None and self._is_multi_topic_mode(mapping) and is_forum and message.message_thread_id:
            topic_name = self._topic_names.get((message.chat_id, message.message_thread_id)) or str(message.message_thread_id)
            lines.append(f'Ветка: [B]#{topic_name}[/B]')
        if self.settings.prefix_with_sender:
            sender = self._sender_name(message)
            lines.append(f'Отправитель: [B]{sender}[/B]')
        lines.append('')
        lines.append(self._build_body(message))
        return '\n'.join(lines).strip()

    def render_bitrix_message(self, bitrix_message: BitrixMessage, sender_name: str) -> str:
        lines: list[str] = [f'Отправитель: <b>{html.escape(sender_name)}</b>']
        text = bitrix_message.text.strip()
        if text:
            lines.append('')
            lines.append(_bbcode_to_html(text))
        return '\n'.join(lines).strip()

    async def start(self, application: Application) -> None:
        self._application = application
        self._stop_event.clear()
        await self.state_store.initialize()
        self._forwarding_enabled = await self.state_store.get_forwarding_enabled()
        logger.info('Message forwarding is %s', 'enabled' if self._forwarding_enabled else 'disabled')
        self._topic_names = await self.state_store.load_topic_names()
        if not self.settings.chat_mappings:
            logger.warning('No chat mappings are configured. Add mappings via the monitoring web dashboard (/monitor) and restart the service.')
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup_loop(), name='periodic-cleanup')
        if self.settings.sync_bitrix_to_telegram:
            self._bitrix_event_offset = await self.state_store.load_bitrix_event_offset(self.settings.bitrix_bot_id)
            self._bitrix_event_task = asyncio.create_task(self._bitrix_event_loop(application), name='bitrix-event-fetcher')
        else:
            logger.info('Bitrix → Telegram sync is disabled by configuration')

    async def stop(self) -> None:
        self._stop_event.set()
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
            self._scheduler_task = None
        if self._bitrix_event_task is not None:
            self._bitrix_event_task.cancel()
            try:
                await self._bitrix_event_task
            except asyncio.CancelledError:
                pass
            self._bitrix_event_task = None
        for task in self._bitrix_on_demand_tasks.values():
            task.cancel()
        for task in self._bitrix_on_demand_tasks.values():
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._bitrix_on_demand_tasks.clear()
        for worker in self._channel_workers.values():
            worker.cancel()
        for worker in self._channel_workers.values():
            try:
                await worker
            except asyncio.CancelledError:
                pass
        self._channel_workers.clear()
        self._channel_queues.clear()
        self._application = None

    async def enqueue_telegram_message(self, message: Message) -> None:
        if not self._forwarding_enabled:
            logger.info('Dropping Telegram message %s because forwarding is disabled', message.message_id)
            return
        mapping = self.resolve_mapping_for_telegram_message(message)
        if mapping is None:
            logger.warning('No mapping found for telegram chat_id=%s thread_id=%s, dropping message', message.chat_id, message.message_thread_id)
            return
        chat_id = message.chat_id
        if chat_id not in self._channel_queues:
            self._channel_queues[chat_id] = asyncio.Queue(maxsize=self.settings.bitrix_send_queue_maxsize)
            self._channel_workers[chat_id] = asyncio.create_task(self._per_channel_worker(chat_id), name=f'channel-worker-{chat_id}')
        queue = self._channel_queues[chat_id]
        # Backpressure keeps the update in Telegram's webhook retry path instead of dropping it.
        await queue.put(message)

    async def _handle_bitrix_event(self, event: BitrixBotEvent) -> None:
        handlers = {'ONIMBOTV2MESSAGEADD': self._handle_bitrix_message_add, 'ONIMBOTV2MESSAGEUPDATE': self._handle_bitrix_message_update, 'ONIMBOTV2MESSAGEDELETE': self._handle_bitrix_message_delete, 'ONIMBOTV2REACTIONCHANGE': self._handle_bitrix_reaction_change, 'ONIMBOTV2JOINCHAT': self._handle_bitrix_join_chat}
        handler = handlers.get(event.event_type)
        if handler is not None:
            await handler(event.data)
        else:
            logger.warning('Acknowledging unhandled Bitrix event type=%r eventId=%s', event.event_type, event.event_id)

    async def _handle_bitrix_message_add(self, data: dict[str, Any]) -> None:
        chat = data.get('chat')
        message_data = data.get('message')
        user_data = data.get('user')
        if not isinstance(chat, dict) or not isinstance(message_data, dict):
            return
        dialog_id = chat.get('dialogId')
        if not isinstance(dialog_id, str):
            return
        bitrix_message = BitrixMessage.from_api_payload(message_data)
        if bitrix_message is None:
            return
        if self.settings.bitrix_bot_id and bitrix_message.author_id == self.settings.bitrix_bot_id:
            return
        text_lower = (bitrix_message.text or '').strip().lower()
        if text_lower in {'/start', 'start'}:
            reply = 'Привет. Я получил сообщение и могу отвечать в этот чат.'
            await self.bitrix.send_message(reply, dialog_id=dialog_id, reply_id=bitrix_message.message_id)
            return
        elif text_lower in {'/ping', 'ping'}:
            await self.bitrix.send_message('pong', dialog_id=dialog_id, reply_id=bitrix_message.message_id)
            return
        elif text_lower == '/tg_connect' or text_lower.startswith('/tg_connect '):
            import secrets
            import time
            token = secrets.token_hex(4)
            expires_at = int(time.time()) + 600
            try:
                await self.state_store.save_pending_connection(dialog_id, token, expires_at)
                reply = f'🔑 Одноразовый токен сгенерирован.\nОтправьте следующую команду в вашей Telegram-группе в течение 10 минут:\n\n/connect {dialog_id} {token}'
            except Exception:
                logger.exception('Failed to create pending connection token')
                reply = '⚠️ Не удалось создать токен подключения. Попробуйте позже.'
            await self.bitrix.send_message(reply, dialog_id=dialog_id, reply_id=bitrix_message.message_id)
            return
        mapping = self.get_mapping_for_bitrix_dialog(dialog_id)
        if mapping is None:
            logger.debug('Bitrix message for unmapped dialog %s, acknowledging without forwarding', dialog_id)
            return
        existing_link = await self.state_store.get_link_by_bitrix_message(bitrix_message_id=bitrix_message.message_id)
        if existing_link is not None:
            return
        if not self._forwarding_enabled:
            return
        if self._application is None:
            return
        users_by_id = {}
        if isinstance(user_data, dict):
            user = BitrixUser.from_api_payload(user_data)
            if user is not None:
                users_by_id[user.user_id] = user
        files_by_id = await self._collect_files_by_id(data, bitrix_message)
        snapshot = BitrixDialogSnapshot(messages=[bitrix_message], users_by_id=users_by_id, files_by_id=files_by_id)
        tg_chat_id = mapping.tg_chat_id
        default_thread_id = None if self._is_multi_topic_mode(mapping) else mapping.default_topic_id
        sender_name = self._resolve_bitrix_sender_name(snapshot, bitrix_message)
        reply_tg_id: int | None = None
        message_thread_id = default_thread_id
        if bitrix_message.reply_id is None and bitrix_message.message_id in self._webhook_reply_cache:
            cached_reply = self._webhook_reply_cache.pop(bitrix_message.message_id)
            bitrix_message = dataclasses.replace(bitrix_message, reply_id=cached_reply)
        if bitrix_message.reply_id is not None:
            reply_link = await self.state_store.get_link_by_bitrix_message(bitrix_message_id=bitrix_message.reply_id)
            if reply_link is not None:
                reply_tg_id = reply_link.telegram_message_id
                if reply_link.telegram_chat_id == tg_chat_id and reply_link.telegram_message_thread_id is not None:
                    message_thread_id = reply_link.telegram_message_thread_id
        try:
            forwarded = await self._forward_bitrix_message(self._application, snapshot, bitrix_message, sender_name, tg_chat_id=tg_chat_id, message_thread_id=message_thread_id, reply_to_message_id=reply_tg_id)
            logger.info('Mirrored Bitrix message %s from dialog %s to Telegram chat %s (photo=%s)', bitrix_message.message_id, dialog_id, tg_chat_id, bool(forwarded.photo))
            await self.state_store.upsert_link(telegram_chat_id=forwarded.chat_id, telegram_message_id=forwarded.message_id, bitrix_message_id=bitrix_message.message_id, origin=MirrorOrigin.BITRIX, telegram_message_date_unix=int(forwarded.date.timestamp()) if forwarded.date else None, bitrix_author_id=bitrix_message.author_id, last_seen_bitrix_revision=self._build_bitrix_revision(bitrix_message), telegram_message_thread_id=message_thread_id)
            self._forward_attempts.pop(bitrix_message.message_id, None)
        except Exception:
            attempts = self._forward_attempts.get(bitrix_message.message_id, 0) + 1
            self._forward_attempts[bitrix_message.message_id] = attempts
            logger.exception('Failed to mirror Bitrix message %s (attempt %d); leaving event unacknowledged', bitrix_message.message_id, attempts)
            raise

    async def _handle_bitrix_message_update(self, data: dict[str, Any]) -> None:
        chat = data.get('chat')
        message_data = data.get('message')
        user_data = data.get('user')
        if not isinstance(chat, dict) or not isinstance(message_data, dict):
            return
        dialog_id = chat.get('dialogId')
        if not isinstance(dialog_id, str):
            return
        bitrix_message = BitrixMessage.from_api_payload(message_data)
        if bitrix_message is None:
            return
        if self.settings.bitrix_bot_id and bitrix_message.author_id == self.settings.bitrix_bot_id:
            return
        if not self._forwarding_enabled:
            return
        if self._application is None:
            return
        link = await self.state_store.get_link_by_bitrix_message(bitrix_message_id=bitrix_message.message_id)
        if link is None:
            return
        users_by_id = {}
        if isinstance(user_data, dict):
            user = BitrixUser.from_api_payload(user_data)
            if user is not None:
                users_by_id[user.user_id] = user
        files_by_id = await self._collect_files_by_id(data, bitrix_message)
        snapshot = BitrixDialogSnapshot(messages=[bitrix_message], users_by_id=users_by_id, files_by_id=files_by_id)
        current_revision = self._build_bitrix_revision(bitrix_message)
        if link.last_seen_bitrix_revision != current_revision:
            await self._apply_bitrix_edit_to_telegram(self._application, snapshot, link, bitrix_message)
            await self.state_store.upsert_link(telegram_chat_id=link.telegram_chat_id, telegram_message_id=link.telegram_message_id, bitrix_message_id=bitrix_message.message_id, origin=link.origin, telegram_message_date_unix=link.telegram_message_date_unix, bitrix_author_id=bitrix_message.author_id, last_seen_bitrix_revision=current_revision, telegram_message_thread_id=link.telegram_message_thread_id)

    async def _handle_bitrix_message_delete(self, data: dict[str, Any]) -> None:
        message_id = data.get('messageId')
        if not isinstance(message_id, int):
            message_data = data.get('message')
            if isinstance(message_data, dict):
                message_id = message_data.get('id')
        if not isinstance(message_id, int):
            return
        link = await self.state_store.get_link_by_bitrix_message(bitrix_message_id=message_id)
        if link is None:
            return
        if self._application is None:
            return
        try:
            await self._application.bot.delete_message(chat_id=link.telegram_chat_id, message_id=link.telegram_message_id)
        except BadRequest as exc:
            if 'message to delete not found' in str(exc).lower() or 'message not found' in str(exc).lower():
                logger.warning('Telegram message to delete not found, treating as deleted.')
            else:
                raise
        await self.state_store.delete_link_by_bitrix_message(bitrix_message_id=message_id)

    async def _handle_bitrix_reaction_change(self, data: dict[str, Any]) -> None:
        reaction = data.get('reaction')
        action = data.get('action')
        message_data = data.get('message')
        user_data = data.get('user')
        if not isinstance(message_data, dict) or not isinstance(user_data, dict):
            return
        message_id = message_data.get('id')
        user_id = user_data.get('id')
        if not isinstance(message_id, int) or not isinstance(user_id, int):
            return
        if reaction != 'like':
            return
        link = await self.state_store.get_link_by_bitrix_message(bitrix_message_id=message_id)
        if link is None:
            return
        if self._application is None:
            return
        if link.bitrix_liked_by_bot and self.settings.bitrix_bot_id == user_id:
            await self.state_store.update_reaction_state(bitrix_message_id=message_id, bitrix_liked_by_bot=False, last_seen_bitrix_likes=link.last_seen_bitrix_likes)
            return
        likes_set = set()
        if link.last_seen_bitrix_likes:
            for item in link.last_seen_bitrix_likes.split(','):
                if item.strip().isdigit():
                    likes_set.add(int(item.strip()))
        if action == 'add':
            likes_set.add(user_id)
        elif action == 'delete':
            likes_set.discard(user_id)
        sorted_likes = sorted(likes_set)
        likes_str = ','.join(str(uid) for uid in sorted_likes)
        await self._sync_bitrix_reaction_to_telegram(self._application, link, bool(likes_set))
        await self.state_store.update_reaction_state(bitrix_message_id=message_id, bitrix_liked_by_bot=link.bitrix_liked_by_bot, last_seen_bitrix_likes=likes_str)

    async def _handle_bitrix_join_chat(self, data: dict[str, Any]) -> None:
        dialog_id = data.get('dialogId')
        if not isinstance(dialog_id, str):
            dialog_id = data.get('chat', {}).get('dialogId')
        if not isinstance(dialog_id, str):
            return
        bot_id = self.settings.bitrix_bot_id
        if bot_id:
            await self.bitrix.send_message('Привет. Бот подключён и готов к работе.', dialog_id=dialog_id)

    async def sync_telegram_edit(self, message: Message) -> None:
        if not self._forwarding_enabled:
            logger.info('Skipping Telegram edit %s from chat %s because forwarding is disabled', message.message_id, message.chat_id)
            return
        link = await self.state_store.get_link_by_telegram_message(telegram_chat_id=message.chat_id, telegram_message_id=message.message_id)
        if link is None:
            logger.debug('Skipping Telegram edit %s because no Bitrix mapping was found', message.message_id)
            return
        try:
            await self.bitrix.update_message(message_id=link.bitrix_message_id, text=self.render_telegram_message(message))
        except RuntimeError as exc:
            if 'BITRIX_ACCESS_DENIED' in str(exc):
                logger.warning('Cannot edit Bitrix message %s (created by previous bot before Vibe migration); skipping edit mirror', link.bitrix_message_id)
                return
            raise
        logger.info('Mirrored Telegram edit %s from chat %s to Bitrix message %s', message.message_id, message.chat_id, link.bitrix_message_id)

    async def sync_telegram_reaction(self, chat_id: int, message_id: int, has_reactions: bool) -> None:
        if not self._forwarding_enabled:
            logger.info('Skipping Telegram reaction for message %s in chat %s because forwarding is disabled', message_id, chat_id)
            return
        link = await self.state_store.get_link_by_telegram_message(telegram_chat_id=chat_id, telegram_message_id=message_id)
        if link is None:
            logger.debug('Skipping Telegram reaction for message %s because no Bitrix mapping was found', message_id)
            return
        if has_reactions and link.bitrix_liked_by_bot:
            logger.debug('Skipping Telegram reaction for message %s because Bitrix message %s is already liked by bot', message_id, link.bitrix_message_id)
            return
        try:
            await self.bitrix.set_message_like(link.bitrix_message_id, liked=has_reactions)
            await self.state_store.update_reaction_state(bitrix_message_id=link.bitrix_message_id, bitrix_liked_by_bot=has_reactions, last_seen_bitrix_likes=link.last_seen_bitrix_likes)
            if has_reactions:
                logger.info('Mirrored Telegram reaction on message %s in chat %s to Bitrix like on message %s', message_id, chat_id, link.bitrix_message_id)
            else:
                logger.info('Mirrored Telegram reaction removal on message %s in chat %s to Bitrix unlike on message %s', message_id, chat_id, link.bitrix_message_id)
        except Exception:
            logger.exception('Failed to mirror Telegram reaction on message %s to Bitrix message %s', message_id, link.bitrix_message_id)




    async def _collect_files_by_id(self, data: dict[str, Any], bitrix_message: BitrixMessage) -> dict[int, BitrixFile]:
        """Build file_id -> BitrixFile from the event payload, filling gaps via disk metadata.

        Vibe events may or may not inline `data.files`; for any attachment the
        event does not describe, fall back to GET /files/:id (named metadata),
        and finally to an anonymous placeholder so forwarding still proceeds.
        """
        files_by_id: dict[int, BitrixFile] = {}
        raw_files = data.get('files')
        if isinstance(raw_files, dict):
            for v in raw_files.values():
                if isinstance(v, dict):
                    f = BitrixFile.from_api_payload(v)
                    if f is not None:
                        files_by_id[f.file_id] = f
        elif isinstance(raw_files, list):
            for v in raw_files:
                if isinstance(v, dict):
                    f = BitrixFile.from_api_payload(v)
                    if f is not None:
                        files_by_id[f.file_id] = f
        for fid in bitrix_message.file_ids:
            if fid in files_by_id:
                continue
            meta = await self.bitrix.get_file_meta(fid)
            if meta is not None:
                files_by_id[fid] = meta
            else:
                files_by_id[fid] = BitrixFile(file_id=fid, name=f'file_{fid}', url_download=None, mime_type=None, file_type='file', is_image=False, author_id=bitrix_message.author_id)
        return files_by_id

    async def _bitrix_event_loop(self, application: Application) -> None:
        logger.info('Starting Bitrix event fetch loop')
        backoff = self.settings.bitrix_poll_error_backoff_seconds
        while not self._stop_event.is_set():
            try:
                has_more = await self._fetch_bitrix_events_once()
                self._poll_error_count = 0
                backoff = self.settings.bitrix_poll_error_backoff_seconds
                if not has_more:
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=self.settings.bitrix_poll_interval_seconds)
                    except TimeoutError:
                        pass
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception('Error in Bitrix event fetch loop')
                self._poll_error_count += 1
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, self.settings.bitrix_poll_max_backoff_seconds)

    async def _fetch_bitrix_events_once(self) -> bool:
        page = await self.bitrix.get_bot_events(offset=self._bitrix_event_offset)
        for event in page.events:
            await self._handle_bitrix_event(event)
            self._bitrix_event_offset = event.event_id + 1
            await self.state_store.save_bitrix_event_offset(self.settings.bitrix_bot_id, self._bitrix_event_offset)
        if not page.events and page.next_offset is not None:
            self._bitrix_event_offset = page.next_offset
            await self.state_store.save_bitrix_event_offset(self.settings.bitrix_bot_id, self._bitrix_event_offset)
        return page.has_more

    async def _per_channel_worker(self, chat_id: int) -> None:
        queue = self._channel_queues[chat_id]
        min_interval = 0.2
        last_send_time = 0.0
        retry_message: Message | None = None
        while not self._stop_event.is_set():
            try:
                if retry_message is None:
                    message = await queue.get()
                else:
                    message = retry_message
                    retry_message = None
            except asyncio.CancelledError:
                break
            mapping = None
            try:
                if not self._forwarding_enabled:
                    logger.info('Dropping queued message %s from chat %s because forwarding is disabled', message.message_id, chat_id)
                    continue
                now = asyncio.get_running_loop().time()
                elapsed = now - last_send_time
                if elapsed < min_interval:
                    await asyncio.sleep(min_interval - elapsed)
                    now = asyncio.get_running_loop().time()
                last_send_time = now
                mapping = self.resolve_mapping_for_telegram_message(message)
                if mapping is None:
                    continue
                dialog_id = mapping.bitrix_dialog_id
                reply_bitrix_id: int | None = None
                if message.reply_to_message:
                    is_topic_header_reply = message.message_thread_id is not None and message.reply_to_message.message_id == message.message_thread_id
                    if not is_topic_header_reply:
                        reply_link = await self.state_store.get_link_by_telegram_message(telegram_chat_id=message.chat_id, telegram_message_id=message.reply_to_message.message_id)
                        if reply_link is not None:
                            reply_bitrix_id = reply_link.bitrix_message_id
                rendered = self.render_telegram_message(message)
                parts = [rendered[i:i + _BITRIX_MESSAGE_LIMIT] for i in range(0, len(rendered), _BITRIX_MESSAGE_LIMIT)] or ['']
                bitrix_message_ids: list[int] = []
                if self._has_uploadable_file(message):
                    bitrix_message_ids.append(await self._forward_telegram_file_to_bitrix(message, dialog_id=dialog_id, reply_id=reply_bitrix_id, caption=parts.pop(0)))
                    reply_bitrix_id = None
                for part in parts:
                    bitrix_message_ids.append(await self.bitrix.send_message(part, dialog_id=dialog_id, reply_id=reply_bitrix_id))
                    reply_bitrix_id = None
                await self.state_store.upsert_link(telegram_chat_id=message.chat_id, telegram_message_id=message.message_id, bitrix_message_id=bitrix_message_ids[0], origin=MirrorOrigin.TELEGRAM, telegram_message_date_unix=int(message.date.timestamp()) if message.date else None, bitrix_author_id=None, last_seen_bitrix_revision='telegram-origin', telegram_message_thread_id=message.message_thread_id)
                self._tg_forward_dead_letters.pop(chat_id, None)
            except Exception:
                dead_count = self._tg_forward_dead_letters.get(chat_id, 0) + 1
                self._tg_forward_dead_letters[chat_id] = dead_count
                logger.exception('Failed to mirror Telegram message %s to Bitrix (chat=%s, dialog=%s, retry_count=%d)', message.message_id, chat_id, mapping.bitrix_dialog_id if mapping else 'unknown', dead_count)
                await asyncio.sleep(min(self.settings.bitrix_retry_base_delay_seconds * 2 ** min(dead_count - 1, 10), self.settings.bitrix_retry_max_delay_seconds))
                retry_message = message
            finally:
                if retry_message is None:
                    queue.task_done()
    async def _should_forward_bitrix_message(self, dialog_id: str, bitrix_message: BitrixMessage) -> bool:
        if self.settings.bitrix_bot_id and bitrix_message.author_id == self.settings.bitrix_bot_id:
            logger.debug('Ignoring Bitrix message %s from our own bot (author_id=%s)', bitrix_message.message_id, bitrix_message.author_id)
            return False
        last_seen = self._last_seen_bitrix_message_ids.get(dialog_id)
        if last_seen is not None and bitrix_message.message_id <= last_seen:
            self._forward_attempts.pop(bitrix_message.message_id, None)
            return False
        existing_link = await self.state_store.get_link_by_bitrix_message(bitrix_message_id=bitrix_message.message_id)
        if existing_link is not None:
            return False
        if bitrix_message.author_id == 0:
            logger.debug('Ignoring Bitrix service message %s from author_id=0', bitrix_message.message_id)
            return False
        text_lower = bitrix_message.text.lower().strip()
        if text_lower == '/tg_connect' or text_lower.startswith('/tg_connect '):
            logger.debug('Ignoring Bitrix /tg_connect command message %s', bitrix_message.message_id)
            return False
        if bitrix_message.is_sticker:
            logger.debug('Ignoring Bitrix sticker message %s', bitrix_message.message_id)
            return False
        if bitrix_message.is_meeting:
            logger.debug('Ignoring Bitrix meeting card %s', bitrix_message.message_id)
            return False
        if bitrix_message.is_task:
            logger.debug('Ignoring Bitrix task card %s', bitrix_message.message_id)
            return False
        if not bitrix_message.text.strip() and (not bitrix_message.file_ids):
            return False
        link = await self.state_store.get_link_by_bitrix_message(bitrix_message_id=bitrix_message.message_id)
        if link is not None and link.origin == MirrorOrigin.TELEGRAM:
            logger.debug('Suppressing Bitrix message %s because it is mapped to Telegram-origin message %s', bitrix_message.message_id, link.telegram_message_id)
            return False
        return True

    def _has_uploadable_file(self, message: Message) -> bool:
        return bool(message.photo or message.document or message.video or message.audio)

    async def _forward_telegram_file_to_bitrix(self, message: Message, *, dialog_id: str, reply_id: int | None=None, caption: str | None=None) -> int:
        if message.photo:
            file_source: Any = message.photo[-1]
            original_name = None
            fallback_name = f'photo_{message.message_id}.jpg'
        elif message.document:
            file_source = message.document
            original_name = message.document.file_name
            fallback_name = f'document_{message.message_id}'
        elif message.video:
            file_source = message.video
            original_name = message.video.file_name
            fallback_name = f'video_{message.message_id}.mp4'
        elif message.audio:
            file_source = message.audio
            original_name = message.audio.file_name
            fallback_name = f'audio_{message.message_id}.ogg'
        else:
            raise ValueError('No uploadable file attachment found in message')
        telegram_file = await file_source.get_file()
        file_bytes = await telegram_file.download_as_bytearray()
        cap = min(self.settings.max_file_size_bytes, self.settings.bitrix_max_upload_file_bytes)
        if len(file_bytes) > cap:
            logger.warning('Telegram file too large (%s bytes > %s max), skipping upload for message %s', len(file_bytes), cap, message.message_id)
            return await self.bitrix.send_message(self.render_telegram_message(message) + '\n\n[Файл слишком большой для пересылки]', dialog_id=dialog_id, reply_id=reply_id)
        file_path_name = telegram_file.file_path.rsplit('/', 1)[-1] if telegram_file.file_path else None
        filename = original_name or file_path_name or fallback_name
        return await self.bitrix.send_photo(caption=caption if caption is not None else self.render_telegram_message(message), filename=filename, content=bytes(file_bytes), dialog_id=dialog_id)

    async def _forward_bitrix_message(self, application: Application, snapshot: BitrixDialogSnapshot, bitrix_message: BitrixMessage, sender_name: str, *, tg_chat_id: int, message_thread_id: int | None=None, reply_to_message_id: int | None=None) -> Message:

        async def _send_with_thread_fallback(send_callable: Any, requested_reply_id: int | None=reply_to_message_id) -> Message:
            try:
                return cast(Message, await send_callable(message_thread_id, requested_reply_id))
            except BadRequest as exc:
                if message_thread_id is None or 'Message thread not found' not in str(exc):
                    raise
                logger.warning('Telegram thread_id=%s not found for Bitrix message %s in chat %s; falling back to main feed', message_thread_id, bitrix_message.message_id, tg_chat_id)
                return cast(Message, await send_callable(None, None))
        rendered = self.render_bitrix_message(bitrix_message, sender_name=sender_name)
        async def send_text_parts(text: str, *, first_reply_id: int | None = reply_to_message_id) -> Message:
            parse_mode: str | None = 'HTML'
            if len(text) > _TELEGRAM_MESSAGE_LIMIT:
                text = html.unescape(re.sub(r'<[^>]+>', '', text))
                parse_mode = None
            parts = [text[i:i + _TELEGRAM_MESSAGE_LIMIT] for i in range(0, len(text), _TELEGRAM_MESSAGE_LIMIT)] or ['']
            first: Message | None = None
            current_reply_id = first_reply_id
            for part in parts:
                sent = await _send_with_thread_fallback(lambda thread_id, reply_id, part=part: application.bot.send_message(chat_id=tg_chat_id, message_thread_id=thread_id, reply_to_message_id=reply_id, text=part, parse_mode=parse_mode, disable_web_page_preview=self.settings.disable_link_preview), current_reply_id)
                if first is None:
                    first = sent
                current_reply_id = None
            assert first is not None
            return first
        attachment = self._select_bitrix_file(snapshot, bitrix_message)
        if attachment is None or not attachment.url_download:
            return await send_text_parts(rendered)
        try:
            file_bytes = await self.bitrix.download_file_by_id(attachment.file_id, fallback_url=attachment.url_download)
            if len(file_bytes) > self.settings.max_file_size_bytes:
                logger.warning('Bitrix file too large (%s bytes > %s max), sending text only for message %s', len(file_bytes), self.settings.max_file_size_bytes, bitrix_message.message_id)
                return await send_text_parts(rendered + '\n\n[Файл слишком большой для пересылки]')
            if len(rendered) > _TELEGRAM_CAPTION_LIMIT:
                send_caption = None
            else:
                send_caption = rendered or None
            if attachment.is_image:
                sent = await _send_with_thread_fallback(lambda thread_id, reply_id: application.bot.send_photo(chat_id=tg_chat_id, message_thread_id=thread_id, reply_to_message_id=reply_id, photo=BytesIO(file_bytes), filename=attachment.name, caption=send_caption, parse_mode='HTML'))
            elif attachment.file_type == 'video' or (attachment.mime_type or '').startswith('video/'):
                sent = await _send_with_thread_fallback(lambda thread_id, reply_id: application.bot.send_video(chat_id=tg_chat_id, message_thread_id=thread_id, reply_to_message_id=reply_id, video=BytesIO(file_bytes), filename=attachment.name, caption=send_caption, parse_mode='HTML'))
            elif attachment.file_type == 'audio' or (attachment.mime_type or '').startswith('audio/'):
                sent = await _send_with_thread_fallback(lambda thread_id, reply_id: application.bot.send_audio(chat_id=tg_chat_id, message_thread_id=thread_id, reply_to_message_id=reply_id, audio=BytesIO(file_bytes), filename=attachment.name, caption=send_caption, parse_mode='HTML'))
            else:
                sent = await _send_with_thread_fallback(lambda thread_id, reply_id: application.bot.send_document(chat_id=tg_chat_id, message_thread_id=thread_id, reply_to_message_id=reply_id, document=BytesIO(file_bytes), filename=attachment.name, caption=send_caption, parse_mode='HTML'))
            if send_caption is None:
                await send_text_parts(rendered, first_reply_id=None)
            return sent
        except Exception:
            logger.exception('Failed to forward Bitrix file for message %s, falling back to text', bitrix_message.message_id)
            return await send_text_parts(rendered)

    def _select_bitrix_file(self, snapshot: BitrixDialogSnapshot, bitrix_message: BitrixMessage) -> BitrixFile | None:
        for file_id in bitrix_message.file_ids:
            file = snapshot.files_by_id.get(file_id)
            if file:
                return file
        if not bitrix_message.file_ids and bitrix_message.author_id is not None:
            for file in snapshot.files_by_id.values():
                if file.author_id is not None and file.author_id == bitrix_message.author_id:
                    logger.debug('Matched file %s to message %s by author_id=%s (fallback heuristic)', file.file_id, bitrix_message.message_id, bitrix_message.author_id)
                    return file
        return None

    def _resolve_bitrix_sender_name(self, snapshot: BitrixDialogSnapshot, bitrix_message: BitrixMessage) -> str:
        if bitrix_message.author_id is None:
            return 'Неизвестный отправитель'
        user = snapshot.users_by_id.get(bitrix_message.author_id)
        if user is not None:
            return user.display_name
        logger.warning('Bitrix author_id=%s for message_id=%s is missing in users directory', bitrix_message.author_id, bitrix_message.message_id)
        return f'Bitrix user_id: {bitrix_message.author_id}'

    async def _sync_bitrix_reaction_to_telegram(self, application: Application, link: MessageMirrorLink, has_likes: bool) -> None:
        try:
            if has_likes:
                await application.bot.set_message_reaction(chat_id=link.telegram_chat_id, message_id=link.telegram_message_id, reaction=[ReactionTypeEmoji(emoji='👍')])
                logger.info('Mirrored Bitrix like to Telegram reaction on message %s in chat %s', link.telegram_message_id, link.telegram_chat_id)
            else:
                await application.bot.set_message_reaction(chat_id=link.telegram_chat_id, message_id=link.telegram_message_id, reaction=[])
                logger.info('Removed Telegram reaction on message %s in chat %s (Bitrix likes removed)', link.telegram_message_id, link.telegram_chat_id)
        except BadRequest as exc:
            logger.warning('Telegram rejected reaction update for message_id=%s chat_id=%s: %s', link.telegram_message_id, link.telegram_chat_id, str(exc))

    async def _apply_bitrix_edit_to_telegram(self, application: Application, snapshot: BitrixDialogSnapshot, link: MessageMirrorLink, bitrix_message: BitrixMessage) -> None:
        sender_name = self._resolve_bitrix_sender_name(snapshot, bitrix_message)
        rendered = self.render_bitrix_message(bitrix_message, sender_name=sender_name)
        photo = self._select_bitrix_file(snapshot, bitrix_message)
        try:
            if photo is None:
                await application.bot.edit_message_text(chat_id=link.telegram_chat_id, message_id=link.telegram_message_id, text=rendered, parse_mode='HTML', disable_web_page_preview=self.settings.disable_link_preview)
                logger.info('Mirrored Bitrix edit %s to Telegram message %s', bitrix_message.message_id, link.telegram_message_id)
                return
            await application.bot.edit_message_caption(chat_id=link.telegram_chat_id, message_id=link.telegram_message_id, caption=rendered, parse_mode='HTML')
            logger.info('Mirrored Bitrix caption edit %s to Telegram message %s', bitrix_message.message_id, link.telegram_message_id)
        except ChatMigrated as exc:
            await self._cleanup_migrated_chat_links(old_chat_id=link.telegram_chat_id, new_chat_id=exc.new_chat_id)
            raise
        except BadRequest as exc:
            logger.warning('Telegram rejected Bitrix edit for bitrix_message_id=%s telegram_message_id=%s chat_id=%s: %s', bitrix_message.message_id, link.telegram_message_id, link.telegram_chat_id, str(exc))
            if 'message to edit not found' in str(exc).lower():
                logger.warning('Removing stale Bitrix-origin link for bitrix_message_id=%s because Telegram message %s in chat %s no longer exists', bitrix_message.message_id, link.telegram_message_id, link.telegram_chat_id)
                await self.state_store.delete_link_by_bitrix_message(bitrix_message_id=bitrix_message.message_id)
            elif 'message is not modified' in str(exc).lower():
                logger.debug('Ignoring no-op Bitrix edit for bitrix_message_id=%s because Telegram content is already актуален', bitrix_message.message_id)
                return
            else:
                logger.warning('Removing Bitrix-origin link for bitrix_message_id=%s after generic Telegram BadRequest to prevent repeated blocking edit failures', bitrix_message.message_id)
                await self.state_store.delete_link_by_bitrix_message(bitrix_message_id=bitrix_message.message_id)
            raise

    async def _cleanup_migrated_chat_links(self, *, old_chat_id: int, new_chat_id: int) -> None:
        logger.warning('Telegram chat migrated from %s to %s. Removing stale links for old chat id.', old_chat_id, new_chat_id)
        await self.state_store.delete_links_by_telegram_chat(telegram_chat_id=old_chat_id)

    def _sender_name(self, message: Message) -> str:
        if message.sender_chat:
            title = message.sender_chat.title or 'Анонимный администратор'
            if message.author_signature:
                return f'{title} ({message.author_signature})'
            return title
        if message.from_user:
            if message.from_user.username == 'GroupAnonymousBot':
                if message.author_signature:
                    return f'Анонимный администратор ({message.author_signature})'
                return 'Анонимный администратор'
            full_name = message.from_user.full_name.strip()
            username = message.from_user.username
            if username:
                return f'{full_name} (@{username})'
            return full_name  # type: ignore[no-any-return]
        return 'Неизвестный отправитель'

    def _build_body(self, message: Message) -> str:
        text = self._extract_primary_text(message)
        extra = self._describe_attachments(message)
        parts: list[str] = []
        if text:
            parts.append(text)
        if extra:
            if parts:
                parts.append('')
            parts.append(extra)
        if not parts:
            if self._has_uploadable_file(message):
                return ''
            return '[Сообщение без поддерживаемого текста или вложения]'
        return '\n'.join(parts)

    def _extract_primary_text(self, message: Message) -> str:
        if message.text:
            return message.text  # type: ignore[no-any-return]
        if message.caption:
            return message.caption  # type: ignore[no-any-return]
        return ''

    def _describe_attachments(self, message: Message) -> str:
        chunks: list[str] = []
        if message.sticker:
            sticker = message.sticker
            label = sticker.emoji or '[Стикер]'
            chunks.append(label)
        if message.contact:
            contact = message.contact
            contact_name = ' '.join(part for part in [contact.first_name, contact.last_name or ''] if part).strip()
            chunks.append(f'[Контакт] {contact_name} | {contact.phone_number}')
        if message.location:
            location = message.location
            chunks.append(f'[Локация] https://maps.google.com/maps?q={location.latitude},{location.longitude}')
        if message.poll:
            poll = message.poll
            options = ', '.join(opt.text for opt in poll.options)
            chunks.append(f'[Опрос] {poll.question} | {options}')
        return '\n'.join(chunks)

    def _shorten(self, value: str, limit: int) -> str:
        clean = ' '.join(value.split())
        if len(clean) <= limit:
            return clean
        return clean[:limit - 1] + '…'

    def _build_bitrix_revision(self, bitrix_message: BitrixMessage) -> str:
        digest = hashlib.sha256()
        digest.update(bitrix_message.text.encode('utf-8', errors='ignore'))
        digest.update(b'|')
        digest.update(';'.join(str(file_id) for file_id in bitrix_message.file_ids).encode('ascii', errors='ignore'))
        return digest.hexdigest()

    async def _periodic_cleanup_loop(self) -> None:
        """Run DB cleanup and file cache cleanup every hour."""
        cleanup_interval = 3600
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=cleanup_interval)
                break
            except TimeoutError:
                pass
            try:
                deleted = await self.state_store.cleanup_old_links(max_age_seconds=self.settings.db_cleanup_max_age_seconds)
                if deleted:
                    logger.info('Periodic DB cleanup: removed %s old link(s)', deleted)
                await asyncio.to_thread(self._cleanup_file_cache_sync)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Periodic cleanup failed')

    def _cleanup_file_cache_sync(self) -> None:
        """Remove oldest files from file_cache_dir if total size exceeds file_cache_max_bytes."""
        cache_dir = self.settings.file_cache_dir
        if not cache_dir:
            return
        cache_path = Path(cache_dir)
        if not cache_path.is_dir():
            return
        max_bytes = self.settings.file_cache_max_bytes
        if max_bytes <= 0:
            return
        files: list[tuple[float, int, Path]] = []
        total_size = 0
        for entry in cache_path.iterdir():
            if entry.is_file():
                stat = entry.stat()
                files.append((stat.st_mtime, stat.st_size, entry))
                total_size += stat.st_size
        if total_size <= max_bytes:
            return
        files.sort(key=lambda x: x[0])
        removed = 0
        for mtime, size, fpath in files:
            if total_size <= max_bytes:
                break
            try:
                fpath.unlink()
                total_size -= size
                removed += 1
            except OSError:
                pass
        if removed:
            logger.info('File cache cleanup: removed %s file(s), cache now ~%s MB', removed, total_size // (1024 * 1024))
