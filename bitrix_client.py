from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any, Optional, cast

import httpx

from models import BitrixDialogSnapshot, BitrixFile, BitrixMessage, BitrixUser
from settings import Settings

logger = logging.getLogger("tg-bitrix-mirror")
BITRIX_MESSAGES_PAGE_LIMIT = 50


class BitrixClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(
            timeout=settings.request_timeout_seconds,
            proxy=settings.socks5_proxy_url,
        )
        self._request_semaphore = asyncio.Semaphore(settings.bitrix_max_concurrent_requests)
    async def close(self) -> None:
        await self._client.aclose()

    async def send_message(self, text: str, *, dialog_id: str, reply_id: Optional[int] = None) -> int:
        fields: dict[str, Any] = {
            "message": text,
            "system": False,
            "urlPreview": not self.settings.disable_link_preview,
        }
        if reply_id is not None:
            fields["replyId"] = reply_id
        payload: dict[str, Any] = {
            "botId": self.settings.bitrix_bot_id,
            "botToken": self.settings.bitrix_bot_client_id,
            "dialogId": dialog_id,
            "fields": fields,
        }
        data = await self._call("imbot.v2.Chat.Message.send", payload)
        result = data.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"Unexpected Bitrix response: {data}")
        message_id = result.get("id")
        if not isinstance(message_id, int):
            raise RuntimeError(f"Missing id in imbot.v2.Chat.Message.send response: {data}")
        return message_id

    async def update_message(self, *, message_id: int, text: str) -> None:
        payload: dict[str, Any] = {
            "botId": self.settings.bitrix_bot_id,
            "botToken": self.settings.bitrix_bot_client_id,
            "messageId": message_id,
            "fields": {
                "message": text,
                "urlPreview": not self.settings.disable_link_preview,
            },
        }
        data = await self._call("imbot.v2.Chat.Message.update", payload)
        result = data.get("result")
        if result is not True:
            raise RuntimeError(f"Unexpected Bitrix response: {data}")

    async def set_message_like(self, message_id: int, *, liked: bool) -> None:
        method = "imbot.v2.Chat.Message.Reaction.add" if liked else "imbot.v2.Chat.Message.Reaction.delete"
        payload: dict[str, Any] = {
            "botId": self.settings.bitrix_bot_id,
            "botToken": self.settings.bitrix_bot_client_id,
            "messageId": message_id,
            "reaction": "like",
        }
        try:
            data = await self._call(method, payload)
        except RuntimeError as exc:
            err = str(exc)
            if "REACTION_ALREADY_SET" in err or "REACTION_NOT_FOUND" in err:
                return
            raise
        result = data.get("result")
        if result is not True:
            raise RuntimeError(f"Unexpected Bitrix response: {data}")

    async def send_photo(self, *, caption: str, filename: str, content: bytes, dialog_id: str) -> int:
        encoded = base64.b64encode(content).decode("ascii")
        return await self._upload_file(
            dialog_id=dialog_id,
            filename=filename,
            encoded=encoded,
            caption=caption,
        )

    async def _upload_file(self, *, dialog_id: str, filename: str, encoded: str, caption: str) -> int:
        file_fields: dict[str, Any] = {
            "name": filename,
            "content": encoded,
            "message": caption,
        }
        payload: dict[str, Any] = {
            "botId": self.settings.bitrix_bot_id,
            "botToken": self.settings.bitrix_bot_client_id,
            "dialogId": dialog_id,
            "fields": file_fields,
        }
        data = await self._call("imbot.v2.File.upload", payload)
        result = data.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"Unexpected Bitrix response for imbot.v2.File.upload: {data}")
        message_id = result.get("messageId")
        if not isinstance(message_id, int):
            raise RuntimeError(f"Missing messageId in imbot.v2.File.upload response: {data}")
        return message_id

    async def get_latest_message_id(self, *, dialog_id: str) -> Optional[int]:
        snapshot = await self.get_messages_page(dialog_id=dialog_id, limit=1)
        return max((message.message_id for message in snapshot.messages), default=None)

    async def get_recent_messages(self, *, dialog_id: str, limit_total: int) -> BitrixDialogSnapshot:
        effective_limit = max(1, limit_total)
        aggregate = BitrixDialogSnapshot(messages=[], users_by_id={}, files_by_id={})
        next_last_id: Optional[int] = None

        while len(aggregate.messages) < effective_limit:
            page_limit = min(BITRIX_MESSAGES_PAGE_LIMIT, effective_limit - len(aggregate.messages))
            page = await self.get_messages_page(dialog_id=dialog_id, last_id=next_last_id, limit=page_limit)
            if not page.messages:
                break
            aggregate = self._merge_snapshots(aggregate, page)
            if len(page.messages) < page_limit:
                break
            next_last_id = min(message.message_id for message in page.messages)

        if len(aggregate.messages) > effective_limit:
            trimmed_messages = sorted(aggregate.messages, key=lambda item: item.message_id)[-effective_limit:]
            return BitrixDialogSnapshot(
                messages=trimmed_messages,
                users_by_id=aggregate.users_by_id,
                files_by_id=aggregate.files_by_id,
            )
        return aggregate

    async def get_messages_after(self, *, dialog_id: str, after_id: int) -> BitrixDialogSnapshot:
        next_first_id = after_id
        aggregate = BitrixDialogSnapshot(messages=[], users_by_id={}, files_by_id={})

        while True:
            page = await self.get_messages_page(
                dialog_id=dialog_id,
                first_id=next_first_id,
                limit=BITRIX_MESSAGES_PAGE_LIMIT,
            )
            if not page.messages:
                break
            aggregate = self._merge_snapshots(aggregate, page)
            if len(page.messages) < BITRIX_MESSAGES_PAGE_LIMIT:
                break
            next_first_id = max(message.message_id for message in page.messages)

        return aggregate

    async def get_messages_page(
        self,
        *,
        dialog_id: str,
        first_id: Optional[int] = None,
        last_id: Optional[int] = None,
        limit: int = BITRIX_MESSAGES_PAGE_LIMIT,
    ) -> BitrixDialogSnapshot:
        payload: dict[str, Any] = {
            "DIALOG_ID": dialog_id,
            "LIMIT": max(1, min(limit, BITRIX_MESSAGES_PAGE_LIMIT)),
        }
        if first_id is not None:
            payload["FIRST_ID"] = first_id
        if last_id is not None:
            payload["LAST_ID"] = last_id
        data = await self._call("im.dialog.messages.get", payload)
        result = data.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"Unexpected Bitrix response: {data}")

        messages = result.get("messages", [])
        if not isinstance(messages, list):
            raise RuntimeError(f"Unexpected Bitrix messages payload: {data}")

        users = result.get("users", [])
        if not isinstance(users, list):
            raise RuntimeError(f"Unexpected Bitrix users payload: {data}")

        raw_files = result.get("files", [])
        if not isinstance(raw_files, (list, dict)):
            raise RuntimeError(f"Unexpected Bitrix files payload: {data}")

        for payload_item in messages:
            logger.debug("RAW Bitrix message payload: %s", payload_item)

        parsed_messages = [
            message
            for payload_item in messages
            if isinstance(payload_item, dict)
            for message in [BitrixMessage.from_api_payload(payload_item)]
            if message is not None
        ]
        parsed_messages.sort(key=lambda item: item.message_id)
        users_by_id = {
            user.user_id: user
            for payload_item in users
            if isinstance(payload_item, dict)
            for user in [BitrixUser.from_api_payload(payload_item)]
            if user is not None
        }
        file_items = raw_files.values() if isinstance(raw_files, dict) else raw_files
        files_by_id = {
            file.file_id: file
            for payload_item in file_items
            if isinstance(payload_item, dict)
            for file in [BitrixFile.from_api_payload(payload_item)]
            if file is not None
        }

        return BitrixDialogSnapshot(messages=parsed_messages, users_by_id=users_by_id, files_by_id=files_by_id)

    async def download_file(self, url: str) -> bytes:
        for attempt in range(1, self.settings.bitrix_retry_attempts + 1):
            try:
                async with self._request_semaphore:
                    response = await self._client.get(url)
                response.raise_for_status()
                return response.content
            except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError, httpx.HTTPStatusError) as exc:
                is_last_attempt = attempt >= self.settings.bitrix_retry_attempts
                if is_last_attempt:
                    raise
                logger.warning(
                    "Bitrix file download failed on attempt %s/%s: %s",
                    attempt,
                    self.settings.bitrix_retry_attempts,
                    exc,
                )
                await asyncio.sleep(self.settings.bitrix_retry_base_delay_seconds)
        raise RuntimeError(f"Bitrix file download exhausted retries for {url}")

    async def download_file_by_id(self, file_id: int, fallback_url: Optional[str] = None) -> bytes:
        primary_url: Optional[str] = None
        try:
            primary_url = await self._get_file_download_url(file_id)
        except Exception as exc:
            logger.warning("Failed to resolve download URL for Bitrix file_id=%s: %s", file_id, exc)

        candidate_urls = [u for u in (primary_url, fallback_url) if u]
        if not candidate_urls:
            raise RuntimeError(f"Unable to download Bitrix file_id={file_id}: no URLs resolved")

        errors: list[str] = []
        for url in candidate_urls:
            try:
                return await self.download_file(url)
            except Exception as exc:
                logger.warning("Failed to download Bitrix file_id=%s using %s: %s", file_id, url, exc)
                errors.append(f"{url}: {exc}")

        raise RuntimeError(
            f"Unable to download Bitrix file_id={file_id}. Tried: {' | '.join(errors)}"
        )

    async def get_message_reply_id(self, *, dialog_id: str, message_id: int) -> Optional[int]:
        """Fetch REPLY_ID for a specific message via im.dialog.messages.search.

        im.dialog.messages.get does not return REPLY_ID in params,
        but im.dialog.messages.search does.
        """
        chat_id_str = dialog_id.replace("chat", "")
        if not chat_id_str.isdigit():
            logger.debug("Cannot extract chat_id from dialog_id=%s for reply lookup", dialog_id)
            return None
        chat_id = int(chat_id_str)

        payload: dict[str, Any] = {
            "CHAT_ID": chat_id,
            "LAST_ID": message_id + 1,
            "LIMIT": 1,
            "ORDER": {"ID": "DESC"},
        }
        try:
            data = await self._call("im.dialog.messages.search", payload)
        except Exception:
            logger.warning("Failed to fetch reply_id via search for message %s", message_id, exc_info=True)
            return None

        result = data.get("result")
        if not isinstance(result, dict):
            return None

        messages = result.get("messages", [])
        if not isinstance(messages, list):
            return None

        for msg in messages:
            if not isinstance(msg, dict):
                continue
            if msg.get("id") != message_id:
                continue
            params = msg.get("params")
            if not isinstance(params, dict):
                continue
            for key in ("REPLY_ID", "replyId", "reply_id"):
                raw = params.get(key)
                if isinstance(raw, int) and raw > 0:
                    return raw
                if isinstance(raw, str) and raw.strip().isdigit() and int(raw.strip()) > 0:
                    return int(raw.strip())
        return None

    async def _get_file_download_url(self, file_id: int) -> str:
        payload: dict[str, Any] = {
            "botId": self.settings.bitrix_bot_id,
            "botToken": self.settings.bitrix_bot_client_id,
            "fileId": file_id,
        }
        data = await self._call("imbot.v2.File.download", payload)
        result = data.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"Unexpected Bitrix response for imbot.v2.File.download: {data}")
        download_url = result.get("downloadUrl")
        if not isinstance(download_url, str) or not download_url.strip():
            raise RuntimeError(f"Missing downloadUrl in imbot.v2.File.download response: {data}")
        return download_url.strip()

    async def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.settings.bitrix_webhook_base}/{method}"
        delay = self.settings.bitrix_retry_base_delay_seconds

        for attempt in range(1, self.settings.bitrix_retry_attempts + 1):
            try:
                async with self._request_semaphore:
                    response = await self._client.post(url, json=payload)

                if response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"Temporary Bitrix HTTP error: {response.status_code}",
                        request=response.request,
                        response=response,
                    )

                response.raise_for_status()
                data = response.json()
                if "error" in data:
                    error_code = str(data.get("error") or "")
                    if error_code.upper() in {"QUERY_LIMIT_EXCEEDED", "TEMPORARY_ERROR"}:
                        raise RuntimeError(f"Temporary Bitrix error: {error_code}")
                    raise RuntimeError(
                        f"Bitrix error: {data['error']} | {data.get('error_description', '')}"
                    )
                return data
            except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError, httpx.HTTPStatusError, RuntimeError) as exc:
                is_last_attempt = attempt >= self.settings.bitrix_retry_attempts
                is_retryable = self._is_retryable_exception(exc)
                if is_last_attempt or not is_retryable:
                    raise
                logger.warning(
                    "Bitrix call %s failed on attempt %s/%s: %s. Retrying in %.1fs",
                    method,
                    attempt,
                    self.settings.bitrix_retry_attempts,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.settings.bitrix_retry_max_delay_seconds)

        raise RuntimeError(f"Bitrix call {method} exhausted retries without returning a response")

    def _merge_snapshots(
        self,
        current: BitrixDialogSnapshot,
        new_page: BitrixDialogSnapshot,
    ) -> BitrixDialogSnapshot:
        messages_by_id = {message.message_id: message for message in current.messages}
        messages_by_id.update({message.message_id: message for message in new_page.messages})
        merged_messages = sorted(messages_by_id.values(), key=lambda item: item.message_id)
        merged_users = dict(current.users_by_id)
        merged_users.update(new_page.users_by_id)
        merged_files = dict(current.files_by_id)
        merged_files.update(new_page.files_by_id)
        return BitrixDialogSnapshot(
            messages=merged_messages,
            users_by_id=merged_users,
            files_by_id=merged_files,
        )

    def _is_retryable_exception(self, exc: Exception) -> bool:
        if isinstance(exc, RuntimeError):
            return str(exc).startswith("Temporary Bitrix error:")
        if isinstance(exc, httpx.HTTPStatusError):
            http_status_error = cast(httpx.HTTPStatusError, exc)
            response = http_status_error.response
            status_code = response.status_code
            return status_code >= 500 or status_code in {408, 429}
        return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError))

