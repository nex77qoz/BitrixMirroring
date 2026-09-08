from __future__ import annotations

from pathlib import Path

import pytest

_ENV_KEYS = (
    "MONITOR_PASSWORD",
    "MIRROR_STATE_DB_PATH",
    "BITRIX_LOG_PATH",
    "TELEGRAM_BOT_TOKEN",
    "VIBE_API_KEY",
    "VIBE_BASE_URL",
    "BITRIX_MAX_UPLOAD_FILE_BYTES",
    "TELEGRAM_WEBHOOK_SECRET",
    "MIRROR_INTERNAL_WEBHOOK_SECRET",
    "MONITOR_USERNAME",
    "BITRIX_FORWARDED_EVENTS",
    "BITRIX_BOT_ID",
)


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate test environment from host env vars that affect module-level imports."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> str:
    return str(tmp_path / "test_state.sqlite3")
