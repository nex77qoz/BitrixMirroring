from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any, cast

import httpx

from models import BitrixBotEvent, BitrixEventPage, BitrixFile
from settings import Settings

logger = logging.getLogger('tg-bitrix-mirror')

_TRANSIENT_BITRIX_SUBSTRINGS = (
    'QUERY_LIMIT_EXCEEDED',
    'TEMPORARY_ERROR',
    'OPERATION_TIME_LIMIT',
    'OVERLOAD_LIMIT',
)
_RETRYABLE_VIBE_CODES = {'RATE_LIMITED', 'BITRIX_UNAVAILABLE', 'INTERNAL_ERROR'}


class BitrixClient:

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(
            timeout=settings.request_timeout_seconds,
            proxy=settings.socks5_proxy_url,
            headers={'X-Api-Key': settings.vibe_api_key, 'Accept': 'application/json'},
        )
        self._request_semaphore = asyncio.Semaphore(settings.bitrix_max_concurrent_requests)
        self._rate_last_request: float = 0.0
        self._rate_min_interval: float = 1.0 / max(settings.bitrix_max_concurrent_requests, 1)

    async def close(self) -> None:
        await self._client.aclose()

    async def send_message(self, text: str, *, dialog_id: str, reply_id: int | None=None) -> int:
        fields: dict[str, Any] = {'message': text, 'system': False, 'urlPreview': not self.settings.disable_link_preview}
        if reply_id is not None:
            fields['replyId'] = reply_id
        body: dict[str, Any] = {'dialogId': dialog_id, 'fields': fields}
        data = await self._request('POST', f'/bots/{self.settings.bitrix_bot_id}/messages', json_body=body)
        message_id = data.get('id')
        if not isinstance(message_id, int):
            raise RuntimeError(f'Missing id in Vibe message send response: {data}')
        return message_id

    async def update_message(self, *, message_id: int, text: str) -> None:
        body: dict[str, Any] = {'message': text, 'urlPreview': not self.settings.disable_link_preview}
        data = await self._request('PATCH', f'/bots/{self.settings.bitrix_bot_id}/messages/{message_id}', json_body=body)
        if data.get('result') is not True:
            raise RuntimeError(f'Unexpected Vibe response: {data}')

    async def set_message_like(self, message_id: int, *, liked: bool) -> None:
        path = f'/bots/{self.settings.bitrix_bot_id}/messages/{message_id}/reactions'
        try:
            if liked:
                data = await self._request('POST', path, json_body={'reaction': 'like'})
            else:
                data = await self._request('DELETE', path, json_body={'reaction': 'like'})
        except RuntimeError as exc:
            err = str(exc)
            if 'REACTION_ALREADY_SET' in err or 'REACTION_NOT_FOUND' in err:
                return
            raise
        if data.get('result') is not True:
            raise RuntimeError(f'Unexpected Vibe response: {data}')

    async def send_photo(self, *, caption: str, filename: str, content: bytes, dialog_id: str) -> int:
        limit = self.settings.bitrix_max_upload_file_bytes
        if len(content) > limit:
            raise RuntimeError(f'File too large for Bitrix upload: {len(content)} > {limit}')
        encoded = base64.b64encode(content).decode('ascii')
        return await self._upload_file(dialog_id=dialog_id, filename=filename, encoded=encoded, caption=caption)

    async def _upload_file(self, *, dialog_id: str, filename: str, encoded: str, caption: str) -> int:
        body: dict[str, Any] = {'dialogId': dialog_id, 'file': {'name': filename, 'content': encoded}, 'message': caption}
        data = await self._request('POST', f'/bots/{self.settings.bitrix_bot_id}/files', json_body=body)
        message_id = data.get('messageId')
        if not isinstance(message_id, int):
            raise RuntimeError(f'Missing messageId in Vibe file upload response: {data}')
        return message_id

    async def get_bot_events(self, *, offset: int | None, limit: int=100) -> BitrixEventPage:
        params: dict[str, Any] = {'limit': max(1, min(limit, 1000))}
        if offset is not None:
            params['offset'] = offset
        data = await self._request('GET', f'/bots/{self.settings.bitrix_bot_id}/events', params=params)
        events = data.get('events')
        next_offset = data.get('nextOffset')
        if not isinstance(events, list) or (next_offset is not None and type(next_offset) is not int):
            raise RuntimeError(f'Unexpected Vibe event response: {data}')
        parsed_events: list[BitrixBotEvent] = []
        for raw_event in events:
            if not isinstance(raw_event, dict):
                raise RuntimeError(f'Bitrix event cannot be acknowledged safely: {data}')
            event = BitrixBotEvent.from_api_payload(raw_event)
            if event is None:
                raise RuntimeError(f'Bitrix event cannot be acknowledged safely: {data}')
            parsed_events.append(event)
        return BitrixEventPage(events=tuple(parsed_events), next_offset=next_offset, has_more=bool(data.get('hasMore')))

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
        # Vibe downloadUrl is single-use: every attempt must re-resolve a fresh link.
        errors: list[str] = []
        for attempt in range(1, self.settings.bitrix_retry_attempts + 1):
            try:
                primary_url = await self._get_file_download_url(file_id)
            except Exception as exc:
                logger.warning('Failed to resolve download URL for Bitrix file_id=%s (attempt %s/%s): %s', file_id, attempt, self.settings.bitrix_retry_attempts, exc)
                errors.append(f'resolve: {exc}')
                continue
            try:
                return await self.download_file(primary_url)
            except Exception as exc:
                logger.warning('Failed to download Bitrix file_id=%s using %s: %s', file_id, primary_url, exc)
                errors.append(f'{primary_url}: {exc}')
        if fallback_url:
            try:
                return await self.download_file(fallback_url)
            except Exception as exc:
                errors.append(f'{fallback_url}: {exc}')
        if not errors:
            raise RuntimeError(f'Unable to download Bitrix file_id={file_id}: no URLs resolved')
        raise RuntimeError(f"Unable to download Bitrix file_id={file_id}. Tried: {' | '.join(errors)}")

    async def _get_file_download_url(self, file_id: int) -> str:
        data = await self._request('GET', f'/bots/{self.settings.bitrix_bot_id}/files/{file_id}')
        download_url = data.get('downloadUrl')
        if not isinstance(download_url, str) or not download_url.strip():
            raise RuntimeError(f'Missing downloadUrl in Vibe file download response: {data}')
        return download_url.strip()

    async def get_file_meta(self, file_id: int) -> BitrixFile | None:
        """Read disk metadata for a chat attachment via GET /files/{file_id}.

        The Bitrix24 disk index can lag the bot event by 1-2 seconds, so a
        fresh 403/404 is retried once after a short pause. Denials that persist
        and network failures yield None (caller falls back to a placeholder);
        other errors propagate.
        """
        for attempt in range(1, 3):
            try:
                data = await self._request('GET', f'/files/{file_id}')
            except RuntimeError as exc:
                if not _is_file_meta_denied(str(exc)):
                    raise
                if attempt < 2:
                    await asyncio.sleep(1.5)
                    continue
                logger.warning('File metadata for Bitrix file_id=%s unavailable: %s', file_id, exc)
                return None
            except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError, httpx.HTTPStatusError) as exc:
                if attempt < 2:
                    await asyncio.sleep(1.5)
                    continue
                logger.warning('File metadata request failed for Bitrix file_id=%s: %s', file_id, exc)
                return None
            file_meta = BitrixFile.from_api_payload(data)
            if file_meta is None:
                logger.warning('Unexpected file metadata payload for Bitrix file_id=%s: %s', file_id, data)
            return file_meta
        return None

    async def _request(self, verb: str, path: str, *, json_body: dict | None=None, params: dict | None=None) -> dict[str, Any]:
        url = f'{self.settings.vibe_base_url}{path}'
        delay = self.settings.bitrix_retry_base_delay_seconds
        for attempt in range(1, self.settings.bitrix_retry_attempts + 1):
            retry_after: float | None = None
            try:
                now = asyncio.get_running_loop().time()
                wait_for = self._rate_last_request + self._rate_min_interval - now
                if wait_for > 0:
                    self._rate_last_request = now + wait_for
                    await asyncio.sleep(wait_for)
                else:
                    self._rate_last_request = now
                async with self._request_semaphore:
                    response = await self._client.request(verb, url, json=json_body, params=params)
                if response.status_code >= 500 or response.status_code in {408, 429}:
                    raise httpx.HTTPStatusError(f'Temporary Vibe HTTP error: {response.status_code}', request=response.request, response=response)
                data = cast(dict[str, Any], response.json())
                if response.status_code >= 400 or not data.get('success', True):
                    error_obj = data.get('error')
                    error: dict[str, Any] = error_obj if isinstance(error_obj, dict) else {}
                    code = str(error.get('code') or response.status_code)
                    message = str(error.get('message') or '')
                    if code in _RETRYABLE_VIBE_CODES:
                        raise RuntimeError(f'Temporary Vibe error: {code}')
                    if code == 'BITRIX_ERROR' and any(token in message for token in _TRANSIENT_BITRIX_SUBSTRINGS):
                        raise RuntimeError(f'Temporary Vibe error: {code}')
                    raise RuntimeError(f'Vibe error: {code} | {message}')
                payload = data.get('data')
                return payload if isinstance(payload, dict) else {}
            except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError, httpx.HTTPStatusError, RuntimeError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                    retry_after = _parse_retry_after(exc.response.headers.get('Retry-After'))
                is_last_attempt = attempt >= self.settings.bitrix_retry_attempts
                is_retryable = self._is_retryable_exception(exc)
                if is_last_attempt or not is_retryable:
                    raise
                sleep_for = delay if retry_after is None else max(delay, retry_after)
                logger.warning('Vibe call %s %s failed on attempt %s/%s: %s. Retrying in %.1fs', verb, path, attempt, self.settings.bitrix_retry_attempts, exc, sleep_for)
                await asyncio.sleep(sleep_for)
                delay = min(delay * 2, self.settings.bitrix_retry_max_delay_seconds)
        raise RuntimeError(f'Vibe call {verb} {path} exhausted retries without returning a response')

    def _is_retryable_exception(self, exc: Exception) -> bool:
        if isinstance(exc, RuntimeError):
            return str(exc).startswith('Temporary Vibe error:')
        if isinstance(exc, httpx.HTTPStatusError):
            http_status_error = cast(httpx.HTTPStatusError, exc)
            response = http_status_error.response
            status_code = response.status_code
            return status_code >= 500 or status_code in {408, 429}
        return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError))


def _is_file_meta_denied(text: str) -> bool:
    # Only auth/lookup failures are worth the silent single retry; anything
    # else must propagate so callers see the real transport error.
    return any(token in text for token in (
        'Vibe error: 403', 'Vibe error: 404',
        'BITRIX_ACCESS_DENIED', 'NOT_FOUND', 'FORBIDDEN', 'ACCESS_DENIED',
    ))


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = int(value.strip())
    except ValueError:
        return None
    return max(float(seconds), 0.0)
