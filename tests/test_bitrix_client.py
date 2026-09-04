from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any

import httpx

from bitrix_client import BitrixClient
from models import BitrixBotEvent, BitrixEventPage
from tests.helpers import make_settings


def make_client(responses: list[tuple[int, dict[str, Any], dict[str, str]]]) -> tuple[BitrixClient, list[httpx.Request]]:
    """Client whose transport replays `responses` (status, envelope, headers)."""
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        index = min(len(requests) - 1, len(responses) - 1)
        status, envelope, headers = responses[index]
        return httpx.Response(status, json=envelope, headers=headers)

    client = BitrixClient(make_settings())
    # Replace the real transport with the mock; the unused underlying client
    # never opens connections because no request goes through it.
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"X-Api-Key": "vibe-test-key", "Accept": "application/json"},
    )
    return client, requests


def ok(data: dict[str, Any]) -> tuple[int, dict[str, Any], dict[str, str]]:
    return (200, {"success": True, "data": data}, {})


class BitrixClientTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_send_message_posts_vibe_envelope_and_returns_id(self) -> None:
        client, requests = make_client([ok({"id": 321, "uuidMap": []})])
        self.addAsyncCleanup(client.close)

        message_id = await client.send_message("hello", dialog_id="chat42", reply_id=9)

        self.assertEqual(message_id, 321)
        request = requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(str(request.url), "https://vibe.example.com/v1/bots/7/messages")
        self.assertEqual(request.headers["X-Api-Key"], "vibe-test-key")
        body = json.loads(request.content)
        self.assertEqual(body["dialogId"], "chat42")
        self.assertEqual(body["fields"]["message"], "hello")
        self.assertIs(body["fields"]["urlPreview"], False)
        self.assertEqual(body["fields"]["replyId"], 9)

    async def test_update_message_patches_flat_body(self) -> None:
        client, requests = make_client([ok({"result": True})])
        self.addAsyncCleanup(client.close)

        await client.update_message(message_id=10, text="edited")

        request = requests[0]
        self.assertEqual(request.method, "PATCH")
        self.assertEqual(str(request.url), "https://vibe.example.com/v1/bots/7/messages/10")
        body = json.loads(request.content)
        self.assertEqual(body["message"], "edited")
        self.assertIs(body["urlPreview"], False)

    async def test_set_message_like_uses_post_and_delete_with_reaction_body(self) -> None:
        client, requests = make_client([ok({"result": True}), ok({"result": True})])
        self.addAsyncCleanup(client.close)

        await client.set_message_like(10, liked=True)
        await client.set_message_like(10, liked=False)

        self.assertEqual(requests[0].method, "POST")
        self.assertEqual(requests[1].method, "DELETE")
        for request in requests:
            self.assertEqual(str(request.url), "https://vibe.example.com/v1/bots/7/messages/10/reactions")
            self.assertEqual(json.loads(request.content), {"reaction": "like"})

    async def test_set_message_like_ignores_duplicate_errors(self) -> None:
        client, _ = make_client([
            (422, {"success": False, "error": {"code": "BITRIX_ERROR", "message": "REACTION_ALREADY_SET"}}, {}),
        ])
        self.addAsyncCleanup(client.close)
        await client.set_message_like(10, liked=True)

        client, _ = make_client([
            (422, {"success": False, "error": {"code": "BITRIX_ERROR", "message": "REACTION_NOT_FOUND"}}, {}),
        ])
        self.addAsyncCleanup(client.close)
        await client.set_message_like(10, liked=False)

    async def test_send_photo_uploads_and_returns_message_id(self) -> None:
        client, requests = make_client([ok({"file": {"id": 88}, "messageId": 1520})])
        self.addAsyncCleanup(client.close)

        message_id = await client.send_photo(caption="txt", filename="a.bin", content=b"abc", dialog_id="chat42")

        self.assertEqual(message_id, 1520)
        request = requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(str(request.url), "https://vibe.example.com/v1/bots/7/files")
        body = json.loads(request.content)
        self.assertEqual(body["dialogId"], "chat42")
        self.assertEqual(body["file"]["name"], "a.bin")
        self.assertEqual(body["file"]["content"], "YWJj")
        self.assertEqual(body["message"], "txt")

    async def test_send_photo_rejects_oversize_content(self) -> None:
        client, requests = make_client([ok({"messageId": 1})])
        self.addAsyncCleanup(client.close)
        client.settings = make_settings(bitrix_max_upload_file_bytes=1024)

        with self.assertRaisesRegex(RuntimeError, "File too large for Bitrix upload"):
            await client.send_photo(caption="", filename="a.bin", content=b"x" * 1025, dialog_id="chat42")
        self.assertEqual(requests, [])

    async def test_get_bot_events_parses_envelope_with_query_params(self) -> None:
        client, requests = make_client([ok({
            "events": [{"eventId": 8, "type": "ONIMBOTV2MESSAGEADD", "data": {"messageId": 3}}],
            "nextOffset": 9,
            "hasMore": True,
        })])
        self.addAsyncCleanup(client.close)

        page = await client.get_bot_events(offset=7, limit=1500)

        self.assertEqual(
            page,
            BitrixEventPage(
                events=(BitrixBotEvent(8, "ONIMBOTV2MESSAGEADD", {"messageId": 3}),),
                next_offset=9,
                has_more=True,
            ),
        )
        request = requests[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(
            str(request.url),
            "https://vibe.example.com/v1/bots/7/events?limit=1000&offset=7",
        )

    async def test_get_bot_events_omits_offset_when_unset(self) -> None:
        client, requests = make_client([ok({"events": [], "nextOffset": None, "hasMore": False})])
        self.addAsyncCleanup(client.close)

        page = await client.get_bot_events(offset=None, limit=0)

        self.assertEqual(page.events, ())
        self.assertIsNone(page.next_offset)
        self.assertFalse(page.has_more)
        self.assertEqual(str(requests[0].url), "https://vibe.example.com/v1/bots/7/events?limit=1")

    async def test_get_bot_events_rejects_malformed_payloads(self) -> None:
        invalid_data: tuple[object, ...] = ([], {}, {"events": {}}, {"events": [{}]}, {"events": [], "nextOffset": "9"}, {"events": [], "nextOffset": True})
        for payload in invalid_data:
            with self.subTest(payload=payload):
                client, _ = make_client([(200, {"success": True, "data": payload}, {})])
                self.addAsyncCleanup(client.close)
                with self.assertRaises(RuntimeError):
                    await client.get_bot_events(offset=None)

    async def test_transient_bitrix_error_is_retried_then_succeeds(self) -> None:
        client, requests = make_client([
            (422, {"success": False, "error": {"code": "BITRIX_ERROR", "message": "Operational time limit exceeded: OPERATION_TIME_LIMIT"}}, {}),
            ok({"id": 5}),
        ])
        self.addAsyncCleanup(client.close)

        message_id = await client.send_message("hi", dialog_id="chat42")

        self.assertEqual(message_id, 5)
        self.assertEqual(len(requests), 2)

    async def test_non_retryable_vibe_error_raises_immediately(self) -> None:
        client, requests = make_client([
            (400, {"success": False, "error": {"code": "INVALID_BOT_ID", "message": "bad"}}, {}),
        ])
        self.addAsyncCleanup(client.close)

        with self.assertRaisesRegex(RuntimeError, "Vibe error: INVALID_BOT_ID"):
            await client.send_message("hi", dialog_id="chat42")
        self.assertEqual(len(requests), 1)

    def test_documented_transient_errors_are_retryable(self) -> None:
        client, _ = make_client([ok({})])
        self.addAsyncCleanup(client.close)
        for code in ("OPERATION_TIME_LIMIT", "OVERLOAD_LIMIT"):
            with self.subTest(code=code):
                self.assertTrue(client._is_retryable_exception(RuntimeError(f"Temporary Vibe error: {code}")))

    async def test_429_honours_retry_after_header(self) -> None:
        client, requests = make_client([
            (429, {"success": False, "error": {"code": "RATE_LIMITED", "message": "slow down"}}, {"Retry-After": "2"}),
            ok({"id": 6}),
        ])
        self.addAsyncCleanup(client.close)

        sleeps: list[float] = []
        original_sleep = asyncio.sleep

        async def fake_sleep(delay: float, *args: Any, **kwargs: Any) -> None:
            sleeps.append(delay)
            await original_sleep(0)

        import unittest.mock as mock
        with mock.patch("asyncio.sleep", side_effect=fake_sleep):
            message_id = await client.send_message("hi", dialog_id="chat42")

        self.assertEqual(message_id, 6)
        self.assertEqual(len(requests), 2)
        self.assertIn(2.0, sleeps)


    async def test_get_file_meta_resolves_disk_entity_after_denied_attempt(self) -> None:
        client, requests = make_client([
            (403, {"success": False, "error": {"code": "BITRIX_ACCESS_DENIED", "message": "disk lag"}}, {}),
            ok({"ID": 9, "NAME": "pic.jpg", "MIME_TYPE": "image/jpeg", "DOWNLOAD_URL": "https://example.com/pic.jpg"}),
        ])
        self.addAsyncCleanup(client.close)

        sleeps: list[float] = []
        original_sleep = asyncio.sleep

        async def fake_sleep(delay: float, *args: Any, **kwargs: Any) -> None:
            sleeps.append(delay)
            await original_sleep(0)

        import unittest.mock as mock
        with mock.patch("asyncio.sleep", side_effect=fake_sleep):
            meta = await client.get_file_meta(9)

        self.assertIsNotNone(meta)
        assert meta is not None
        self.assertEqual(meta.name, "pic.jpg")
        self.assertTrue(meta.is_image)
        self.assertEqual(str(requests[0].url), "https://vibe.example.com/v1/files/9")
        self.assertEqual(len(requests), 2)

    async def test_get_file_meta_returns_none_after_persistent_denial(self) -> None:
        client, _ = make_client([
            (403, {"success": False, "error": {"code": "BITRIX_ACCESS_DENIED", "message": "not ours"}}, {}),
        ])
        self.addAsyncCleanup(client.close)

        import unittest.mock as mock
        with mock.patch("asyncio.sleep"):
            self.assertIsNone(await client.get_file_meta(9))

    async def test_get_file_meta_propagates_unrelated_errors(self) -> None:
        client, _ = make_client([
            (403, {"success": False, "error": {"code": "SCOPE_DENIED", "message": "no disk scope"}}, {}),
        ])
        self.addAsyncCleanup(client.close)

        with self.assertRaisesRegex(RuntimeError, "SCOPE_DENIED"):
            await client.get_file_meta(9)

    async def test_download_file_by_id_re_resolves_single_use_url_each_attempt(self) -> None:
        resolve_fail = (422, {"success": False, "error": {"code": "BITRIX_ERROR", "message": "nope"}}, {})
        client, requests = make_client([
            resolve_fail,
            resolve_fail,
        ])
        self.addAsyncCleanup(client.close)

        download_attempts = {"count": 0}

        async def fake_download(url: str) -> bytes:
            download_attempts["count"] += 1
            raise httpx.ConnectError("down")

        client.download_file = fake_download  # type: ignore[method-assign]
        import unittest.mock as mock
        with mock.patch("asyncio.sleep"):
            with self.assertRaisesRegex(RuntimeError, "Unable to download Bitrix file_id=9"):
                await client.download_file_by_id(9)
        # bitrix_retry_attempts=2 in fixture settings -> two resolve attempts
        self.assertEqual(sum(1 for r in requests if r.url.path.endswith("/files/9")), 2)

    def test_bitrix_delivery_code_contains_no_user_scoped_message_methods(self) -> None:
        from pathlib import Path
        sources = [
            Path("bitrix_client.py").read_text(encoding="utf-8"),
            Path("mirror_service.py").read_text(encoding="utf-8"),
        ]
        combined = "\n".join(sources)
        self.assertNotIn("im.dialog.messages.get", combined)
        self.assertNotIn("im.dialog.messages.search", combined)


if __name__ == "__main__":
    unittest.main()
