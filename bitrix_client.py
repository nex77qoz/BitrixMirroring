from __future__ import annotations

import asyncio
import base64
import logging
from urllib.parse import urlparse
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

    async def send_message(self, text: str, *, dialog_id: str) -> int:
        if self.settings.bitrix_use_chat_bot:
            return await self._send_bot_message(text=text, dialog_id=dialog_id)

        data = await self._call(
            "im.message.add",
            {
                "DIALOG_ID": dialog_id,
                "MESSAGE": text,
                "SYSTEM": "N",
                "URL_PREVIEW": "N" if self.settings.disable_link_preview else "Y",
            },
        )
        result = data.get("result")
        if not isinstance(result, int):
            raise RuntimeError(f"Unexpected Bitrix response: {data}")
        return result

    async def _send_bot_message(self, *, text: str, dialog_id: str) -> int:
        payload: dict[str, Any] = {
            "DIALOG_ID": dialog_id,
            "MESSAGE": text,
            "SYSTEM": "N",
            "URL_PREVIEW": "N" if self.settings.disable_link_preview else "Y",
            "CLIENT_ID": self.settings.bitrix_bot_client_id,
        }
        if self.settings.bitrix_bot_id is not None:
            payload["BOT_ID"] = self.settings.bitrix_bot_id

        data = await self._call("imbot.message.add", payload)
        result = data.get("result")
        if not isinstance(result, int):
            raise RuntimeError(f"Unexpected Bitrix response: {data}")
        return result

    async def update_message(self, *, message_id: int, text: str) -> None:
        if self.settings.bitrix_use_chat_bot:
            await self._update_bot_message(message_id=message_id, text=text)
            return

        data = await self._call(
            "im.message.update",
            {
                "MESSAGE_ID": message_id,
                "MESSAGE": text,
                "URL_PREVIEW": "N" if self.settings.disable_link_preview else "Y",
            },
        )
        result = data.get("result")
        if result not in (True, "Y", 1):
            raise RuntimeError(f"Unexpected Bitrix response: {data}")

    async def _update_bot_message(self, *, message_id: int, text: str) -> None:
        payload: dict[str, Any] = {
            "MESSAGE_ID": message_id,
            "MESSAGE": text,
            "URL_PREVIEW": "N" if self.settings.disable_link_preview else "Y",
        }
        if self.settings.bitrix_bot_client_id is not None:
            payload["CLIENT_ID"] = self.settings.bitrix_bot_client_id
        if self.settings.bitrix_bot_id is not None:
            payload["BOT_ID"] = self.settings.bitrix_bot_id
        data = await self._call("imbot.message.update", payload)
        result = data.get("result")
        if result not in (True, "Y", 1):
            raise RuntimeError(f"Unexpected Bitrix response: {data}")

    async def set_message_like(self, message_id: int, *, liked: bool) -> None:
        action = "plus" if liked else "minus"
        try:
            data = await self._call("im.message.like", {"MESSAGE_ID": message_id, "ACTION": action})
        except RuntimeError as exc:
            if "WITHOUT_CHANGES" in str(exc):
                return
            raise
        result = data.get("result")
        if result not in (True, "Y", 1):
            raise RuntimeError(f"Unexpected Bitrix response: {data}")

    async def send_photo(self, *, caption: str, filename: str, content: bytes, dialog_id: str) -> int:
        encoded = base64.b64encode(content).decode("ascii")
        previous_message_id = await self.get_latest_message_id(dialog_id=dialog_id)
        folder_id = await self._get_chat_upload_folder_id(dialog_id)
        uploaded_file_id = await self._upload_file_to_folder(folder_id=folder_id, filename=filename, encoded_content=encoded)
        commit_result = await self._commit_chat_file(dialog_id=dialog_id, file_id=uploaded_file_id, message=caption)
        message_id = self._extract_message_id(commit_result)
        if message_id is not None:
            return message_id
        return await self._find_committed_message_id(
            dialog_id=dialog_id,
            after_id=previous_message_id,
            expected_file_id=uploaded_file_id,
        )

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
        candidate_urls = await self._get_file_download_candidates(file_id)
        if fallback_url:
            candidate_urls.append(fallback_url)

        errors: list[str] = []
        seen: set[str] = set()
        for candidate_url in candidate_urls:
            normalized = candidate_url.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            try:
                return await self.download_file(normalized)
            except Exception as exc:
                logger.warning("Failed to download Bitrix file_id=%s using %s: %s", file_id, normalized, exc)
                errors.append(f"{normalized}: {exc}")

        raise RuntimeError(
            f"Unable to download Bitrix file_id={file_id}. Tried: {' | '.join(errors) if errors else 'no URLs resolved'}"
        )

    async def _get_file_download_candidates(self, file_id: int) -> list[str]:
        data = await self._call("disk.file.get", {"id": file_id})
        result = data.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"Unexpected Bitrix response for disk.file.get: {data}")

        candidates: list[str] = []
        for key in (
            "DOWNLOAD_URL",
            "downloadUrl",
            "URL_DOWNLOAD",
            "urlDownload",
            "DETAIL_URL",
            "detailUrl",
            "SHOW_URL",
            "showUrl",
        ):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(self._normalize_bitrix_url(value.strip()))
        return candidates

    def _normalize_bitrix_url(self, value: str) -> str:
        if value.startswith(("http://", "https://")):
            return value

        parsed = urlparse(self.settings.bitrix_webhook_base)
        base = f"{parsed.scheme}://{parsed.netloc}"
        if value.startswith("/"):
            return f"{base}{value}"
        return f"{base}/{value}"

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

    async def _get_chat_upload_folder_id(self, dialog_id: str) -> int:
        data = await self._call("im.disk.folder.get", {"DIALOG_ID": dialog_id})
        result = data.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"Unexpected Bitrix response for im.disk.folder.get: {data}")
        folder_id = result.get("ID")
        if not isinstance(folder_id, int):
            raise RuntimeError(f"Unexpected Bitrix folder payload: {data}")
        return folder_id

    async def _upload_file_to_folder(self, *, folder_id: int, filename: str, encoded_content: str) -> int:
        data = await self._call(
            "disk.folder.uploadfile",
            {
                "id": folder_id,
                "data": {"NAME": filename},
                "fileContent": [filename, encoded_content],
                "generateUniqueName": True,
            },
        )
        result = data.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"Unexpected Bitrix response for disk.folder.uploadfile: {data}")
        file_id = result.get("ID")
        if not isinstance(file_id, int):
            raise RuntimeError(f"Unexpected Bitrix upload payload: {data}")
        return file_id

    async def _commit_chat_file(self, *, dialog_id: str, file_id: int, message: str) -> dict[str, Any]:
        data = await self._call(
            "im.disk.file.commit",
            {
                "DIALOG_ID": dialog_id,
                "FILE_ID": [file_id],
                "MESSAGE": message,
            },
        )
        result = data.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"Unexpected Bitrix response for im.disk.file.commit: {data}")
        return result

    async def _find_committed_message_id(
        self,
        *,
        dialog_id: str,
        after_id: Optional[int],
        expected_file_id: int,
    ) -> int:
        first_id = after_id if after_id is not None else 0
        for attempt in range(4):
            snapshot = await self.get_messages_after(dialog_id=dialog_id, after_id=first_id)
            for message in reversed(snapshot.messages):
                if expected_file_id in message.file_ids:
                    return message.message_id
            if attempt < 3:
                await asyncio.sleep(0.4)
        raise RuntimeError(
            f"Bitrix committed a file to dialog {dialog_id}, but the resulting message id could not be resolved"
        )

    def _extract_message_id(self, payload: Any) -> Optional[int]:
        if isinstance(payload, dict):
            for key in ("MESSAGE_ID", "message_id", "messageId"):
                value = payload.get(key)
                if isinstance(value, int):
                    return value
                if isinstance(value, str) and value.strip().isdigit():
                    return int(value.strip())
            for value in payload.values():
                nested = self._extract_message_id(value)
                if nested is not None:
                    return nested
        if isinstance(payload, list):
            for item in payload:
                nested = self._extract_message_id(item)
                if nested is not None:
                    return nested
        return None

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

