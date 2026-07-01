from __future__ import annotations

from pathlib import Path

import pytest

_ENV_KEYS = (
    "MONITOR_PASSWORD",
    "MIRROR_STATE_DB_PATH",
    "BITRIX_LOG_PATH",
    "TELEGRAM_BOT_TOKEN",
    "BITRIX_WEBHOOK_BASE",
    "BITRIX_BOT_CLIENT_ID",
    "TELEGRAM_WEBHOOK_SECRET",
    "MIRROR_INTERNAL_WEBHOOK_SECRET",
    "MONITOR_USERNAME",
    "BITRIX_FORWARDED_EVENTS",
    "BITRIX_BOT_ID",
    "BITRIX_WEBHOOK_BRIDGE_ENABLED",
    "MIRROR_INTERNAL_BASE_URL",
    "MIRROR_INTERNAL_EVENT_PATH",
    "MIRROR_INTERNAL_TIMEOUT_SECONDS",
)


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate test environment from host env vars that affect module-level imports."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> str:
    return str(tmp_path / "test_state.sqlite3")
