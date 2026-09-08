from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).parent.parent / "server-side"))

os.environ.setdefault("MONITOR_PASSWORD", "testpass")
os.environ.setdefault("MIRROR_STATE_DB_PATH", ":memory:")

import monitor_app


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Point monitor_app at a fresh SQLite file with the mappings schema."""
    db_file = str(tmp_path / "mappings.sqlite3")
    monkeypatch.setattr(monitor_app, "DB_PATH", db_file)
    monitor_app._ensure_chat_mappings_table()
    # Default: the internal reload call succeeds (mirror reachable).
    monkeypatch.setattr(monitor_app, "_notify_mirror_mappings_reload", lambda: (True, None))
    return db_file


def _insert(dialog_id: str, tg_chat_id: int, label: str = "", topic_ids: str = "") -> None:
    conn = monitor_app._db_connect()
    conn.execute(
        "INSERT INTO chat_mappings (tg_chat_id, bitrix_dialog_id, label, created_at_unix, topic_ids)"
        " VALUES (?,?,?,?,?)",
        (tg_chat_id, dialog_id, label, 1748000000, topic_ids),
    )
    conn.commit()
    conn.close()


def _rows() -> list[tuple]:
    conn = monitor_app._db_connect()
    try:
        fetched = conn.execute(
            "SELECT tg_chat_id, bitrix_dialog_id, label, topic_ids FROM chat_mappings ORDER BY bitrix_dialog_id"
        ).fetchall()
    finally:
        conn.close()
    return [tuple(row) for row in fetched]


class _Upload:
    def __init__(self, payload: dict) -> None:
        self._data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    async def read(self) -> bytes:
        return self._data


async def _import(payload: dict, mode: str = "merge") -> dict:
    # monitor_app is imported dynamically (server-side path); to mypy its return
    # is Any, so bind through a typed local to satisfy no-any-return.
    result: dict = await monitor_app.api_import_mappings(_Upload(payload), mode, "admin")
    return result


# ── Export ───────────────────────────────────────────────────────────────────

def test_export_shape_and_topics_parsed(db):
    _insert("chat9", -100123, "MyChat", "12,34")
    response = monitor_app.api_export_mappings("admin")
    data = json.loads(response.body)
    assert data["kind"] == "bitrix-bot-mappings"
    assert data["version"] == "1"
    assert isinstance(data["exported_at"], int)
    assert "attachment" in response.headers.get("content-disposition", "")
    assert len(data["mappings"]) == 1
    m = data["mappings"][0]
    assert m["bitrix_dialog_id"] == "chat9"
    assert m["tg_chat_id"] == -100123
    assert m["label"] == "MyChat"
    # topic_ids is a real list in the file, not the stored "12,34" string
    assert m["topic_ids"] == [12, 34]


def test_export_empty(db):
    response = monitor_app.api_export_mappings("admin")
    data = json.loads(response.body)
    assert data["mappings"] == []


# ── Import: merge into an empty store ─────────────────────────────────────────

async def test_import_merge_adds_all(db):
    result = await _import({
        "kind": "bitrix-bot-mappings",
        "version": "1",
        "mappings": [
            {"tg_chat_id": -1, "bitrix_dialog_id": "chat500", "label": "L", "topic_ids": [5]},
            {"tg_chat_id": -2, "bitrix_dialog_id": "sg1", "label": "", "topic_ids": []},
        ],
    })
    assert result["added"] == 2
    assert result["updated"] == 0
    assert result["skipped"] == 0
    assert result["reloaded"] is True
    stored = {row[1]: row for row in _rows()}
    assert stored["chat500"] == (-1, "chat500", "L", "5")
    assert stored["sg1"] == (-2, "sg1", "", "")


# ── Import: merge onto an existing store ─────────────────────────────────────

async def test_import_updates_matching_dialog_same_chat(db):
    _insert("chat9", -1, "old-label", "9")
    result = await _import({
        "kind": "bitrix-bot-mappings",
        "mappings": [{"tg_chat_id": -1, "bitrix_dialog_id": "chat9", "label": "new", "topic_ids": [1, 2]}],
    })
    assert result["updated"] == 1
    assert result["added"] == 0
    rows = _rows()
    assert len(rows) == 1
    assert rows[0] == (-1, "chat9", "new", "1,2")


async def test_import_skips_dialog_bound_to_different_chat(db):
    # A dialog already tied to another TG chat must not be silently hijacked.
    _insert("chat9", -1, "mine")
    result = await _import({
        "kind": "bitrix-bot-mappings",
        "mappings": [{"tg_chat_id": -2, "bitrix_dialog_id": "chat9", "label": "attacker"}],
    })
    assert result["skipped"] == 1
    assert result["added"] == 0
    assert result["errors"] and result["errors"][0]["bitrix_dialog_id"] == "chat9"
    # Existing binding is untouched.
    assert _rows() == [(-1, "chat9", "mine", "")]


async def test_import_replace_wipes_then_inserts(db):
    _insert("chat1", -1, "gone")
    result = await _import({
        "kind": "bitrix-bot-mappings",
        "mappings": [{"tg_chat_id": -2, "bitrix_dialog_id": "chat2", "label": "kept"}],
    }, mode="replace")
    assert result["mode"] == "replace"
    assert result["added"] == 1
    assert _rows() == [(-2, "chat2", "kept", "")]


# ── Import: validation ────────────────────────────────────────────────────────

async def test_import_rejects_wrong_kind(db):
    with pytest.raises(HTTPException) as exc:
        await _import({"kind": "something-else", "mappings": []})
    assert exc.value.status_code == 400


async def test_import_rejects_bad_mode(db):
    with pytest.raises(HTTPException) as exc:
        await _import({"kind": "bitrix-bot-mappings", "mappings": []}, mode="yolo")
    assert exc.value.status_code == 400


async def test_import_skips_invalid_rows_but_keeps_valid(db):
    result = await _import({
        "kind": "bitrix-bot-mappings",
        "mappings": [
            {"tg_chat_id": -1, "bitrix_dialog_id": "chat900", "label": "ok"},
            {"tg_chat_id": -1, "bitrix_dialog_id": "not-a-dialog"},        # bad format
            {"tg_chat_id": "x", "bitrix_dialog_id": "chat7", "label": ""},  # bad tg id
            "junk",                                                         # not an object
        ],
    })
    assert result["added"] == 1
    assert result["skipped"] == 3
    assert len(result["errors"]) == 3
    assert _rows() == [(-1, "chat900", "ok", "")]


async def test_import_rejects_topic_conflict_with_existing_sibling(db):
    # chat1 already owns topic 5 for tg -1; a new dialog claiming topic 5 is skipped.
    _insert("chat1", -1, "", "5")
    result = await _import({
        "kind": "bitrix-bot-mappings",
        "mappings": [{"tg_chat_id": -1, "bitrix_dialog_id": "chat2", "label": "", "topic_ids": [5]}],
    })
    assert result["skipped"] == 1
    assert result["added"] == 0
    # only the original sibling remains
    assert [r[1] for r in _rows()] == ["chat1"]


# ── Import: reload signalling ─────────────────────────────────────────────────

async def test_import_surfaces_reload_failure(db, monkeypatch):
    monkeypatch.setattr(monitor_app, "_notify_mirror_mappings_reload", lambda: (False, "boom"))
    result = await _import({
        "kind": "bitrix-bot-mappings",
        "mappings": [{"tg_chat_id": -1, "bitrix_dialog_id": "chat9", "label": "x"}],
    })
    # Data still committed even though the live mirror could not be nudged.
    assert result["added"] == 1
    assert result["reloaded"] is False
    assert result["reload_error"] == "boom"
    assert _rows() == [(-1, "chat9", "x", "")]


async def test_import_all_invalid_skips_reload(db, monkeypatch):
    calls = {"n": 0}

    def _counting():
        calls["n"] += 1
        return (True, None)

    monkeypatch.setattr(monitor_app, "_notify_mirror_mappings_reload", _counting)
    result = await _import({"kind": "bitrix-bot-mappings", "mappings": [{"bitrix_dialog_id": "bad"}]})
    assert result["reloaded"] is False
    assert calls["n"] == 0  # nothing changed, no reason to poke the mirror


# ── Cross-VPS round trip (export -> import) ───────────────────────────────────

async def test_export_import_roundtrip_preserves_mappings(db, monkeypatch):
    _insert("chat1", -100, "Chat One", "10")
    _insert("sg2", -200, "Chat Two", "")
    exported = json.loads(monitor_app.api_export_mappings("admin").body)

    # Simulate the second VPS: an independent, empty store.
    second = str(Path(db).parent / "vps2.sqlite3")
    monkeypatch.setattr(monitor_app, "DB_PATH", second)
    monitor_app._ensure_chat_mappings_table()
    assert _rows() == []
    result = await _import(exported)
    assert result["added"] == 2
    rows = {r[1]: r for r in _rows()}
    assert rows["chat1"] == (-100, "chat1", "Chat One", "10")
    assert rows["sg2"] == (-200, "sg2", "Chat Two", "")
