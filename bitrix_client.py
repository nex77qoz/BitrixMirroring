from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any, cast

import httpx

from models import BitrixBotEvent, BitrixEventPage
from settings import Settings

logger = logging.getLogger('tg-bitrix-mirror')

class BitrixClient:

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(timeout=settings.request_timeout_seconds, proxy=settings.socks5_proxy_url)
        self._request_semaphore = asyncio.Semaphore(settings.bitrix_max_concurrent_requests)
        self._rate_last_request: float = 0.0
        self._rate_min_interval: float = 1.0 / max(settings.bitrix_max_concurrent_requests, 1)

    async def close(self) -> None:
        await self._client.aclose()

    async def send_message(self, text: str, *, dialog_id: str, reply_id: int | None=None) -> int:
        fields: dict[str, Any] = {'message': text, 'system': False, 'urlPreview': not self.settings.disable_link_preview}
        if reply_id is not None:
            fields['replyId'] = reply_id
        payload: dict[str, Any] = {'botId': self.settings.bitrix_bot_id, 'botToken': self.settings.bitrix_bot_client_id, 'dialogId': dialog_id, 'fields': fields}
        data = await self._call('imbot.v2.Chat.Message.send', payload)
        result = data.get('result')
        if not isinstance(result, dict):
            raise RuntimeError(f'Unexpected Bitrix response: {data}')
        message_id = result.get('id')
        if not isinstance(message_id, int):
            raise RuntimeError(f'Missing id in imbot.v2.Chat.Message.send response: {data}')
        return message_id

    async def update_message(self, *, message_id: int, text: str) -> None:
        payload: dict[str, Any] = {'botId': self.settings.bitrix_bot_id, 'botToken': self.settings.bitrix_bot_client_id, 'messageId': message_id, 'fields': {'message': text, 'urlPreview': 'Y' if not self.settings.disable_link_preview else 'N'}}
        data = await self._call('imbot.v2.Chat.Message.update', payload)
        result = data.get('result')
        if not isinstance(result, dict) or result.get('result') is not True:
            raise RuntimeError(f'Unexpected Bitrix response: {data}')

    async def set_message_like(self, message_id: int, *, liked: bool) -> None:
        method = 'imbot.v2.Chat.Message.Reaction.add' if liked else 'imbot.v2.Chat.Message.Reaction.delete'
        payload: dict[str, Any] = {'botId': self.settings.bitrix_bot_id, 'botToken': self.settings.bitrix_bot_client_id, 'messageId': message_id, 'reaction': 'like'}
        try:
            data = await self._call(method, payload)
        except RuntimeError as exc:
            err = str(exc)
            if 'REACTION_ALREADY_SET' in err or 'REACTION_NOT_FOUND' in err:
                return
            raise
        result = data.get('result')
        if not isinstance(result, dict) or result.get('result') is not True:
            raise RuntimeError(f'Unexpected Bitrix response: {data}')

    async def send_photo(self, *, caption: str, filename: str, content: bytes, dialog_id: str) -> int:
        encoded = base64.b64encode(content).decode('ascii')
        return await self._upload_file(dialog_id=dialog_id, filename=filename, encoded=encoded, caption=caption)

    async def _upload_file(self, *, dialog_id: str, filename: str, encoded: str, caption: str) -> int:
        file_fields: dict[str, Any] = {'name': filename, 'content': encoded, 'message': caption}
        payload: dict[str, Any] = {'botId': self.settings.bitrix_bot_id, 'botToken': self.settings.bitrix_bot_client_id, 'dialogId': dialog_id, 'fields': file_fields}
        data = await self._call('imbot.v2.File.upload', payload)
        result = data.get('result')
        if not isinstance(result, dict):
            raise RuntimeError(f'Unexpected Bitrix response for imbot.v2.File.upload: {data}')
        message_id = result.get('messageId')
        if not isinstance(message_id, int):
            raise RuntimeError(f'Missing messageId in imbot.v2.File.upload response: {data}')
        return message_id

    async def get_bot_events(self, *, offset: int | None, limit: int=100) -> BitrixEventPage:
        payload: dict[str, Any] = {'botId': self.settings.bitrix_bot_id, 'botToken': self.settings.bitrix_bot_client_id, 'limit': max(1, min(limit, 1000))}
        if offset is not None:
            payload['offset'] = offset
        data = await self._call('imbot.v2.Event.get', payload)
        result = data.get('result')
        if not isinstance(result, dict):
            raise RuntimeError(f'Unexpected Bitrix event response: {data}')
        events = result.get('events')
        next_offset = result.get('nextOffset')
        if not isinstance(events, list) or (next_offset is not None and type(next_offset) is not int):
            raise RuntimeError(f'Unexpected Bitrix event response: {data}')
        parsed_events: list[BitrixBotEvent] = []
        for raw_event in events:
            if not isinstance(raw_event, dict):
                raise RuntimeError(f'Bitrix event cannot be acknowledged safely: {data}')
            event = BitrixBotEvent.from_api_payload(raw_event)
            if event is None:
                raise RuntimeError(f'Bitrix event cannot be acknowledged safely: {data}')
            parsed_events.append(event)
        return BitrixEventPage(events=tuple(parsed_events), next_offset=next_offset, has_more=bool(result.get('hasMore')))

    async def download_file(self, url: str) -> bytes:
        for attempt in range(1, self.settings.bitrix_retry_attempts + 1):
            try:
                async with self._request_semaphore:
                    response = await self._client.get(url)
                response.raise_for_status()
                return response.content
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500:
                    raise
                is_last_attempt = attempt >= self.settings.bitrix_retry_attempts
                if is_last_attempt:
                    raise
                logger.warning('Bitrix file download HTTP %s on attempt %s/%s', exc.response.status_code, attempt, self.settings.bitrix_retry_attempts)
                await asyncio.sleep(self.settings.bitrix_retry_base_delay_seconds)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError) as exc:
                is_last_attempt = attempt >= self.settings.bitrix_retry_attempts
                if is_last_attempt:
                    raise
                logger.warning('Bitrix file download failed on attempt %s/%s: %s', attempt, self.settings.bitrix_retry_attempts, exc)
                await asyncio.sleep(self.settings.bitrix_retry_base_delay_seconds)
        raise RuntimeError(f'Bitrix file download exhausted retries for {url}')

    async def download_file_by_id(self, file_id: int, fallback_url: str | None=None) -> bytes:
        if fallback_url:
            try:
                return await self.download_file(fallback_url)
            except Exception:
                logger.debug('fallback_url download failed for file_id=%s, trying imbot.v2.File.download', file_id)
        primary_url: str | None = None
        try:
            primary_url = await self._get_file_download_url(file_id)
        except Exception as exc:
            logger.warning('Failed to resolve download URL for Bitrix file_id=%s: %s', file_id, exc)
        candidate_urls = [u for u in (primary_url, fallback_url) if u]
        if not candidate_urls:
            raise RuntimeError(f'Unable to download Bitrix file_id={file_id}: no URLs resolved')
        errors: list[str] = []
        for url in candidate_urls:
            try:
                return await self.download_file(url)
            except Exception as exc:
                logger.warning('Failed to download Bitrix file_id=%s using %s: %s', file_id, url, exc)
                errors.append(f'{url}: {exc}')
        raise RuntimeError(f"Unable to download Bitrix file_id={file_id}. Tried: {' | '.join(errors)}")

    async def _get_file_download_url(self, file_id: int) -> str:
        payload: dict[str, Any] = {'botId': self.settings.bitrix_bot_id, 'botToken': self.settings.bitrix_bot_client_id, 'fileId': file_id}
        data = await self._call('imbot.v2.File.download', payload)
        result = data.get('result')
        if not isinstance(result, dict):
            raise RuntimeError(f'Unexpected Bitrix response for imbot.v2.File.download: {data}')
        download_url = result.get('downloadUrl')
        if not isinstance(download_url, str) or not download_url.strip():
            raise RuntimeError(f'Missing downloadUrl in imbot.v2.File.download response: {data}')
        return download_url.strip()

    async def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f'{self.settings.bitrix_webhook_base}/{method}'
        delay = self.settings.bitrix_retry_base_delay_seconds
        for attempt in range(1, self.settings.bitrix_retry_attempts + 1):
            try:
                now = asyncio.get_running_loop().time()
                wait_for = self._rate_last_request + self._rate_min_interval - now
                if wait_for > 0:
                    self._rate_last_request = now + wait_for
                    await asyncio.sleep(wait_for)
                else:
                    self._rate_last_request = now
                async with self._request_semaphore:
                    response = await self._client.post(url, json=payload)
                if response.status_code >= 500:
                    raise httpx.HTTPStatusError(f'Temporary Bitrix HTTP error: {response.status_code}', request=response.request, response=response)
                response.raise_for_status()
                data = cast(dict[str, Any], response.json())
                if 'error' in data:
                    error_code = str(data.get('error') or '')
                    if error_code.upper() in {'QUERY_LIMIT_EXCEEDED', 'TEMPORARY_ERROR'}:
                        raise RuntimeError(f'Temporary Bitrix error: {error_code}')
                    raise RuntimeError(f"Bitrix error: {data['error']} | {data.get('error_description', '')}")
                return data
            except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError, httpx.HTTPStatusError, RuntimeError) as exc:
                is_last_attempt = attempt >= self.settings.bitrix_retry_attempts
                is_retryable = self._is_retryable_exception(exc)
                if is_last_attempt or not is_retryable:
                    raise
                logger.warning('Bitrix call %s failed on attempt %s/%s: %s. Retrying in %.1fs', method, attempt, self.settings.bitrix_retry_attempts, exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.settings.bitrix_retry_max_delay_seconds)
        raise RuntimeError(f'Bitrix call {method} exhausted retries without returning a response')

    def _is_retryable_exception(self, exc: Exception) -> bool:
        if isinstance(exc, RuntimeError):
            return str(exc).startswith('Temporary Bitrix error:')
        if isinstance(exc, httpx.HTTPStatusError):
            http_status_error = cast(httpx.HTTPStatusError, exc)
            response = http_status_error.response
            status_code = response.status_code
            return status_code >= 500 or status_code in {408, 429}
        return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError))
