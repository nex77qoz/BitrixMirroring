from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

_logger = logging.getLogger("tg-bitrix-mirror")


@dataclass(frozen=True)
class BitrixUser:
    user_id: int
    display_name: str

    @staticmethod
    def from_api_payload(payload: dict[str, Any]) -> Optional["BitrixUser"]:
        raw_user_id = payload.get("id")
        if not isinstance(raw_user_id, int):
            return None

        last_name = str(payload.get("last_name") or payload.get("LAST_NAME") or "").strip()
        first_name = str(payload.get("first_name") or payload.get("NAME") or "").strip()
        full_name = " ".join(part for part in [last_name, first_name] if part).strip()
        display_name = full_name or str(payload.get("name") or "").strip() or f"Bitrix user_id: {raw_user_id}"
        return BitrixUser(user_id=raw_user_id, display_name=display_name)


@dataclass(frozen=True)
class BitrixDialogSnapshot:
    messages: list["BitrixMessage"]
    users_by_id: dict[int, BitrixUser]
    files_by_id: dict[int, "BitrixFile"]


@dataclass(frozen=True)
class BitrixMessage:
    message_id: int
    author_id: Optional[int]
    text: str
    file_ids: tuple[int, ...]
    update_time_unix: Optional[int]
    like_user_ids: tuple[int, ...]
    reply_id: Optional[int] = None
    is_sticker: bool = False
    is_meeting: bool = False
    is_task: bool = False

    @staticmethod
    def from_api_payload(payload: dict[str, Any]) -> Optional["BitrixMessage"]:
        raw_message_id = payload.get("id")
        if not isinstance(raw_message_id, int):
            return None

        raw_author_id = payload.get("author_id")
        author_id: Optional[int]
        if isinstance(raw_author_id, int):
            author_id = raw_author_id
        elif isinstance(raw_author_id, str) and raw_author_id.strip().isdigit():
            author_id = int(raw_author_id.strip())
        else:
            author_id = None

        params = payload.get("params")
        file_ids: list[int] = []
        if isinstance(params, dict):
            raw_file_ids = (
                params.get("FILE_ID")
                or params.get("DISK_ID")
                or params.get("FILES")
                or params.get("fileId")
                or params.get("diskId")
                or params.get("file_ids")
                or params.get("disk_id")
            )
            if isinstance(raw_file_ids, list):
                for raw_file_id in raw_file_ids:
                    if isinstance(raw_file_id, int):
                        file_ids.append(raw_file_id)
                    elif isinstance(raw_file_id, str) and raw_file_id.strip().isdigit():
                        file_ids.append(int(raw_file_id.strip()))
            elif isinstance(raw_file_ids, int):
                file_ids.append(raw_file_ids)
            elif isinstance(raw_file_ids, str):
                for part in raw_file_ids.split(","):
                    normalized = part.strip()
                    if normalized.isdigit():
                        file_ids.append(int(normalized))

        like_user_ids: list[int] = []
        if isinstance(params, dict):
            raw_likes = params.get("LIKE")
            if raw_likes is None:
                raw_likes = params.get("like")
            if isinstance(raw_likes, list):
                for uid in raw_likes:
                    if isinstance(uid, int):
                        like_user_ids.append(uid)
                    elif isinstance(uid, str) and uid.strip().isdigit():
                        like_user_ids.append(int(uid.strip()))

        is_sticker = isinstance(params, dict) and bool(params.get("STICKER_PARAMS"))
        is_meeting = isinstance(params, dict) and bool(params.get("MEETING_CONFIRM"))
        is_task = isinstance(params, dict) and bool(params.get("TASK_ID"))

        reply_id: Optional[int] = None
        # Check top-level payload fields first (im.dialog.messages.get may return it here)
        for key in ("reply_id", "replyId", "REPLY_ID"):
            raw = payload.get(key)
            if isinstance(raw, int) and raw > 0:
                reply_id = raw
                break
            if isinstance(raw, str) and raw.strip().isdigit() and int(raw.strip()) > 0:
                reply_id = int(raw.strip())
                break
        # Fallback: check inside params (webhook / imbot event payload)
        if reply_id is None and isinstance(params, dict):
            for key in ("REPLY_ID", "replyId", "reply_id"):
                raw = params.get(key)
                if isinstance(raw, int) and raw > 0:
                    reply_id = raw
                    break
                if isinstance(raw, str) and raw.strip().isdigit() and int(raw.strip()) > 0:
                    reply_id = int(raw.strip())
                    break
        if reply_id is not None:
            _logger.debug("Bitrix message %s has reply_id=%s", raw_message_id, reply_id)
        elif _logger.isEnabledFor(logging.DEBUG):
            _logger.debug(
                "Bitrix message %s payload keys=%s params=%s",
                raw_message_id,
                list(payload.keys()),
                params if isinstance(params, dict) else repr(params),
            )

        return BitrixMessage(
            message_id=raw_message_id,
            author_id=author_id,
            text=str(payload.get("text") or ""),
            file_ids=tuple(file_ids),
            update_time_unix=_extract_unix_timestamp(
                payload.get("date_update")
                or payload.get("dateUpdate")
                or payload.get("date_modified")
                or payload.get("dateModified")
            ),
            like_user_ids=tuple(sorted(like_user_ids)),
            reply_id=reply_id,
            is_sticker=is_sticker,
            is_meeting=is_meeting,
            is_task=is_task,
        )


