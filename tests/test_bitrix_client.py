from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any

import httpx

from bitrix_client import BitrixClient
from models import BitrixBotEvent, BitrixEventPage
from tests.helpers import make_settings

WEBHOOK = "https://portal.example.com/rest/1/hook"


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
        headers={"Accept": "application/json"},
    )
    return client, requests


def ok(data: dict[str, Any]) -> tuple[int, dict[str, Any], dict[str, str]]:
    return (200, {"result": data}, {})


class BitrixClientTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_file_download_never_sends_webhook_credentials_to_download_host(self) -> None:
        client, _ = make_client([ok({})])
        self.addAsyncCleanup(client.close)
        await client._download_client.aclose()
        seen: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=b"file")

        client._download_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        self.assertEqual(await client.download_file("https://files.example.test/object"), b"file")
        self.assertIsNone(seen[0].headers.get("X-Api-Key"))

    async def test_send_message_calls_imbot_v2_and_returns_id(self) -> None:
        client, requests = make_client([ok({"id": 321, "uuidMap": []})])
        self.addAsyncCleanup(client.close)

        message_id = await client.send_message("hello", dialog_id="chat42", reply_id=9)

        self.assertEqual(message_id, 321)
        request = requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(str(request.url), f"{WEBHOOK}/imbot.v2.Chat.Message.send")
        body = json.loads(request.content)
        self.assertEqual(body["botId"], 7)
        self.assertEqual(body["botToken"], "bot-token")
        self.assertEqual(body["dialogId"], "chat42")
        self.assertEqual(body["fields"]["message"], "hello")
        self.assertIs(body["fields"]["urlPreview"], False)
        self.assertEqual(body["fields"]["replyId"], 9)

    async def test_update_message_sends_fields(self) -> None:
        client, requests = make_client([ok({"result": True})])
        self.addAsyncCleanup(client.close)

        await client.update_message(message_id=10, text="edited")

        request = requests[0]
        self.assertEqual(str(request.url), f"{WEBHOOK}/imbot.v2.Chat.Message.update")
        body = json.loads(request.content)
        self.assertEqual(body["messageId"], 10)
        self.assertEqual(body["fields"]["message"], "edited")
        self.assertIs(body["fields"]["urlPreview"], False)

    async def test_set_message_like_uses_reaction_add_and_delete(self) -> None:
        client, requests = make_client([ok({"result": True}), ok({"result": True})])
        self.addAsyncCleanup(client.close)

        await client.set_message_like(10, liked=True)
        await client.set_message_like(10, liked=False)

        self.assertEqual(str(requests[0].url), f"{WEBHOOK}/imbot.v2.Chat.Message.Reaction.add")
        self.assertEqual(str(requests[1].url), f"{WEBHOOK}/imbot.v2.Chat.Message.Reaction.delete")
        for request in requests:
            body = json.loads(request.content)
            self.assertEqual((body["messageId"], body["reaction"]), (10, "like"))

    async def test_set_message_like_ignores_duplicate_errors(self) -> None:
        client, _ = make_client([
            (422, {"error": "REACTION_ALREADY_SET", "error_description": "REACTION_ALREADY_SET"}, {}),
        ])
        self.addAsyncCleanup(client.close)
        await client.set_message_like(10, liked=True)

        client, _ = make_client([
            (422, {"error": "REACTION_NOT_FOUND", "error_description": "REACTION_NOT_FOUND"}, {}),
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
        self.assertEqual(str(request.url), f"{WEBHOOK}/imbot.v2.File.upload")
        body = json.loads(request.content)
        self.assertEqual(body["dialogId"], "chat42")
        self.assertEqual(body["fields"]["name"], "a.bin")
        self.assertEqual(body["fields"]["content"], "YWJj")
        self.assertEqual(body["fields"]["message"], "txt")

    async def test_send_photo_rejects_oversize_content(self) -> None:
        client, requests = make_client([ok({"messageId": 1})])
        self.addAsyncCleanup(client.close)
        client.settings = make_settings(bitrix_max_upload_file_bytes=1024)

        with self.assertRaisesRegex(RuntimeError, "File too large for Bitrix upload"):
            await client.send_photo(caption="", filename="a.bin", content=b"x" * 1025, dialog_id="chat42")
        self.assertEqual(requests, [])

    async def test_get_bot_events_parses_result_with_params(self) -> None:
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
        self.assertEqual(str(request.url), f"{WEBHOOK}/imbot.v2.Event.get")
        body = json.loads(request.content)
        self.assertEqual((body["limit"], body["offset"]), (1000, 7))

    async def test_get_bot_events_omits_offset_when_unset(self) -> None:
        client, requests = make_client([ok({"events": [], "nextOffset": None, "hasMore": False})])
        self.addAsyncCleanup(client.close)

        page = await client.get_bot_events(offset=None, limit=0)

        self.assertEqual(page.events, ())
        self.assertIsNone(page.next_offset)
        self.assertFalse(page.has_more)
        body = json.loads(requests[0].content)
        self.assertEqual(body["limit"], 1)
        self.assertNotIn("offset", body)

    async def test_get_bot_events_rejects_malformed_envelopes(self) -> None:
        invalid_data: tuple[object, ...] = ([], {}, {"events": {}}, {"events": [], "nextOffset": "9"}, {"events": [], "nextOffset": True})
        for payload in invalid_data:
            with self.subTest(payload=payload):
                client, _ = make_client([(200, {"result": payload}, {})])
                self.addAsyncCleanup(client.close)
                with self.assertRaises(RuntimeError):
                    await client.get_bot_events(offset=None)

    async def test_get_bot_events_skips_malformed_events_without_dropping_the_page(self) -> None:
        # One bad event must not stall the whole page (or the offset that covers it).
        client, _ = make_client([ok({
            "events": [{}, {"eventId": 8, "type": "ONIMBOTV2MESSAGEADD", "data": {"messageId": 3}}],
            "nextOffset": 9,
            "hasMore": False,
        })])
        self.addAsyncCleanup(client.close)

        page = await client.get_bot_events(offset=None)

        self.assertEqual(page.events, (BitrixBotEvent(8, "ONIMBOTV2MESSAGEADD", {"messageId": 3}),))
        self.assertEqual(page.next_offset, 9)

    async def test_transient_bitrix_error_is_retried_then_succeeds(self) -> None:
        client, requests = make_client([
            (422, {"error": "OPERATION_TIME_LIMIT", "error_description": "Operational time limit exceeded: OPERATION_TIME_LIMIT"}, {}),
            ok({"id": 5}),
        ])
        self.addAsyncCleanup(client.close)

        message_id = await client.send_message("hi", dialog_id="chat42")

        self.assertEqual(message_id, 5)
        self.assertEqual(len(requests), 2)

    async def test_non_retryable_bitrix_error_raises_immediately(self) -> None:
        client, requests = make_client([
            (400, {"error": "INVALID_BOT_ID", "error_description": "bad"}, {}),
        ])
        self.addAsyncCleanup(client.close)

        with self.assertRaisesRegex(RuntimeError, "Bitrix error: INVALID_BOT_ID"):
            await client.send_message("hi", dialog_id="chat42")
        self.assertEqual(len(requests), 1)

    async def test_send_message_does_not_retry_after_ambiguous_timeout(self) -> None:
        # A ReadTimeout on message creation gives no signal on whether Bitrix
        # already received and processed the POST. Retrying blindly here is
        # what causes duplicate messages (chat443900 incident), so send_message
        # must surface the error instead of resending.
        client = BitrixClient(make_settings())
        self.addAsyncCleanup(client.close)
        call_count = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            raise httpx.ReadTimeout("timed out", request=request)

        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            headers={"Accept": "application/json"},
        )

        with self.assertRaises(httpx.ReadTimeout):
            await client.send_message("hi", dialog_id="chat42")
        self.assertEqual(call_count, 1)

    async def test_get_bot_events_retries_after_ambiguous_timeout(self) -> None:
        # GET is idempotent: retrying after a timeout is safe and expected.
        client = BitrixClient(make_settings())
        self.addAsyncCleanup(client.close)
        call_count = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ReadTimeout("timed out", request=request)
            return httpx.Response(200, json={"result": {"events": [], "nextOffset": None, "hasMore": False}})

        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            headers={"Accept": "application/json"},
        )

        page = await client.get_bot_events(offset=None)

        self.assertEqual(call_count, 2)
        self.assertEqual(page.events, ())

    async def test_webhook_url_is_not_throttled_by_default(self) -> None:
        client, requests = make_client([ok({"id": 1}), ok({"id": 2}), ok({"id": 3})])
        self.addAsyncCleanup(client.close)

        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.gather(*(client.send_message("x", dialog_id="chat1") for _ in range(3)))

        self.assertEqual(len(requests), 3)
        self.assertLess(loop.time() - started, 0.5)

    async def test_non_json_response_points_at_webhook_url(self) -> None:
        client = BitrixClient(make_settings())
        self.addAsyncCleanup(client.close)

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>login</html>")

        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        with self.assertRaisesRegex(RuntimeError, "BITRIX_WEBHOOK_URL"):
            await client.get_bot_events(offset=None)

    def test_documented_transient_errors_are_retryable(self) -> None:
        client, _ = make_client([ok({})])
        self.addAsyncCleanup(client.close)
        for code in ("OPERATION_TIME_LIMIT", "OVERLOAD_LIMIT"):
            with self.subTest(code=code):
                self.assertTrue(client._is_retryable_exception(RuntimeError(f"Temporary Bitrix error: {code}")))

    async def test_429_honours_retry_after_header(self) -> None:
        client, requests = make_client([
            (429, {"error": "QUERY_LIMIT_EXCEEDED", "error_description": "slow down"}, {"Retry-After": "2"}),
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
            (403, {"error": "ACCESS_DENIED", "error_description": "disk lag"}, {}),
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
        self.assertEqual(str(requests[0].url), f"{WEBHOOK}/disk.file.get")
        self.assertEqual(len(requests), 2)

    async def test_get_file_meta_returns_none_after_persistent_denial(self) -> None:
        client, _ = make_client([
            (403, {"error": "ACCESS_DENIED", "error_description": "not ours"}, {}),
        ])
        self.addAsyncCleanup(client.close)

        import unittest.mock as mock
        with mock.patch("asyncio.sleep"):
            self.assertIsNone(await client.get_file_meta(9))

    async def test_get_file_meta_propagates_unrelated_errors(self) -> None:
        client, _ = make_client([
            (403, {"error": "INSUFFICIENT_SCOPE", "error_description": "no disk scope"}, {}),
        ])
        self.addAsyncCleanup(client.close)

        with self.assertRaisesRegex(RuntimeError, "INSUFFICIENT_SCOPE"):
            await client.get_file_meta(9)

    async def test_download_file_by_id_re_resolves_url_each_attempt(self) -> None:
        resolve_fail: tuple[int, dict[str, Any], dict[str, str]] = (400, {"error": "BITRIX_ERROR", "error_description": "nope"}, {})
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
        self.assertEqual(sum(1 for r in requests if r.url.path.endswith("imbot.v2.File.download")), 2)

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
