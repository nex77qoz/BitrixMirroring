from __future__ import annotations

import os
import sqlite3 as _sqlite3
from dataclasses import dataclass, field
from pathlib import Path as _Path
from typing import Optional


def _read_env(name: str, default: Optional[str] = None) -> str:
    value = os.getenv(name)
    if value is None:
        if default is None:
            raise ValueError(f"{name} is required")
        value = default
    return value.strip()


def _parse_bool(name: str, default: str) -> bool:
    raw = _read_env(name, default)
    normalized = raw.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


def _parse_optional_int(name: str) -> Optional[int]:
    raw = _read_env(name, "")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _parse_float(name: str, default: str, *, minimum: float) -> float:
    raw = _read_env(name, default)
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _parse_int(name: str, default: Optional[str] = None, *, minimum: int) -> int:
    raw = _read_env(name, default)
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _parse_topic_ids(raw: str) -> frozenset[int]:
    """Parse a comma-separated string of integers into a frozenset.

    Empty string or whitespace-only input returns an empty frozenset (= all topics allowed).
    Invalid tokens are silently ignored.
    """
    result: set[int] = set()
    for token in raw.split(","):
        token = token.strip()
        if token:
            try:
                result.add(int(token))
            except ValueError:
                pass
    return frozenset(result)


@dataclass(frozen=True)
class ChatMapping:
    tg_chat_id: int
    bitrix_dialog_id: str
    topic_ids: frozenset[int] = field(default_factory=frozenset)


