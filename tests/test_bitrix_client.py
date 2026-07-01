from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

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

    async def test_get_messages_page_parses_snapshot(self) -> None:
        self.client._call = AsyncMock(
            return_value={
                "result": {
                    "messages": [
                        {"id": 5, "author_id": "11", "text": "b", "params": {"FILE_ID": "2"}},
                        {"id": 4, "author_id": 10, "text": "a", "params": {"LIKE": ["9"]}},
                    ],
                    "users": [{"id": 10, "name": "Alice"}],
                    "files": {"2": {"id": 2, "name": "doc.txt"}},
                }
            }
        )
        snapshot = await self.client.get_messages_page(dialog_id="chat42", limit=2)
        self.assertEqual([item.message_id for item in snapshot.messages], [4, 5])
        self.assertEqual(snapshot.users_by_id[10].display_name, "Alice")
        self.assertEqual(snapshot.files_by_id[2].name, "doc.txt")

    async def test_get_recent_messages_combines_pages(self) -> None:
        self.client._call = AsyncMock(
            side_effect=[
                {
                    "result": {
                        "messages": [{"id": 7, "text": "w"}, {"id": 8, "text": "x"}],
                        "users": [{"id": 1, "name": "Alice"}],
                        "files": [],
                    }
                },
                {
                    "result": {
                        "messages": [{"id": 9, "text": "y"}, {"id": 10, "text": "z"}],
                        "users": [{"id": 2, "name": "Bob"}],
                        "files": [],
                    }
                },
            ]
        )
        with patch("bitrix_client.BITRIX_MESSAGES_PAGE_LIMIT", 2):
            snapshot = await self.client.get_recent_messages(dialog_id="chat42", limit_total=4)
        self.assertEqual([item.message_id for item in snapshot.messages], [7, 8, 9, 10])
        self.assertEqual(sorted(snapshot.users_by_id), [1, 2])
