from __future__ import annotations

import unittest

from models import BitrixFile, BitrixMessage, BitrixUser


class ModelsTestCase(unittest.TestCase):
    def test_bitrix_user_uses_full_name(self) -> None:
        user = BitrixUser.from_api_payload({"id": 5, "last_name": "Ivanov", "NAME": "Ivan"})
        self.assertEqual(user.display_name, "Ivanov Ivan")

    def test_bitrix_message_parses_reply_likes_and_flags(self) -> None:
        message = BitrixMessage.from_api_payload(
            {
                "id": 10,
                "author_id": "77",
                "text": "hello",
                "date_update": "123",
                "params": {
                    "FILE_ID": ["1", 2],
                    "LIKE": ["4", 3],
                    "REPLY_ID": "99",
                    "STICKER_PARAMS": {"code": "x"},
                    "MEETING_CONFIRM": True,
                    "TASK_ID": 11,
                },
            }
        )
        self.assertIsNotNone(message)
        assert message is not None
        self.assertEqual(message.author_id, 77)
        self.assertEqual(message.file_ids, (1, 2))
        self.assertEqual(message.like_user_ids, (3, 4))
        self.assertEqual(message.reply_id, 99)
        self.assertTrue(message.is_sticker)
        self.assertTrue(message.is_meeting)
        self.assertTrue(message.is_task)
        self.assertEqual(message.update_time_unix, 123)

    def test_bitrix_file_detects_image_and_author(self) -> None:
        file = BitrixFile.from_api_payload(
            {
                "ID": "12",
                "original_name": "photo.JPG",
                "TYPE": "file",
                "MIME_TYPE": "image/jpeg",
                "DOWNLOAD_URL": "https://example.test/file",
                "AUTHOR_ID": "55",
            }
        )
        self.assertIsNotNone(file)
        assert file is not None
        self.assertEqual(file.file_id, 12)
        self.assertTrue(file.is_image)
        self.assertEqual(file.author_id, 55)
        self.assertEqual(file.url_download, "https://example.test/file")