@dataclass(frozen=True)
class BitrixFile:
    file_id: int
    name: str
    url_download: Optional[str]
    mime_type: Optional[str]
    file_type: str
    is_image: bool
    author_id: Optional[int] = None

    @staticmethod
    def from_api_payload(payload: dict[str, Any]) -> Optional["BitrixFile"]:
        raw_file_id = payload.get("id") or payload.get("ID")
        if isinstance(raw_file_id, str) and raw_file_id.strip().isdigit():
            raw_file_id = int(raw_file_id.strip())
        if not isinstance(raw_file_id, int):
            return None

        name = str(payload.get("name") or payload.get("NAME") or payload.get("original_name") or f"file_{raw_file_id}").strip()
        # file_type is the Bitrix category: "image", "video", "audio", or "file"
        file_type = str(payload.get("type") or payload.get("TYPE") or "file").strip().lower()
        # mime_type is the actual MIME type when available (e.g. "image/png"); may coincide with file_type
        mime_type_raw = payload.get("mime_type") or payload.get("MIME_TYPE")
        mime_type = str(mime_type_raw).strip() if mime_type_raw else None
        url_download_raw = (
            payload.get("urlDownload")
            or payload.get("url_download")
            or payload.get("downloadUrl")
            or payload.get("DOWNLOAD_URL")
            or payload.get("urlShow")
            or payload.get("urlPreview")
        )
        url_download = str(url_download_raw).strip() if url_download_raw else None
        raw_author_id = payload.get("authorId") or payload.get("author_id") or payload.get("AUTHOR_ID")
        author_id: Optional[int] = None
        if isinstance(raw_author_id, int):
            author_id = raw_author_id
        elif isinstance(raw_author_id, str) and raw_author_id.strip().isdigit():
            author_id = int(raw_author_id.strip())
        lower_name = name.lower()
        is_image = (
            file_type == "image"
            or bool(mime_type and mime_type.startswith("image/"))
            or lower_name.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"))
        )
        return BitrixFile(
            file_id=raw_file_id,
            name=name,
            url_download=url_download,
            mime_type=mime_type,
            file_type=file_type,
            is_image=is_image,
            author_id=author_id,
        )


@dataclass(frozen=True)
class CursorState:
    last_seen_bitrix_message_id: Optional[int]


class MirrorOrigin(str, Enum):
    TELEGRAM = "telegram"
    BITRIX = "bitrix"


@dataclass(frozen=True)
class MessageMirrorLink:
    telegram_chat_id: int
    telegram_message_id: int
    bitrix_message_id: int
    origin: MirrorOrigin
    telegram_message_date_unix: Optional[int]
    bitrix_author_id: Optional[int]
    last_seen_bitrix_revision: str
    created_at_unix: int
    updated_at_unix: int
    bitrix_liked_by_bot: bool
    last_seen_bitrix_likes: str
    telegram_message_thread_id: Optional[int] = None


def _extract_unix_timestamp(value: Any) -> Optional[int]:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        normalized = value.strip()
        if normalized.isdigit():
            return int(normalized)
    return None
