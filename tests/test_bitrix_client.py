from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from bitrix_client import BitrixClient
from models import BitrixBotEvent, BitrixEventPage
from tests.helpers import make_settings


class BitrixClientTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = BitrixClient(make_settings())
        self.addAsyncCleanup(self.client.close)

    async def test_send_message_returns_message_id(self) -> None:
        self.client._call = AsyncMock(return_value={"result": {"id": 321}})
        message_id = await self.client.send_message("hello", dialog_id="chat42", reply_id=9)
        self.assertEqual(message_id, 321)
        self.client._call.assert_awaited_once()

    async def test_set_message_like_ignores_duplicate_errors(self) -> None:
        self.client._call = AsyncMock(side_effect=RuntimeError("Bitrix error: REACTION_ALREADY_SET"))
        await self.client.set_message_like(10, liked=True)

    async def test_update_message_accepts_v2_nested_success_result(self) -> None:
        self.client._call = AsyncMock(return_value={"result": {"result": True}})

        await self.client.update_message(message_id=10, text="edited")

    async def test_set_message_like_accepts_v2_nested_success_result(self) -> None:
        self.client._call = AsyncMock(return_value={"result": {"result": True}})

        await self.client.set_message_like(10, liked=True)

    async def test_get_bot_events_parses_page_and_sends_exact_payload(self) -> None:
        self.client._call = AsyncMock(
            return_value={
                "result": {
                    "events": [{"eventId": 8, "type": "MESSAGE_ADD", "data": {"messageId": 3}}],
                    "nextOffset": 9,
                    "hasMore": 1,
                }
            }
        )

        page = await self.client.get_bot_events(offset=7, limit=1500)

        self.assertEqual(
            page,
            BitrixEventPage(
                events=(BitrixBotEvent(8, "MESSAGE_ADD", {"messageId": 3}),),
                next_offset=9,
                has_more=True,
            ),
        )
        self.client._call.assert_awaited_once_with(
            "imbot.v2.Event.get",
            {"botId": 7, "botToken": "bot-token", "limit": 1000, "offset": 7},
        )

    async def test_get_bot_events_omits_offset_and_clamps_low_limit(self) -> None:
        self.client._call = AsyncMock(
            return_value={"result": {"events": [], "nextOffset": None, "hasMore": False}}
        )

        await self.client.get_bot_events(offset=None, limit=0)

        self.client._call.assert_awaited_once_with(
            "imbot.v2.Event.get",
            {"botId": 7, "botToken": "bot-token", "limit": 1},
        )

    async def test_get_bot_events_rejects_malformed_response(self) -> None:
        invalid_results: tuple[object, ...] = (
            [],
            {"events": {}},
            {"events": [{}]},
            {"events": [], "nextOffset": "9"},
            {"events": [], "nextOffset": True},
        )
        for result in invalid_results:
            with self.subTest(result=result):
                self.client._call = AsyncMock(return_value={"result": result})
                with self.assertRaises(RuntimeError):
                    await self.client.get_bot_events(offset=None)

    def test_bitrix_delivery_code_contains_no_user_scoped_message_methods(self) -> None:
        from pathlib import Path
        sources = [
            Path("bitrix_client.py").read_text(encoding="utf-8"),
            Path("mirror_service.py").read_text(encoding="utf-8"),
        ]
        combined = "\n".join(sources)
        self.assertNotIn("im.dialog.messages.get", combined)
        self.assertNotIn("im.dialog.messages.search", combined)
