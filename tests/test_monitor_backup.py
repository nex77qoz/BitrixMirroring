from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "server-side"))

os.environ.setdefault("MONITOR_PASSWORD", "testpass")
os.environ.setdefault("MIRROR_STATE_DB_PATH", ":memory:")

import monitor_app
from monitor_app import app

AUTH = ("admin", "testpass")
_ALL_TABLES = (
    "chat_mappings",
    "cursor_state",
    "message_links",
    "topic_names",
    "telegram_admins",
    "runtime_settings",
)

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS chat_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_chat_id INTEGER NOT NULL,
    bitrix_dialog_id TEXT NOT NULL,
    label TEXT DEFAULT '',
    created_at_unix INTEGER NOT NULL,
    topic_ids TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS cursor_state (
    bitrix_dialog_id TEXT PRIMARY KEY,
    last_seen_bitrix_message_id INTEGER
);
CREATE TABLE IF NOT EXISTS message_links (
    telegram_chat_id INTEGER NOT NULL,
    telegram_message_id INTEGER NOT NULL,
    bitrix_message_id INTEGER NOT NULL UNIQUE,
    origin TEXT NOT NULL,
    telegram_message_date_unix INTEGER,
    bitrix_author_id INTEGER,
    last_seen_bitrix_revision TEXT NOT NULL,
    created_at_unix INTEGER NOT NULL,
    updated_at_unix INTEGER NOT NULL,
    bitrix_liked_by_bot INTEGER DEFAULT 0,
    last_seen_bitrix_likes TEXT DEFAULT '',
    telegram_message_thread_id INTEGER,
    PRIMARY KEY (telegram_chat_id, telegram_message_id)
);
CREATE TABLE IF NOT EXISTS topic_names (
    tg_chat_id INTEGER NOT NULL,
    topic_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (tg_chat_id, topic_id)
);
CREATE TABLE IF NOT EXISTS telegram_admins (
    tg_user_id INTEGER PRIMARY KEY,
    added_at_unix INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at_unix INTEGER NOT NULL
);
"""


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test.sqlite3")
    monkeypatch.setattr(monitor_app, "DB_PATH", db_file)
    conn = sqlite3.connect(db_file)
    for stmt in _CREATE_SQL.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(stmt)
    conn.commit()
    conn.close()
    return db_file


@pytest.fixture()
def fresh_env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        'TELEGRAM_BOT_TOKEN="tok123"\nMONITOR_PASSWORD="secret"\nAPP_DOMAIN="old.example.com"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(monitor_app, "_ENV_FILE_PATH", env_file)
    return env_file


@pytest.fixture()
def client(fresh_db, fresh_env, monkeypatch):
    monkeypatch.setattr(monitor_app, "MONITOR_PASSWORD", "testpass")
    monkeypatch.setattr(monitor_app, "MONITOR_USERNAME", "admin")
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ── Export tests ──────────────────────────────────────────────────────────────

def test_export_requires_auth(client):
    r = client.get("/monitor/api/backup")
    assert r.status_code == 401


def test_export_response_structure(client):
    r = client.get("/monitor/api/backup", auth=AUTH)
    assert r.status_code == 200
    assert "attachment" in r.headers.get("content-disposition", "")
    data = r.json()
    assert data["version"] == "1"
    assert isinstance(data["exported_at"], int)
    assert isinstance(data["env"], dict)
    assert isinstance(data["db"], dict)
    for table in _ALL_TABLES:
        assert table in data["db"], f"missing table: {table}"


def test_export_reads_env_file(client, fresh_env):
    r = client.get("/monitor/api/backup", auth=AUTH)
    data = r.json()
    assert data["env"]["TELEGRAM_BOT_TOKEN"] == "tok123"
    assert data["env"]["APP_DOMAIN"] == "old.example.com"


def test_export_includes_db_rows(client, fresh_db):
    conn = sqlite3.connect(fresh_db)
    conn.execute(
        "INSERT INTO chat_mappings (tg_chat_id, bitrix_dialog_id, label, created_at_unix, topic_ids)"
        " VALUES (-100123, 'chat9', 'MyChat', 1748000000, '12,34')"
    )
    conn.execute(
        "INSERT INTO telegram_admins (tg_user_id, added_at_unix) VALUES (999888, 1748000001)"
    )
    conn.commit()
    conn.close()

    r = client.get("/monitor/api/backup", auth=AUTH)
    data = r.json()
    assert len(data["db"]["chat_mappings"]) == 1
    assert data["db"]["chat_mappings"][0]["bitrix_dialog_id"] == "chat9"
    assert data["db"]["chat_mappings"][0]["topic_ids"] == "12,34"
    assert len(data["db"]["telegram_admins"]) == 1
    assert data["db"]["telegram_admins"][0]["tg_user_id"] == 999888
