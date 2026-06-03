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


# ── Import tests ──────────────────────────────────────────────────────────────

_MINIMAL_BACKUP: dict = {
    "version": "1",
    "exported_at": 1748000000,
    "env": {"TELEGRAM_BOT_TOKEN": "new_tok", "APP_DOMAIN": "new.example.com"},
    "db": {
        "chat_mappings": [],
        "cursor_state": [],
        "message_links": [],
        "topic_names": [],
        "telegram_admins": [],
        "runtime_settings": [],
    },
}


def _upload(client, payload: dict, auth=AUTH):
    content = json.dumps(payload).encode()
    return client.post(
        "/monitor/api/backup",
        auth=auth,
        files={"file": ("backup.json", io.BytesIO(content), "application/json")},
    )


def test_import_requires_auth(client):
    r = _upload(client, _MINIMAL_BACKUP, auth=None)
    assert r.status_code == 401


def test_import_invalid_json(client):
    r = client.post(
        "/monitor/api/backup",
        auth=AUTH,
        files={"file": ("backup.json", io.BytesIO(b"not json"), "application/json")},
    )
    assert r.status_code == 400


def test_import_wrong_version(client):
    bad = {**_MINIMAL_BACKUP, "version": "99"}
    r = _upload(client, bad)
    assert r.status_code == 400
    assert "версия" in r.json()["detail"].lower()


def test_import_missing_env_field(client):
    bad = {"version": "1", "db": {}}
    r = _upload(client, bad)
    assert r.status_code == 400


def test_import_missing_db_field(client):
    bad = {"version": "1", "env": {}}
    r = _upload(client, bad)
    assert r.status_code == 400


def test_import_restores_tables(client, fresh_db):
    backup = {
        **_MINIMAL_BACKUP,
        "db": {
            **_MINIMAL_BACKUP["db"],
            "chat_mappings": [
                {
                    "id": 1,
                    "tg_chat_id": -100999,
                    "bitrix_dialog_id": "chat99",
                    "label": "Imported",
                    "created_at_unix": 1748000000,
                    "topic_ids": "7,8",
                }
            ],
            "telegram_admins": [{"tg_user_id": 111222333, "added_at_unix": 1748000001}],
            "runtime_settings": [
                {"key": "forwarding_enabled", "value": "1", "updated_at_unix": 1748000002}
            ],
        },
    }
    r = _upload(client, backup)
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["tables_restored"]["chat_mappings"] == 1
    assert data["tables_restored"]["telegram_admins"] == 1
    assert data["tables_restored"]["runtime_settings"] == 1

    conn = sqlite3.connect(fresh_db)
    row = conn.execute("SELECT bitrix_dialog_id, topic_ids FROM chat_mappings").fetchone()
    assert row[0] == "chat99"
    assert row[1] == "7,8"
    admin = conn.execute("SELECT tg_user_id FROM telegram_admins").fetchone()
    assert admin[0] == 111222333
    conn.close()


def test_import_replaces_existing_rows(client, fresh_db):
    conn = sqlite3.connect(fresh_db)
    conn.execute(
        "INSERT INTO chat_mappings (tg_chat_id, bitrix_dialog_id, label, created_at_unix, topic_ids)"
        " VALUES (-100111, 'old_chat', 'Old', 1748000000, '')"
    )
    conn.commit()
    conn.close()

    backup = {
        **_MINIMAL_BACKUP,
        "db": {
            **_MINIMAL_BACKUP["db"],
            "chat_mappings": [
                {
                    "id": 1,
                    "tg_chat_id": -100999,
                    "bitrix_dialog_id": "new_chat",
                    "label": "New",
                    "created_at_unix": 1748000000,
                    "topic_ids": "",
                }
            ],
        },
    }
    r = _upload(client, backup)
    assert r.status_code == 200

    conn = sqlite3.connect(fresh_db)
    rows = conn.execute("SELECT bitrix_dialog_id FROM chat_mappings").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "new_chat"


def test_import_writes_env_file(client, fresh_env):
    r = _upload(client, _MINIMAL_BACKUP)
    assert r.status_code == 200
    assert r.json()["env_written"] is True
    text = fresh_env.read_text(encoding="utf-8")
    assert 'TELEGRAM_BOT_TOKEN="new_tok"' in text
    assert 'APP_DOMAIN="new.example.com"' in text
    assert "Restored from backup" in text


def test_import_env_written_false_on_path_error(client, fresh_db, monkeypatch):
    monkeypatch.setattr(monitor_app, "_ENV_FILE_PATH", Path("/nonexistent/dir/.env"))
    r = _upload(client, _MINIMAL_BACKUP)
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["env_written"] is False
    assert "env_error" in data
