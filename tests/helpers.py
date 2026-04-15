from __future__ import annotations

from types import SimpleNamespace

from settings import ChatMapping, Settings


def make_mapping(
    *,
    mapping_id: int = 1,
    tg_chat_id: int = -1001234567890,
    bitrix_dialog_id: str = "chat42",
    topic_ids: tuple[int, ...] = (),
    label: str = "",
) -> ChatMapping:
    return ChatMapping(
        mapping_id=mapping_id,
        tg_chat_id=tg_chat_id,
        bitrix_dialog_id=bitrix_dialog_id,
        topic_ids=topic_ids,
        label=label,
    )


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "telegram_bot_token": "token",
        "bitrix_webhook_base": "https://example.bitrix24.ru/rest/1/token",
        "bitrix_bot_id": 7,
        "bitrix_bot_client_id": "bot-token",
        "chat_mappings": (make_mapping(),),
        "prefix_with_chat_title": False,
        "prefix_with_sender": True,
        "disable_link_preview": True,
        "request_timeout_seconds": 5.0,
        "socks5_proxy_url": None,
        "bitrix_poll_interval_seconds": 5.0,
        "sync_bitrix_to_telegram": True,
        "sync_telegram_to_bitrix": True,
        "mirror_state_db_path": "test.sqlite3",
        "bitrix_retry_attempts": 2,
        "bitrix_retry_base_delay_seconds": 0.1,
        "bitrix_retry_max_delay_seconds": 0.2,
        "bitrix_poll_error_backoff_seconds": 0.1,
        "bitrix_poll_max_backoff_seconds": 0.2,
        "bitrix_max_concurrent_requests": 2,
        "bitrix_send_queue_maxsize": 10,
        "bitrix_send_workers": 1,
        "bitrix_rescan_recent_messages_limit": 20,
        "max_file_size_bytes": 1024 * 1024,
        "file_cache_dir": "",
        "file_cache_max_bytes": 10 * 1024 * 1024,
        "db_cleanup_max_age_seconds": 3600,
        "mirror_http_host": "127.0.0.1",
        "mirror_http_port": 8090,
        "bitrix_webhook_bridge_enabled": True,
        "mirror_internal_event_path": "/internal/bitrix/event",
        "mirror_internal_webhook_secret": "internal-secret",
        "telegram_webhook_enabled": True,
        "telegram_webhook_path": "/telegram/webhook",
        "telegram_webhook_public_url": "https://bot.example.com",
        "telegram_webhook_secret": "telegram-secret",
        "telegram_webhook_drop_pending_updates": False,
        "telegram_webhook_strict_verify": True,
    }
    values.update(overrides)
    return Settings(**values)


def make_message(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "chat_id": -1001234567890,
        "message_id": 100,
        "message_thread_id": None,
        "chat": SimpleNamespace(id=-1001234567890, type="supergroup", title="Team"),
        "from_user": SimpleNamespace(is_bot=False, username="alice", full_name="Alice Example"),
        "sender_chat": None,
        "reply_to_message": None,
        "forum_topic_created": None,
        "forum_topic_edited": None,
        "new_chat_members": None,
        "left_chat_member": None,
        "group_chat_created": False,
        "supergroup_chat_created": False,
        "delete_chat_photo": False,
        "pinned_message": None,
        "migrate_from_chat_id": None,
        "migrate_to_chat_id": None,
        "forum_topic_closed": False,
        "forum_topic_reopened": False,
        "sticker": None,
        "contact": None,
        "poll": None,
        "location": None,
        "venue": None,
        "voice": None,
        "video_note": None,
        "checklist": None,
        "date": None,
        "text": "hello",
        "caption": None,
        "photo": None,
        "document": None,
        "video": None,
        "audio": None,
        "animation": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)

