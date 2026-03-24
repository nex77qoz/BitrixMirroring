from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
from io import BytesIO
from pathlib import Path
from typing import Optional

from telegram import Message, ReactionTypeEmoji
from telegram.error import BadRequest, ChatMigrated
from telegram.ext import Application

from bitrix_client import BitrixClient
from mirror_state_store import MirrorStateStore
from models import BitrixDialogSnapshot, BitrixFile, BitrixMessage, CursorState, MessageMirrorLink, MirrorOrigin
from settings import ChatMapping, Settings

logger = logging.getLogger("tg-bitrix-mirror")


class MirrorService:
    def __init__(
        self,
        settings: Settings,
        bitrix: BitrixClient,
        state_store: MirrorStateStore,
    ) -> None:
        self.settings = settings
        self.bitrix = bitrix
        self.state_store = state_store
        self._last_seen_bitrix_message_ids: dict[str, Optional[int]] = {}
        self._bitrix_poll_tasks: list[asyncio.Task[None]] = []
        self._telegram_to_bitrix_workers: list[asyncio.Task[None]] = []
        self._stop_event = asyncio.Event()
        self._state_lock = asyncio.Lock()
        self._send_queue: asyncio.Queue[Message] = asyncio.Queue(maxsize=settings.bitrix_send_queue_maxsize)
        self._tg_to_mapping: dict[int, ChatMapping] = {m.tg_chat_id: m for m in settings.chat_mappings}
        self._bitrix_to_mapping: dict[str, ChatMapping] = {m.bitrix_dialog_id: m for m in settings.chat_mappings}
        self._cleanup_task: Optional[asyncio.Task[None]] = None
        self._application: Optional[Application] = None
        self._bitrix_sync_locks: dict[str, asyncio.Lock] = {}
        self._bitrix_on_demand_tasks: dict[str, asyncio.Task[None]] = {}

    def get_mapping_for_telegram_chat(self, tg_chat_id: int) -> Optional[ChatMapping]:
        return self._tg_to_mapping.get(tg_chat_id)

    def get_mapping_for_bitrix_dialog(self, dialog_id: str) -> Optional[ChatMapping]:
        return self._bitrix_to_mapping.get(dialog_id)

    def is_allowed_chat(self, message: Message) -> bool:
        return message.chat_id in self._tg_to_mapping

    def render_telegram_message(self, message: Message) -> str:
        lines: list[str] = ["Сообщение из Телеграм"]

        if self.settings.prefix_with_chat_title:
            chat_title = message.chat.title or message.chat.full_name or str(message.chat_id)
            lines.append(f"Чат: {chat_title}")
            if message.message_thread_id:
                lines.append(f"Тема форума: {message.message_thread_id}")

        if self.settings.prefix_with_sender:
            sender = self._sender_name(message)
            lines.append(f"Отправитель: {sender}")

        if message.reply_to_message:
            reply_sender = self._sender_name(message.reply_to_message)
            reply_excerpt = self._shorten(self._extract_primary_text(message.reply_to_message), 120)
            if reply_excerpt:
                lines.append(f"Ответ на: {reply_sender} — {reply_excerpt}")
            else:
                lines.append(f"Ответ на сообщение от: {reply_sender}")

        lines.append("")
        lines.append(self._build_body(message))
        return "\n".join(lines).strip()

    def render_bitrix_message(self, bitrix_message: BitrixMessage, sender_name: str) -> str:
        lines: list[str] = [
            "Сообщение из Битрикс",
            f"Отправитель: {sender_name}",
        ]
        text = bitrix_message.text.strip()
        if text:
            lines.append("")
            lines.append(text)
        return "\n".join(lines).strip()

    async def start(self, application: Application) -> None:
        self._application = application
        self._stop_event.clear()
        await self.state_store.initialize()
        await self._cleanup_stale_chat_links()
        self._telegram_to_bitrix_workers = [
            asyncio.create_task(self._telegram_to_bitrix_worker(), name=f"bitrix-send-worker-{index}")
            for index in range(self.settings.bitrix_send_workers)
        ]
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup_loop(), name="periodic-cleanup")
        await self.start_bitrix_polling(application)

    async def stop(self) -> None:
        self._stop_event.set()
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
        for task in self._bitrix_poll_tasks:
            await task
        self._bitrix_poll_tasks.clear()
        for task in self._bitrix_on_demand_tasks.values():
            task.cancel()
        for task in self._bitrix_on_demand_tasks.values():
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._bitrix_on_demand_tasks.clear()
        for worker in self._telegram_to_bitrix_workers:
            worker.cancel()
        for worker in self._telegram_to_bitrix_workers:
            try:
                await worker
            except asyncio.CancelledError:
                pass
        self._telegram_to_bitrix_workers.clear()
        self._application = None

    async def enqueue_telegram_message(self, message: Message) -> None:
        await self._send_queue.put(message)

    async def schedule_bitrix_dialog_sync(self, dialog_id: str, *, trigger: str) -> bool:
        if not self.settings.sync_bitrix_to_telegram:
            return False
        if self._application is None:
            logger.warning("Dropping Bitrix webhook for dialog %s because application is not ready", dialog_id)
            return False
        mapping = self.get_mapping_for_bitrix_dialog(dialog_id)
        if mapping is None:
            logger.debug("Ignoring Bitrix webhook for unmapped dialog %s", dialog_id)
            return False

        existing = self._bitrix_on_demand_tasks.get(dialog_id)
        if existing is not None and not existing.done():
            logger.debug("Bitrix sync already scheduled for dialog %s; coalescing trigger=%s", dialog_id, trigger)
            return True

        task = asyncio.create_task(
            self._sync_bitrix_dialog(self._application, mapping, trigger=trigger),
            name=f"bitrix-webhook-sync-{dialog_id}",
        )
        self._bitrix_on_demand_tasks[dialog_id] = task

        def _clear_task(completed: asyncio.Task[None]) -> None:
            current = self._bitrix_on_demand_tasks.get(dialog_id)
            if current is completed:
                self._bitrix_on_demand_tasks.pop(dialog_id, None)
            try:
                completed.result()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("On-demand Bitrix sync failed for dialog %s", dialog_id)

        task.add_done_callback(_clear_task)
        return True

    async def sync_telegram_edit(self, message: Message) -> None:
        link = await self.state_store.get_link_by_telegram_message(
            telegram_chat_id=message.chat_id,
            telegram_message_id=message.message_id,
        )
        if link is None:
            logger.debug("Skipping Telegram edit %s because no Bitrix mapping was found", message.message_id)
            return
        await self.bitrix.update_message(
            message_id=link.bitrix_message_id,
            text=self.render_telegram_message(message),
        )
        logger.info(
            "Mirrored Telegram edit %s from chat %s to Bitrix message %s",
            message.message_id,
            message.chat_id,
            link.bitrix_message_id,
        )

    async def sync_telegram_reaction(self, chat_id: int, message_id: int, has_reactions: bool) -> None:
        link = await self.state_store.get_link_by_telegram_message(
            telegram_chat_id=chat_id,
            telegram_message_id=message_id,
        )
        if link is None:
            logger.debug("Skipping Telegram reaction for message %s because no Bitrix mapping was found", message_id)
            return

        if has_reactions and link.bitrix_liked_by_bot:
            logger.debug(
                "Skipping Telegram reaction for message %s because Bitrix message %s is already liked by bot",
                message_id,
                link.bitrix_message_id,
            )
            return

        try:
            await self.bitrix.set_message_like(link.bitrix_message_id, liked=has_reactions)
            await self.state_store.update_reaction_state(
                bitrix_message_id=link.bitrix_message_id,
                bitrix_liked_by_bot=True,
                last_seen_bitrix_likes=link.last_seen_bitrix_likes,
            )
            if has_reactions:
                logger.info(
                    "Mirrored Telegram reaction on message %s in chat %s to Bitrix like on message %s",
                    message_id,
                    chat_id,
                    link.bitrix_message_id,
                )
            else:
                logger.info(
                    "Mirrored Telegram reaction removal on message %s in chat %s to Bitrix unlike on message %s",
                    message_id,
                    chat_id,
                    link.bitrix_message_id,
                )
        except Exception:
            logger.exception(
                "Failed to mirror Telegram reaction on message %s to Bitrix message %s",
                message_id,
                link.bitrix_message_id,
            )

    async def start_bitrix_polling(self, application: Application) -> None:
        if not self.settings.sync_bitrix_to_telegram:
            logger.info("Bitrix → Telegram sync is disabled by configuration")
            return
        if not self.settings.chat_mappings:
            logger.warning("Bitrix → Telegram sync is disabled because no chat mappings are configured")
            return
        for index, mapping in enumerate(self.settings.chat_mappings):
            # Stagger poll starts to avoid hitting Bitrix rate limits
            stagger_delay = index * 0.7
            task = asyncio.create_task(
                self._bitrix_poll_loop(application, mapping, stagger_delay),
                name=f"bitrix-poll-{mapping.bitrix_dialog_id}",
            )
            self._bitrix_poll_tasks.append(task)

    async def _telegram_to_bitrix_worker(self) -> None:
        while not self._stop_event.is_set():
            try:
                message = await self._send_queue.get()
            except asyncio.CancelledError:
                raise

            try:
                mapping = self.get_mapping_for_telegram_chat(message.chat_id)
                if mapping is None:
                    logger.warning("No mapping found for telegram chat_id=%s, dropping message", message.chat_id)
                    continue
                dialog_id = mapping.bitrix_dialog_id
                if self._has_uploadable_file(message):
                    bitrix_message_id = await self._forward_telegram_file_to_bitrix(message, dialog_id=dialog_id)
                else:
                    bitrix_message_id = await self.bitrix.send_message(
                        self.render_telegram_message(message), dialog_id=dialog_id,
                    )
                await self.state_store.upsert_link(
                    telegram_chat_id=message.chat_id,
                    telegram_message_id=message.message_id,
                    bitrix_message_id=bitrix_message_id,
                    origin=MirrorOrigin.TELEGRAM,
                    telegram_message_date_unix=int(message.date.timestamp()) if message.date else None,
                    bitrix_author_id=None,
                    last_seen_bitrix_revision="telegram-origin",
                )
                logger.info(
                    "Mirrored Telegram message %s from chat %s to Bitrix dialog %s as message %s",
                    message.message_id,
                    message.chat_id,
                    dialog_id,
                    bitrix_message_id,
                )
            except Exception:
                logger.exception("Failed to mirror Telegram message %s from chat %s", message.message_id, message.chat_id)
            finally:
                self._send_queue.task_done()

    async def _bitrix_poll_loop(self, application: Application, mapping: ChatMapping, stagger_delay: float = 0) -> None:
        if stagger_delay > 0:
            await asyncio.sleep(stagger_delay)
        dialog_id = mapping.bitrix_dialog_id
        logger.info(
            "Starting Bitrix polling every %s seconds for dialog %s -> tg_chat %s",
            self.settings.bitrix_poll_interval_seconds,
            dialog_id,
            mapping.tg_chat_id,
        )
        backoff = self.settings.bitrix_poll_error_backoff_seconds
        try:
            await self._initialize_bitrix_cursor(mapping)
            while not self._stop_event.is_set():
                try:
                    await self._sync_bitrix_dialog(application, mapping, trigger="poll")
                    backoff = self.settings.bitrix_poll_error_backoff_seconds
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.settings.bitrix_poll_interval_seconds)
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Bitrix polling iteration failed for dialog %s, retrying in %.1fs", dialog_id, backoff)
                    await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                    backoff = min(backoff * 2, self.settings.bitrix_poll_max_backoff_seconds)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            raise
        finally:
            logger.info("Bitrix polling loop stopped for dialog %s", dialog_id)

    async def _sync_bitrix_dialog(self, application: Application, mapping: ChatMapping, *, trigger: str) -> None:
        dialog_id = mapping.bitrix_dialog_id
        lock = self._bitrix_sync_locks.setdefault(dialog_id, asyncio.Lock())
        async with lock:
            logger.debug("Running Bitrix sync for dialog %s via trigger=%s", dialog_id, trigger)
            await self._sync_bitrix_messages(application, mapping)

    async def _initialize_bitrix_cursor(self, mapping: ChatMapping) -> None:
        dialog_id = mapping.bitrix_dialog_id
        persisted_state = await self.state_store.load_cursor(dialog_id)
        if persisted_state.last_seen_bitrix_message_id is not None:
            self._last_seen_bitrix_message_ids[dialog_id] = persisted_state.last_seen_bitrix_message_id
            logger.info("Loaded Bitrix cursor from state store for dialog %s: %s", dialog_id, persisted_state.last_seen_bitrix_message_id)
            return
        max_message_id = await self.bitrix.get_latest_message_id(dialog_id=dialog_id)
        await self._persist_cursor(dialog_id, max_message_id)
        logger.info("Initial Bitrix cursor for dialog %s set to %s", dialog_id, self._last_seen_bitrix_message_ids.get(dialog_id))

    async def _sync_bitrix_messages(self, application: Application, mapping: ChatMapping) -> None:
        dialog_id = mapping.bitrix_dialog_id
        tg_chat_id = mapping.tg_chat_id
        last_seen = self._last_seen_bitrix_message_ids.get(dialog_id)

        recent_snapshot = await self.bitrix.get_recent_messages(
            dialog_id=dialog_id,
            limit_total=self.settings.bitrix_rescan_recent_messages_limit,
        )
        logger.debug("Loaded full Bitrix snapshot for reconcile dialog=%s: message_count=%s", dialog_id, len(recent_snapshot.messages))
        await self._reconcile_recent_bitrix_messages(application, recent_snapshot)

        snapshot = await self.bitrix.get_messages_after(dialog_id=dialog_id, after_id=last_seen or 0)
        logger.debug(
            "Loaded incremental Bitrix snapshot dialog=%s after_id=%s: message_count=%s",
            dialog_id,
            last_seen,
            len(snapshot.messages),
        )
        fresh_messages: list[BitrixMessage] = []
        for message in snapshot.messages:
            if await self._should_forward_bitrix_message(dialog_id, message):
                fresh_messages.append(message)

        for bitrix_message in fresh_messages:
            sender_name = self._resolve_bitrix_sender_name(snapshot, bitrix_message)
            forwarded = await self._forward_bitrix_message(application, snapshot, bitrix_message, sender_name, tg_chat_id=tg_chat_id)
            logger.info(
                "Mirrored Bitrix message %s from dialog %s to Telegram chat %s (photo=%s)",
                bitrix_message.message_id,
                dialog_id,
                tg_chat_id,
                bool(forwarded.photo),
            )
            await self.state_store.upsert_link(
                telegram_chat_id=forwarded.chat_id,
                telegram_message_id=forwarded.message_id,
                bitrix_message_id=bitrix_message.message_id,
                origin=MirrorOrigin.BITRIX,
                telegram_message_date_unix=int(forwarded.date.timestamp()) if forwarded.date else None,
                bitrix_author_id=bitrix_message.author_id,
                last_seen_bitrix_revision=self._build_bitrix_revision(bitrix_message),
            )
            await self._persist_cursor(dialog_id, bitrix_message.message_id)

    async def _should_forward_bitrix_message(self, dialog_id: str, bitrix_message: BitrixMessage) -> bool:
        last_seen = self._last_seen_bitrix_message_ids.get(dialog_id)
        if last_seen is not None and bitrix_message.message_id <= last_seen:
            return False
        if bitrix_message.author_id == 0:
            logger.debug("Ignoring Bitrix service message %s from author_id=0", bitrix_message.message_id)
            return False
        if not bitrix_message.text.strip() and not bitrix_message.file_ids:
            return False
        link = await self.state_store.get_link_by_bitrix_message(bitrix_message_id=bitrix_message.message_id)
        if link is not None and link.origin == MirrorOrigin.TELEGRAM:
            logger.debug(
                "Suppressing Bitrix message %s because it is mapped to Telegram-origin message %s",
                bitrix_message.message_id,
                link.telegram_message_id,
            )
            return False
        return True

    def _has_uploadable_file(self, message: Message) -> bool:
        return bool(
            message.photo
            or message.document
            or message.video
            or message.audio
            or message.voice
            or message.video_note
            or message.sticker
        )

    async def _forward_telegram_file_to_bitrix(self, message: Message, *, dialog_id: str) -> int:
        if message.photo:
            file_source = message.photo[-1]
            original_name = None
            fallback_name = f"photo_{message.message_id}.jpg"
        elif message.document:
            file_source = message.document
            original_name = message.document.file_name
            fallback_name = f"document_{message.message_id}"
        elif message.video:
            file_source = message.video
            original_name = message.video.file_name
            fallback_name = f"video_{message.message_id}.mp4"
        elif message.audio:
            file_source = message.audio
            original_name = message.audio.file_name
            fallback_name = f"audio_{message.message_id}.ogg"
        elif message.voice:
            file_source = message.voice
            original_name = None
            fallback_name = f"voice_{message.message_id}.ogg"
        elif message.video_note:
            file_source = message.video_note
            original_name = None
            fallback_name = f"video_note_{message.message_id}.mp4"
        elif message.sticker:
            file_source = message.sticker
            original_name = None
            fallback_name = f"sticker_{message.message_id}.webp"
        else:
            raise ValueError("No uploadable file attachment found in message")
        telegram_file = await file_source.get_file()
        file_bytes = await telegram_file.download_as_bytearray()
        if len(file_bytes) > self.settings.max_file_size_bytes:
            logger.warning(
                "Telegram file too large (%s bytes > %s max), skipping upload for message %s",
                len(file_bytes), self.settings.max_file_size_bytes, message.message_id,
            )
            return await self.bitrix.send_message(
                self.render_telegram_message(message) + "\n\n[Файл слишком большой для пересылки]",
                dialog_id=dialog_id,
            )
        file_path_name = telegram_file.file_path.rsplit("/", 1)[-1] if telegram_file.file_path else None
        filename = original_name or file_path_name or fallback_name
        return await self.bitrix.send_photo(
            caption=self.render_telegram_message(message),
            filename=filename,
            content=bytes(file_bytes),
            dialog_id=dialog_id,
        )

    async def _forward_bitrix_message(
        self,
        application: Application,
        snapshot: BitrixDialogSnapshot,
        bitrix_message: BitrixMessage,
        sender_name: str,
        *,
        tg_chat_id: int,
    ) -> Message:
        rendered = self.render_bitrix_message(bitrix_message, sender_name=sender_name)
        attachment = self._select_bitrix_file(snapshot, bitrix_message)
        if attachment is None or not attachment.url_download:
            return await application.bot.send_message(
                chat_id=tg_chat_id,
                text=rendered,
                disable_web_page_preview=self.settings.disable_link_preview,
            )
        try:
            file_bytes = await self.bitrix.download_file_by_id(attachment.file_id, fallback_url=attachment.url_download)
            if len(file_bytes) > self.settings.max_file_size_bytes:
                logger.warning(
                    "Bitrix file too large (%s bytes > %s max), sending text only for message %s",
                    len(file_bytes), self.settings.max_file_size_bytes, bitrix_message.message_id,
                )
                return await application.bot.send_message(
                    chat_id=tg_chat_id,
                    text=rendered + "\n\n[Файл слишком большой для пересылки]",
                    disable_web_page_preview=self.settings.disable_link_preview,
                )
            mime = attachment.mime_type or ""
            if mime.startswith("image/"):
                return await application.bot.send_photo(
                    chat_id=tg_chat_id,
                    photo=BytesIO(file_bytes),
                    filename=attachment.name,
                    caption=rendered or None,
                )
            elif mime.startswith("video/"):
                return await application.bot.send_video(
                    chat_id=tg_chat_id,
                    video=BytesIO(file_bytes),
                    filename=attachment.name,
                    caption=rendered or None,
                )
            elif mime.startswith("audio/"):
                return await application.bot.send_audio(
                    chat_id=tg_chat_id,
                    audio=BytesIO(file_bytes),
                    filename=attachment.name,
                    caption=rendered or None,
                )
            else:
                return await application.bot.send_document(
                    chat_id=tg_chat_id,
                    document=BytesIO(file_bytes),
                    filename=attachment.name,
                    caption=rendered or None,
                )
        except Exception:
            logger.exception("Failed to forward Bitrix file for message %s, falling back to text", bitrix_message.message_id)
            return await application.bot.send_message(
                chat_id=tg_chat_id,
                text=rendered,
                disable_web_page_preview=self.settings.disable_link_preview,
            )

    def _select_bitrix_file(self, snapshot: BitrixDialogSnapshot, bitrix_message: BitrixMessage) -> Optional[BitrixFile]:
        for file_id in bitrix_message.file_ids:
            file = snapshot.files_by_id.get(file_id)
            if file:
                return file
        return None

    def _resolve_bitrix_sender_name(self, snapshot: BitrixDialogSnapshot, bitrix_message: BitrixMessage) -> str:
        if bitrix_message.author_id is None:
            return "Неизвестный отправитель"
        user = snapshot.users_by_id.get(bitrix_message.author_id)
        if user is not None:
            return user.display_name
        logger.warning(
            "Bitrix author_id=%s for message_id=%s is missing in users directory",
            bitrix_message.author_id,
            bitrix_message.message_id,
        )
        return f"Bitrix user_id: {bitrix_message.author_id}"

    async def _persist_cursor(self, dialog_id: str, message_id: Optional[int]) -> None:
        async with self._state_lock:
            self._last_seen_bitrix_message_ids[dialog_id] = message_id
            await self.state_store.save_cursor(dialog_id, CursorState(last_seen_bitrix_message_id=message_id))

    async def _reconcile_recent_bitrix_messages(self, application: Application, snapshot: BitrixDialogSnapshot) -> None:
        if not snapshot.messages:
            return
        recent_messages = sorted(snapshot.messages, key=lambda item: item.message_id)[-self.settings.bitrix_rescan_recent_messages_limit :]
        for bitrix_message in recent_messages:
            link = await self.state_store.get_link_by_bitrix_message(bitrix_message_id=bitrix_message.message_id)
            if link is None:
                continue
            logger.debug(
                "Reconciling Bitrix message %s with Telegram message %s origin=%s update_time_unix=%s",
                bitrix_message.message_id,
                link.telegram_message_id,
                link.origin.value,
                bitrix_message.update_time_unix,
            )

            # --- Edit reconciliation (BITRIX-origin only) ---
            if link.origin == MirrorOrigin.BITRIX:
                current_revision = self._build_bitrix_revision(bitrix_message)
                logger.debug(
                    "Bitrix reconcile revision compare message_id=%s origin=%s last_seen_revision=%s current_revision=%s update_time_unix=%s",
                    bitrix_message.message_id,
                    link.origin.value,
                    link.last_seen_bitrix_revision,
                    current_revision,
                    bitrix_message.update_time_unix,
                )
                if link.last_seen_bitrix_revision != current_revision:
                    try:
                        await self._apply_bitrix_edit_to_telegram(application, snapshot, link, bitrix_message)
                    except Exception:
                        logger.exception(
                            "Failed to mirror Bitrix edit message_id=%s to Telegram message_id=%s",
                            bitrix_message.message_id,
                            link.telegram_message_id,
                        )
                    else:
                        await self.state_store.upsert_link(
                            telegram_chat_id=link.telegram_chat_id,
                            telegram_message_id=link.telegram_message_id,
                            bitrix_message_id=bitrix_message.message_id,
                            origin=link.origin,
                            telegram_message_date_unix=link.telegram_message_date_unix,
                            bitrix_author_id=bitrix_message.author_id,
                            last_seen_bitrix_revision=current_revision,
                        )

            # --- Like/reaction reconciliation (all origins) ---
            current_likes = ",".join(str(uid) for uid in bitrix_message.like_user_ids)
            if current_likes != link.last_seen_bitrix_likes:
                if link.bitrix_liked_by_bot:
                    # This change likely includes our own like from TG→Bitrix sync; just update and reset flag
                    await self.state_store.update_reaction_state(
                        bitrix_message_id=bitrix_message.message_id,
                        bitrix_liked_by_bot=False,
                        last_seen_bitrix_likes=current_likes,
                    )
                else:
                    has_likes = bool(bitrix_message.like_user_ids)
                    try:
                        await self._sync_bitrix_reaction_to_telegram(application, link, has_likes)
                    except Exception:
                        logger.exception(
                            "Failed to mirror Bitrix like change for message_id=%s to Telegram message_id=%s",
                            bitrix_message.message_id,
                            link.telegram_message_id,
                        )
                    await self.state_store.update_reaction_state(
                        bitrix_message_id=bitrix_message.message_id,
                        bitrix_liked_by_bot=False,
                        last_seen_bitrix_likes=current_likes,
                    )

    async def _sync_bitrix_reaction_to_telegram(
        self,
        application: Application,
        link: MessageMirrorLink,
        has_likes: bool,
    ) -> None:
        try:
            if has_likes:
                await application.bot.set_message_reaction(
                    chat_id=link.telegram_chat_id,
                    message_id=link.telegram_message_id,
                    reaction=[ReactionTypeEmoji(emoji="👍")],
                )
                logger.info(
                    "Mirrored Bitrix like to Telegram reaction on message %s in chat %s",
                    link.telegram_message_id,
                    link.telegram_chat_id,
                )
            else:
                await application.bot.set_message_reaction(
                    chat_id=link.telegram_chat_id,
                    message_id=link.telegram_message_id,
                    reaction=[],
                )
                logger.info(
                    "Removed Telegram reaction on message %s in chat %s (Bitrix likes removed)",
                    link.telegram_message_id,
                    link.telegram_chat_id,
                )
        except BadRequest as exc:
            logger.warning(
                "Telegram rejected reaction update for message_id=%s chat_id=%s: %s",
                link.telegram_message_id,
                link.telegram_chat_id,
                str(exc),
            )

    async def _apply_bitrix_edit_to_telegram(
        self,
        application: Application,
        snapshot: BitrixDialogSnapshot,
        link: MessageMirrorLink,
        bitrix_message: BitrixMessage,
    ) -> None:
        sender_name = self._resolve_bitrix_sender_name(snapshot, bitrix_message)
        rendered = self.render_bitrix_message(bitrix_message, sender_name=sender_name)
        photo = self._select_bitrix_photo(snapshot, bitrix_message)
        try:
            if photo is None:
                await application.bot.edit_message_text(
                    chat_id=link.telegram_chat_id,
                    message_id=link.telegram_message_id,
                    text=rendered,
                    disable_web_page_preview=self.settings.disable_link_preview,
                )
                logger.info("Mirrored Bitrix edit %s to Telegram message %s", bitrix_message.message_id, link.telegram_message_id)
                return
            await application.bot.edit_message_caption(
                chat_id=link.telegram_chat_id,
                message_id=link.telegram_message_id,
                caption=rendered,
            )
            logger.info("Mirrored Bitrix caption edit %s to Telegram message %s", bitrix_message.message_id, link.telegram_message_id)
        except ChatMigrated as exc:
            await self._cleanup_migrated_chat_links(old_chat_id=link.telegram_chat_id, new_chat_id=exc.new_chat_id)
            raise
        except BadRequest as exc:
            logger.warning(
                "Telegram rejected Bitrix edit for bitrix_message_id=%s telegram_message_id=%s chat_id=%s: %s",
                bitrix_message.message_id,
                link.telegram_message_id,
                link.telegram_chat_id,
                str(exc),
            )
            if "message to edit not found" in str(exc).lower():
                logger.warning(
                    "Removing stale Bitrix-origin link for bitrix_message_id=%s because Telegram message %s in chat %s no longer exists",
                    bitrix_message.message_id,
                    link.telegram_message_id,
                    link.telegram_chat_id,
                )
                await self.state_store.delete_link_by_bitrix_message(bitrix_message_id=bitrix_message.message_id)
            elif "message is not modified" in str(exc).lower():
                logger.debug(
                    "Ignoring no-op Bitrix edit for bitrix_message_id=%s because Telegram content is already актуален",
                    bitrix_message.message_id,
                )
                return
            else:
                logger.warning(
                    "Removing Bitrix-origin link for bitrix_message_id=%s after generic Telegram BadRequest to prevent repeated blocking edit failures",
                    bitrix_message.message_id,
                )
                await self.state_store.delete_link_by_bitrix_message(bitrix_message_id=bitrix_message.message_id)
            raise

    async def _cleanup_stale_chat_links(self) -> None:
        allowed_tg_chat_ids = {m.tg_chat_id for m in self.settings.chat_mappings}
        rows_to_remove: list[int] = []
        seen_chat_ids: set[int] = set()
        for mapping in self.settings.chat_mappings:
            try:
                recent_snapshot = await self.bitrix.get_recent_messages(
                    dialog_id=mapping.bitrix_dialog_id,
                    limit_total=self.settings.bitrix_rescan_recent_messages_limit,
                )
            except Exception:
                logger.warning("Failed to load messages for dialog %s during stale link cleanup", mapping.bitrix_dialog_id)
                continue
            recent_messages = sorted(recent_snapshot.messages, key=lambda item: item.message_id)[-self.settings.bitrix_rescan_recent_messages_limit :]
            for bitrix_message in recent_messages:
                link = await self.state_store.get_link_by_bitrix_message(bitrix_message_id=bitrix_message.message_id)
                if link is None:
                    continue
                if link.telegram_chat_id not in allowed_tg_chat_ids and link.telegram_chat_id not in seen_chat_ids:
                    seen_chat_ids.add(link.telegram_chat_id)
                    rows_to_remove.append(link.telegram_chat_id)
        for chat_id in rows_to_remove:
            await self.state_store.delete_links_by_telegram_chat(telegram_chat_id=chat_id)

    async def _cleanup_migrated_chat_links(self, *, old_chat_id: int, new_chat_id: int) -> None:
        logger.warning(
            "Telegram chat migrated from %s to %s. Removing stale links for old chat id.",
            old_chat_id,
            new_chat_id,
        )
        await self.state_store.delete_links_by_telegram_chat(telegram_chat_id=old_chat_id)

    def _sender_name(self, message: Message) -> str:
        if message.from_user:
            full_name = message.from_user.full_name.strip()
            username = message.from_user.username
            if username:
                return f"{full_name} (@{username})"
            return full_name
        return "Неизвестный отправитель"

    def _build_body(self, message: Message) -> str:
        text = self._extract_primary_text(message)
        extra = self._describe_attachments(message)
        parts: list[str] = []
        if text:
            parts.append(text)
        if extra:
            if parts:
                parts.append("")
            parts.append(extra)
        if not parts:
            if self._has_uploadable_file(message):
                return ""
            return "[Сообщение без поддерживаемого текста или вложения]"
        return "\n".join(parts)

    def _extract_primary_text(self, message: Message) -> str:
        if message.text:
            return message.text
        if message.caption:
            return message.caption
        return ""

    def _describe_attachments(self, message: Message) -> str:
        chunks: list[str] = []
        if message.sticker:
            sticker = message.sticker
            label = sticker.emoji or "[Стикер]"
            chunks.append(label)
        if message.contact:
            contact = message.contact
            contact_name = " ".join(part for part in [contact.first_name, contact.last_name or ""] if part).strip()
            chunks.append(f"[Контакт] {contact_name} | {contact.phone_number}")
        if message.location:
            location = message.location
            chunks.append(
                f"[Локация] https://maps.google.com/maps?q={location.latitude},{location.longitude}"
            )
        if message.poll:
            poll = message.poll
            options = ", ".join(opt.text for opt in poll.options)
            chunks.append(f"[Опрос] {poll.question} | {options}")
        return "\n".join(chunks)

    def _shorten(self, value: str, limit: int) -> str:
        clean = " ".join(value.split())
        if len(clean) <= limit:
            return clean
        return clean[: limit - 1] + "…"

    def _build_bitrix_revision(self, bitrix_message: BitrixMessage) -> str:
        digest = hashlib.sha256()
        digest.update(bitrix_message.text.encode("utf-8", errors="ignore"))
        digest.update(b"|")
        digest.update(";".join(str(file_id) for file_id in bitrix_message.file_ids).encode("ascii", errors="ignore"))
        return digest.hexdigest()

    # ── Periodic cleanup ──────────────────────────────────────────────────

    async def _periodic_cleanup_loop(self) -> None:
        """Run DB cleanup and file cache cleanup every hour."""
        cleanup_interval = 3600  # 1 hour
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=cleanup_interval)
                break  # stop_event was set
            except asyncio.TimeoutError:
                pass
            try:
                deleted = await self.state_store.cleanup_old_links(
                    max_age_seconds=self.settings.db_cleanup_max_age_seconds,
                )
                if deleted:
                    logger.info("Periodic DB cleanup: removed %s old link(s)", deleted)
                self._cleanup_file_cache()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Periodic cleanup failed")

    def _cleanup_file_cache(self) -> None:
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

        # Sort by modification time, oldest first
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
            logger.info("File cache cleanup: removed %s file(s), cache now ~%s MB", removed, total_size // (1024 * 1024))