def _load_db_chat_mappings(db_path: str) -> tuple[ChatMapping, ...]:
    """Load additional chat mappings from the database chat_mappings table.

    Returns an empty tuple if the file does not exist, the table has not been
    created yet, or any other error occurs — all gracefully ignored so the
    service still starts even without the monitoring app having run first.
    """
    path = _Path(db_path) if _Path(db_path).is_absolute() else _Path.cwd() / db_path
    if not path.exists():
        return ()
    try:
        conn = _sqlite3.connect(str(path))
        try:
            rows = conn.execute(
                "SELECT tg_chat_id, bitrix_dialog_id, topic_ids FROM chat_mappings"
            ).fetchall()
            return tuple(
                ChatMapping(
                    tg_chat_id=int(row[0]),
                    bitrix_dialog_id=str(row[1]),
                    topic_ids=_parse_topic_ids(str(row[2]) if row[2] else ""),
                )
                for row in rows
            )
        except _sqlite3.OperationalError:
            return ()  # table doesn't exist yet
        finally:
            conn.close()
    except Exception:
        return ()


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    bitrix_webhook_base: str
    bitrix_bot_id: int
    bitrix_bot_client_id: str
    chat_mappings: tuple[ChatMapping, ...]
    prefix_with_chat_title: bool
    prefix_with_sender: bool
    disable_link_preview: bool
    request_timeout_seconds: float
    socks5_proxy_url: Optional[str]
    bitrix_poll_interval_seconds: float
    sync_bitrix_to_telegram: bool
    sync_telegram_to_bitrix: bool
    mirror_state_db_path: str
    bitrix_retry_attempts: int
    bitrix_retry_base_delay_seconds: float
    bitrix_retry_max_delay_seconds: float
    bitrix_poll_error_backoff_seconds: float
    bitrix_poll_max_backoff_seconds: float
    bitrix_max_concurrent_requests: int
    bitrix_send_queue_maxsize: int
    bitrix_send_workers: int
    bitrix_rescan_recent_messages_limit: int
    max_file_size_bytes: int
    file_cache_dir: str
    file_cache_max_bytes: int
    db_cleanup_max_age_seconds: int
    mirror_http_host: str
    mirror_http_port: int
    bitrix_webhook_bridge_enabled: bool
    mirror_internal_event_path: str
    mirror_internal_webhook_secret: Optional[str]
    telegram_webhook_enabled: bool
    telegram_webhook_path: str
    telegram_webhook_public_url: Optional[str]
    telegram_webhook_secret: Optional[str]
    telegram_webhook_drop_pending_updates: bool
    telegram_webhook_strict_verify: bool

    @staticmethod
    def from_env() -> "Settings":
        telegram_bot_token = _read_env("TELEGRAM_BOT_TOKEN")
        bitrix_webhook_base = _read_env("BITRIX_WEBHOOK_BASE").rstrip("/")
        if not bitrix_webhook_base.startswith(("http://", "https://")):
            raise ValueError("BITRIX_WEBHOOK_BASE must start with http:// or https://")

        bitrix_bot_id = _parse_int("BITRIX_BOT_ID", minimum=1)
        bitrix_bot_client_id = _read_env("BITRIX_BOT_CLIENT_ID")

        mirror_state_db_path = _read_env("MIRROR_STATE_DB_PATH", "mirror_state.sqlite3")

        chat_mappings = _load_db_chat_mappings(mirror_state_db_path)

        enable_socks5_proxy = _parse_bool("ENABLE_SOCKS5_PROXY", "false")
        socks5_proxy_url = _read_env("SOCKS5_PROXY_URL", "") or None
        if enable_socks5_proxy and not socks5_proxy_url:
            raise ValueError("SOCKS5_PROXY_URL is required when ENABLE_SOCKS5_PROXY=true")
        if not enable_socks5_proxy:
            socks5_proxy_url = None

        mirror_http_host = _read_env("MIRROR_HTTP_HOST", "127.0.0.1")
        mirror_http_port = _parse_int("MIRROR_HTTP_PORT", "8090", minimum=1)
        bitrix_webhook_bridge_enabled = _parse_bool("BITRIX_WEBHOOK_BRIDGE_ENABLED", "false")
        mirror_internal_event_path = _read_env("MIRROR_INTERNAL_EVENT_PATH", "/internal/bitrix/event")
        if not mirror_internal_event_path.startswith("/"):
            mirror_internal_event_path = f"/{mirror_internal_event_path}"
        mirror_internal_webhook_secret = _read_env("MIRROR_INTERNAL_WEBHOOK_SECRET", "") or None
        if bitrix_webhook_bridge_enabled and not mirror_internal_webhook_secret:
            raise ValueError("MIRROR_INTERNAL_WEBHOOK_SECRET is required when BITRIX_WEBHOOK_BRIDGE_ENABLED=true")

        telegram_webhook_enabled = _parse_bool("TELEGRAM_WEBHOOK_ENABLED", "false")
        telegram_webhook_path = _read_env("TELEGRAM_WEBHOOK_PATH", "/telegram/webhook")
        if not telegram_webhook_path.startswith("/"):
            telegram_webhook_path = f"/{telegram_webhook_path}"
        telegram_webhook_public_url = _read_env("TELEGRAM_WEBHOOK_PUBLIC_URL", "") or None
        telegram_webhook_secret = _read_env("TELEGRAM_WEBHOOK_SECRET", "") or None
        telegram_webhook_drop_pending_updates = _parse_bool("TELEGRAM_WEBHOOK_DROP_PENDING_UPDATES", "false")
        telegram_webhook_strict_verify = _parse_bool("TELEGRAM_WEBHOOK_STRICT_VERIFY", "true")
        if telegram_webhook_enabled:
            if not telegram_webhook_public_url:
                raise ValueError("TELEGRAM_WEBHOOK_PUBLIC_URL is required when TELEGRAM_WEBHOOK_ENABLED=true")
            if not telegram_webhook_public_url.startswith(("http://", "https://")):
                raise ValueError("TELEGRAM_WEBHOOK_PUBLIC_URL must start with http:// or https://")
            if not telegram_webhook_secret:
                raise ValueError("TELEGRAM_WEBHOOK_SECRET is required when TELEGRAM_WEBHOOK_ENABLED=true")

        return Settings(
            telegram_bot_token=telegram_bot_token,
            bitrix_webhook_base=bitrix_webhook_base,
            bitrix_bot_id=bitrix_bot_id,
            bitrix_bot_client_id=bitrix_bot_client_id,
            chat_mappings=chat_mappings,
            prefix_with_chat_title=_parse_bool("PREFIX_WITH_CHAT_TITLE", "false"),
            prefix_with_sender=_parse_bool("PREFIX_WITH_SENDER", "true"),
            disable_link_preview=_parse_bool("BITRIX_DISABLE_LINK_PREVIEW", "true"),
            request_timeout_seconds=_parse_float("REQUEST_TIMEOUT_SECONDS", "20", minimum=0.1),
            socks5_proxy_url=socks5_proxy_url,
            bitrix_poll_interval_seconds=_parse_float("BITRIX_POLL_INTERVAL_SECONDS", "5", minimum=0.1),
            sync_bitrix_to_telegram=_parse_bool("SYNC_BITRIX_TO_TELEGRAM", "true"),
            sync_telegram_to_bitrix=_parse_bool("SYNC_TELEGRAM_TO_BITRIX", "true"),
            mirror_state_db_path=mirror_state_db_path,
            bitrix_retry_attempts=_parse_int("BITRIX_RETRY_ATTEMPTS", "4", minimum=1),
            bitrix_retry_base_delay_seconds=_parse_float("BITRIX_RETRY_BASE_DELAY_SECONDS", "1", minimum=0.1),
            bitrix_retry_max_delay_seconds=_parse_float("BITRIX_RETRY_MAX_DELAY_SECONDS", "15", minimum=0.1),
            bitrix_poll_error_backoff_seconds=_parse_float("BITRIX_POLL_ERROR_BACKOFF_SECONDS", "2", minimum=0.1),
            bitrix_poll_max_backoff_seconds=_parse_float("BITRIX_POLL_MAX_BACKOFF_SECONDS", "30", minimum=0.1),
            bitrix_max_concurrent_requests=_parse_int("BITRIX_MAX_CONCURRENT_REQUESTS", "5", minimum=1),
            bitrix_send_queue_maxsize=_parse_int("BITRIX_SEND_QUEUE_MAXSIZE", "1000", minimum=1),
            bitrix_send_workers=_parse_int("BITRIX_SEND_WORKERS", "2", minimum=1),
            bitrix_rescan_recent_messages_limit=_parse_int("BITRIX_RESCAN_RECENT_MESSAGES_LIMIT", "100", minimum=1),
            max_file_size_bytes=_parse_int("MAX_FILE_SIZE_BYTES", str(100 * 1024 * 1024), minimum=1),
            file_cache_dir=_read_env("FILE_CACHE_DIR", ""),
            file_cache_max_bytes=_parse_int("FILE_CACHE_MAX_BYTES", str(10 * 1024 * 1024 * 1024), minimum=0),
            db_cleanup_max_age_seconds=_parse_int("DB_CLEANUP_MAX_AGE_SECONDS", str(7 * 24 * 3600), minimum=3600),
            mirror_http_host=mirror_http_host,
            mirror_http_port=mirror_http_port,
            bitrix_webhook_bridge_enabled=bitrix_webhook_bridge_enabled,
            mirror_internal_event_path=mirror_internal_event_path,
            mirror_internal_webhook_secret=mirror_internal_webhook_secret,
            telegram_webhook_enabled=telegram_webhook_enabled,
            telegram_webhook_path=telegram_webhook_path,
            telegram_webhook_public_url=telegram_webhook_public_url,
            telegram_webhook_secret=telegram_webhook_secret,
            telegram_webhook_drop_pending_updates=telegram_webhook_drop_pending_updates,
            telegram_webhook_strict_verify=telegram_webhook_strict_verify,
        )

