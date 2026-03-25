"""
Bitrix-Telegram Bot — Monitoring Dashboard
==========================================
Admin web UI for real-time service monitoring, log viewing,
and chat mapping management.

Run:
    uvicorn monitor_app:app --host 127.0.0.1 --port 8082

Environment variables (shared with the main .env):
    MIRROR_STATE_DB_PATH  — path to mirror_state.sqlite3
    MONITOR_USERNAME      — HTTP Basic Auth username  (default: admin)
    MONITOR_PASSWORD      — HTTP Basic Auth password  (REQUIRED)
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
import httpx
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Load .env from the parent directory (same as main services)
_env_file = Path(__file__).resolve().parent.parent / ".env"
if _env_file.exists():
    load_dotenv(_env_file, override=False)

_DB_RAW = os.getenv("MIRROR_STATE_DB_PATH", "mirror_state.sqlite3")
# Resolve relative paths against the main bot directory (parent of server-side/)
DB_PATH = (
    _DB_RAW
    if os.path.isabs(_DB_RAW)
    else str(Path(__file__).resolve().parent.parent / _DB_RAW)
)

MONITOR_USERNAME = os.getenv("MONITOR_USERNAME", "admin")
MONITOR_PASSWORD = os.getenv("MONITOR_PASSWORD", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_WEBHOOK_ENABLED = os.getenv("TELEGRAM_WEBHOOK_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
TELEGRAM_WEBHOOK_PUBLIC_URL = os.getenv("TELEGRAM_WEBHOOK_PUBLIC_URL", "").strip().rstrip("/")
TELEGRAM_WEBHOOK_PATH = os.getenv("TELEGRAM_WEBHOOK_PATH", "/telegram/webhook").strip() or "/telegram/webhook"
BITRIX_WEBHOOK_BRIDGE_ENABLED = os.getenv("BITRIX_WEBHOOK_BRIDGE_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
MIRROR_INTERNAL_BASE_URL = os.getenv("MIRROR_INTERNAL_BASE_URL", "").strip().rstrip("/")
MIRROR_INTERNAL_EVENT_PATH = os.getenv("MIRROR_INTERNAL_EVENT_PATH", "/internal/bitrix/event").strip() or "/internal/bitrix/event"
MIRROR_HTTP_HOST = os.getenv("MIRROR_HTTP_HOST", "127.0.0.1").strip() or "127.0.0.1"
MIRROR_HTTP_PORT = int(os.getenv("MIRROR_HTTP_PORT", "8090"))

# Mapping from short key to systemd service name
SERVICES: dict[str, str] = {
    "mirror": "bitrix-telegram-mirror",
    "webhook": "bitrix-bot",
}

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

security = HTTPBasic()


def _check_auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    if not MONITOR_PASSWORD:
        raise HTTPException(
            status_code=503,
            detail="MONITOR_PASSWORD не задан. Добавьте его в файл .env.",
        )
    ok = secrets.compare_digest(
        credentials.username.encode(), MONITOR_USERNAME.encode()
    ) and secrets.compare_digest(
        credentials.password.encode(), MONITOR_PASSWORD.encode()
    )
    if not ok:
        raise HTTPException(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Bitrix Bot Monitor"'},
        )
    return credentials.username


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def _db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_chat_mappings_table() -> None:
    conn = _db_connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_mappings (
                tg_chat_id       INTEGER PRIMARY KEY,
                bitrix_dialog_id TEXT NOT NULL,
                label            TEXT DEFAULT '',
                created_at_unix  INTEGER NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _get_db_stats() -> dict:
    try:
        conn = _db_connect()
        try:
            total = conn.execute("SELECT COUNT(*) AS c FROM message_links").fetchone()["c"]
            per_chat = conn.execute(
                """
                SELECT telegram_chat_id,
                       COUNT(*)           AS count,
                       MAX(updated_at_unix) AS last_activity
                FROM message_links
                GROUP BY telegram_chat_id
                ORDER BY last_activity DESC
                """
            ).fetchall()
            cursors = conn.execute(
                "SELECT bitrix_dialog_id, last_seen_bitrix_message_id FROM cursor_state"
            ).fetchall()
            db_mappings = conn.execute(
                "SELECT tg_chat_id, bitrix_dialog_id, label, created_at_unix "
                "FROM chat_mappings ORDER BY created_at_unix"
            ).fetchall()
        finally:
            conn.close()
        return {
            "total_links": total,
            "per_chat": [dict(r) for r in per_chat],
            "cursors": [dict(r) for r in cursors],
            "db_mappings": [dict(r) for r in db_mappings],
        }
    except Exception as exc:
        return {
            "error": str(exc),
            "total_links": 0,
            "per_chat": [],
            "cursors": [],
            "db_mappings": [],
        }


# ---------------------------------------------------------------------------
# Systemd helpers
# ---------------------------------------------------------------------------


def _get_service_info(service: str) -> dict:
    try:
        r = subprocess.run(
            [
                "systemctl",
                "show",
                service,
                "--property=ActiveState,SubState,LoadState,"
                "ActiveEnterTimestamp,ExecMainPID,NRestarts",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        props: dict[str, str] = {}
        for line in r.stdout.strip().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                props[k] = v
        return {
            "service": service,
            "active_state": props.get("ActiveState", "unknown"),
            "sub_state": props.get("SubState", ""),
            "load_state": props.get("LoadState", ""),
            "since": props.get("ActiveEnterTimestamp", ""),
            "pid": props.get("ExecMainPID", "0"),
            "restarts": props.get("NRestarts", "0"),
        }
    except FileNotFoundError:
        return {
            "service": service,
            "active_state": "unavailable",
            "sub_state": "systemctl not found",
            "load_state": "",
            "since": "",
            "pid": "0",
            "restarts": "0",
        }
    except Exception as exc:
        return {
            "service": service,
            "active_state": "error",
            "sub_state": str(exc),
            "load_state": "",
            "since": "",
            "pid": "0",
            "restarts": "0",
        }


def _get_journal(service: str, lines: int = 50) -> list[str]:
    try:
        r = subprocess.run(
            [
                "journalctl",
                "-u",
                service,
                f"-n{lines}",
                "--no-pager",
                "--output=short-iso",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        result = r.stdout.strip().splitlines()
        return result if result else ["(no log entries)"]
    except Exception as exc:
        return [f"Ошибка чтения journalctl: {exc}"]


def _get_telegram_webhook_status() -> dict:
    expected_url = f"{TELEGRAM_WEBHOOK_PUBLIC_URL}{TELEGRAM_WEBHOOK_PATH}" if TELEGRAM_WEBHOOK_PUBLIC_URL else ""
    status = {
        "enabled": TELEGRAM_WEBHOOK_ENABLED,
        "expected_url": expected_url,
        "configured_path": TELEGRAM_WEBHOOK_PATH,
        "internal_health_url": f"http://{MIRROR_HTTP_HOST}:{MIRROR_HTTP_PORT}/health",
    }

    if not TELEGRAM_WEBHOOK_ENABLED:
        status["mode"] = "polling"
        return status
    if not TELEGRAM_BOT_TOKEN:
        status["mode"] = "webhook"
        status["error"] = "TELEGRAM_BOT_TOKEN not configured"
        return status

    try:
        response = httpx.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getWebhookInfo",
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            status["mode"] = "webhook"
            status["error"] = payload.get("description") or "Telegram API returned not ok"
            return status
        result = payload.get("result") or {}
        status.update(
            {
                "mode": "webhook",
                "actual_url": result.get("url", ""),
                "pending_update_count": result.get("pending_update_count", 0),
                "last_error_date": result.get("last_error_date"),
                "last_error_message": result.get("last_error_message", ""),
                "max_connections": result.get("max_connections"),
                "ip_address": result.get("ip_address", ""),
                "has_custom_certificate": bool(result.get("has_custom_certificate")),
                "verified": result.get("url", "") == expected_url,
            }
        )
        return status
    except Exception as exc:
        status["mode"] = "webhook"
        status["error"] = str(exc)
        return status


def _get_bitrix_bridge_status() -> dict:
    health_url = f"http://{MIRROR_HTTP_HOST}:{MIRROR_HTTP_PORT}/health"
    expected_event_url = f"{MIRROR_INTERNAL_BASE_URL}{MIRROR_INTERNAL_EVENT_PATH}" if MIRROR_INTERNAL_BASE_URL else ""
    status = {
        "enabled": BITRIX_WEBHOOK_BRIDGE_ENABLED,
        "health_url": health_url,
        "expected_event_url": expected_event_url,
        "configured_base_url": MIRROR_INTERNAL_BASE_URL,
        "configured_event_path": MIRROR_INTERNAL_EVENT_PATH,
        "reachable": False,
        "main_ok": False,
        "mirror_bridge_enabled": False,
        "verified": False,
    }

    if not BITRIX_WEBHOOK_BRIDGE_ENABLED:
        status["mode"] = "disabled"
        return status

    try:
        response = httpx.get(health_url, timeout=5)
        response.raise_for_status()
        payload = response.json()
        status["reachable"] = True
        status["main_ok"] = bool(payload.get("ok"))
        status["mirror_bridge_enabled"] = bool(payload.get("bitrix_webhook_bridge_enabled"))
        status["telegram_webhook_enabled"] = bool(payload.get("telegram_webhook_enabled"))
        webhook_status = payload.get("telegram_webhook_status")
        if isinstance(webhook_status, dict):
            status["mirror_telegram_webhook_status"] = webhook_status
        status["verified"] = status["main_ok"] and status["mirror_bridge_enabled"]
        if not status["mirror_bridge_enabled"]:
            status["error"] = "Main mirror process reachable, but Bitrix bridge is disabled there"
        return status
    except Exception as exc:
        status["error"] = str(exc)
        return status


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    _ensure_chat_mappings_table()
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class MappingCreate(BaseModel):
    tg_chat_id: int
    bitrix_dialog_id: str
    label: str = ""


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------


@app.get("/monitor/health")
def health():
    return {"ok": True}


@app.get("/monitor/api/status")
def api_status(_: str = Depends(_check_auth)):
    return {
        "services": {k: _get_service_info(v) for k, v in SERVICES.items()},
        "db": _get_db_stats(),
    "bitrix_bridge": _get_bitrix_bridge_status(),
    "telegram_webhook": _get_telegram_webhook_status(),
        "ts": int(time.time()),
    }


@app.get("/monitor/api/mappings")
def api_get_mappings(_: str = Depends(_check_auth)):
    conn = _db_connect()
    try:
        rows = conn.execute(
            "SELECT tg_chat_id, bitrix_dialog_id, label, created_at_unix "
            "FROM chat_mappings ORDER BY created_at_unix"
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        conn.close()


@app.post("/monitor/api/mappings", status_code=201)
def api_add_mapping(body: MappingCreate, _: str = Depends(_check_auth)):
    if not body.bitrix_dialog_id.strip():
        raise HTTPException(status_code=400, detail="bitrix_dialog_id не может быть пустым")
    conn = _db_connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO chat_mappings"
            "(tg_chat_id, bitrix_dialog_id, label, created_at_unix) VALUES (?,?,?,?)",
            (
                body.tg_chat_id,
                body.bitrix_dialog_id.strip(),
                body.label.strip(),
                int(time.time()),
            ),
        )
        conn.commit()
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        conn.close()


@app.delete("/monitor/api/mappings/{tg_chat_id}")
def api_delete_mapping(tg_chat_id: int, _: str = Depends(_check_auth)):
    conn = _db_connect()
    try:
        conn.execute("DELETE FROM chat_mappings WHERE tg_chat_id = ?", (tg_chat_id,))
        conn.commit()
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        conn.close()


@app.post("/monitor/api/services/{service_key}/restart")
def api_restart(service_key: str, _: str = Depends(_check_auth)):
    if service_key not in SERVICES:
        raise HTTPException(status_code=404, detail="Неизвестный ключ сервиса")
    name = SERVICES[service_key]
    try:
        r = subprocess.run(
            ["sudo", "-n", "systemctl", "restart", name],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode != 0:
            raise HTTPException(
                status_code=500, detail=r.stderr.strip() or "Не удалось перезапустить сервис"
            )
        return {"ok": True, "service": name}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail="systemctl недоступен") from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="Время ожидания перезапуска истекло") from exc


@app.get("/monitor/api/journal/{service_key}")
def api_journal(
    service_key: str, lines: int = 60, _: str = Depends(_check_auth)
):
    if service_key not in SERVICES:
        raise HTTPException(status_code=404, detail="Неизвестный ключ сервиса")
    return {"lines": _get_journal(SERVICES[service_key], min(lines, 300))}


# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Монитор Bitrix Bot</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    [x-cloak] { display: none !important; }
    .log-pre { white-space: pre-wrap; word-break: break-all; }
    details > summary { list-style: none; }
    details > summary::-webkit-details-marker { display: none; }
    .details-arrow { display: inline-block; transition: transform 0.2s; }
    details[open] .details-arrow { transform: rotate(90deg); }
    .dot-pulse::after {
      content: '';
      display: inline-block;
      width: 6px; height: 6px;
      border-radius: 50%;
      background: currentColor;
      animation: pulse 1.5s infinite;
      margin-left: 4px;
      vertical-align: middle;
    }
    @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
  </style>
</head>
<body class="bg-gray-100 min-h-screen font-sans antialiased">

<!-- ═══ LOGIN OVERLAY ═══════════════════════════════════════════════════════ -->
<div id="loginOverlay"
     class="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/80 backdrop-blur-sm">
  <div class="w-full max-w-sm bg-white rounded-2xl shadow-2xl p-8 mx-4">
    <div class="text-center mb-6">
      <div class="text-4xl mb-2">🤖</div>
      <h1 class="text-xl font-bold text-gray-800">Монитор Bitrix Bot</h1>
      <p class="text-sm text-gray-500 mt-1">Войдите, чтобы продолжить</p>
    </div>
    <div id="loginError"
         class="hidden mb-4 p-3 bg-red-50 border border-red-200 text-red-700 rounded-lg text-sm"></div>
    <div class="space-y-3">
      <input id="loginUser" type="text" value="admin"
             class="w-full px-4 py-2.5 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
             placeholder="Имя пользователя">
      <input id="loginPass" type="password"
             class="w-full px-4 py-2.5 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
             placeholder="Пароль">
      <button onclick="doLogin()"
              class="w-full py-2.5 bg-blue-600 hover:bg-blue-700 text-white font-semibold rounded-lg text-sm transition-colors">
        Войти
      </button>
    </div>
  </div>
</div>

<!-- ═══ DASHBOARD ═══════════════════════════════════════════════════════════ -->
<div id="app" class="hidden min-h-screen">

  <!-- Header -->
  <header class="bg-slate-800 text-white shadow-lg sticky top-0 z-40">
    <div class="max-w-6xl mx-auto px-4 py-3 flex items-center justify-between">
      <div class="flex items-center gap-3">
        <span class="text-2xl">🤖</span>
        <div>
          <h1 class="font-bold text-lg leading-none">Монитор Bitrix Bot</h1>
          <p id="lastUpdated" class="text-xs text-slate-400 dot-pulse">Подключение…</p>
        </div>
      </div>
      <div class="flex items-center gap-2">
        <button onclick="loadStatus(); loadMappings()"
                class="px-3 py-1.5 bg-blue-600 hover:bg-blue-700 text-white text-xs font-semibold rounded-lg transition-colors">
          ↻ Обновить
        </button>
        <button onclick="doLogout()"
                class="px-3 py-1.5 bg-slate-700 hover:bg-slate-600 text-slate-300 hover:text-white text-xs rounded-lg transition-colors">
          Выйти
        </button>
      </div>
    </div>
  </header>

  <main class="max-w-6xl mx-auto px-4 py-6 space-y-6">

    <!-- ── Services ─────────────────────────────────────────────────────── -->
    <section>
      <h2 class="text-sm font-semibold text-gray-500 uppercase tracking-wider mb-3">Сервисы</h2>
      <div id="servicesGrid" class="grid grid-cols-1 md:grid-cols-2 gap-4">
        <!-- Injected by JS -->
        <div class="bg-white rounded-xl shadow p-5 h-40 animate-pulse"></div>
        <div class="bg-white rounded-xl shadow p-5 h-40 animate-pulse"></div>
      </div>
    </section>

    <details>
      <summary class="cursor-pointer select-none mb-3">
        <span class="inline-flex items-center gap-2 text-sm font-semibold text-gray-500 uppercase tracking-wider hover:text-gray-700 transition-colors">
          <span class="details-arrow">▶</span> Статус
        </span>
      </summary>
      <div class="space-y-6 mt-3">

    <section>
      <h2 class="text-sm font-semibold text-gray-500 uppercase tracking-wider mb-3">Telegram Webhook</h2>
      <div id="telegramWebhookCard" class="bg-white rounded-xl shadow p-5">
        <div class="text-sm text-gray-400">Загрузка…</div>
      </div>
    </section>

    <section>
      <h2 class="text-sm font-semibold text-gray-500 uppercase tracking-wider mb-3">Bitrix Bridge</h2>
      <div id="bitrixBridgeCard" class="bg-white rounded-xl shadow p-5">
        <div class="text-sm text-gray-400">Загрузка…</div>
      </div>
    </section>

    <!-- ── Database Stats ────────────────────────────────────────────────── -->
    <section>
      <h2 class="text-sm font-semibold text-gray-500 uppercase tracking-wider mb-3">База данных</h2>
      <div class="grid grid-cols-1 md:grid-cols-3 gap-4">

        <!-- Total messages card -->
        <div class="bg-white rounded-xl shadow p-5 flex items-center gap-4">
          <div class="w-12 h-12 bg-blue-100 rounded-xl flex items-center justify-center text-2xl">💬</div>
          <div>
            <p class="text-xs text-gray-500">Всего связей сообщений</p>
            <p id="totalLinks" class="text-2xl font-bold text-gray-800">—</p>
          </div>
        </div>

      </div>

      <!-- Per-chat table -->
      <div class="mt-4 bg-white rounded-xl shadow overflow-hidden">
        <div class="px-5 py-3 border-b border-gray-100 flex items-center justify-between">
          <span class="font-medium text-gray-700 text-sm">Активность по чатам</span>
        </div>
        <table class="min-w-full text-sm">
          <thead class="bg-gray-50 text-xs text-gray-500 uppercase tracking-wider">
            <tr>
              <th class="px-5 py-2.5 text-left font-medium" >TG Chat ID</th>
              <th class="px-5 py-2.5 text-right font-medium">Сообщений</th>
              <th class="px-5 py-2.5 text-left font-medium">Последняя активность</th>
            </tr>
          </thead>
          <tbody id="chatStatsBody" class="divide-y divide-gray-50">
            <tr><td colspan="3" class="px-5 py-4 text-center text-gray-400">Загрузка…</td></tr>
          </tbody>
        </table>
      </div>

    </section>

      </div><!-- /Статус -->
    </details>

    <!-- ── Chat Mappings ─────────────────────────────────────────────────── -->
    <section>
      <h2 class="text-sm font-semibold text-gray-500 uppercase tracking-wider mb-3">Связки чатов</h2>

      <!-- Warning banner -->
      <div class="mb-4 flex items-start gap-3 p-4 bg-amber-50 border border-amber-200 rounded-xl text-sm text-amber-800">
        <span class="text-lg mt-0.5">⚠️</span>
        <div>
          <strong>Перезапустите сервис Mirror</strong> после добавления или удаления связок в базе данных,
          чтобы изменения вступили в силу.
        </div>
      </div>

      <!-- DB mappings (managed here) -->
      <div class="bg-white rounded-xl shadow overflow-hidden">
        <div class="px-5 py-3 border-b border-gray-100 flex items-center gap-2">
          <span class="font-medium text-gray-700 text-sm">Из базы данных</span>
          <span class="text-xs text-blue-700 bg-blue-100 px-2 py-0.5 rounded-full">можно редактировать</span>
        </div>
        <table class="min-w-full text-sm">
          <thead class="bg-gray-50 text-xs text-gray-500 uppercase tracking-wider">
            <tr>
              <th class="px-5 py-2.5 text-left font-medium" >TG Chat ID</th>
              <th class="px-5 py-2.5 text-left font-medium">Bitrix Dialog ID</th>
              <th class="px-5 py-2.5 text-left font-medium">Метка</th>
              <th class="px-5 py-2.5"></th>
            </tr>
          </thead>
          <tbody id="dbMappingsBody" class="divide-y divide-gray-50">
            <tr><td colspan="4" class="px-5 py-4 text-center text-gray-400">Загрузка…</td></tr>
          </tbody>
        </table>

        <!-- Add mapping form -->
        <div class="px-5 py-4 bg-gray-50 border-t border-gray-100">
          <p class="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3">Добавить связку</p>
          <div class="flex flex-wrap gap-2 items-end">
            <div class="flex flex-col gap-1">
              <label class="text-xs text-gray-500">TG Chat ID</label>
              <input id="newTgChatId" type="number"
                     class="w-44 px-3 py-2 border border-gray-300 rounded-lg text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-500"
                     placeholder="-1001234567890">
            </div>
            <div class="flex flex-col gap-1">
              <label class="text-xs text-gray-500">Bitrix Dialog ID</label>
              <input id="newBitrixDialogId" type="text"
                     class="w-40 px-3 py-2 border border-gray-300 rounded-lg text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-500"
                     placeholder="chat2941">
            </div>
            <div class="flex flex-col gap-1">
              <label class="text-xs text-gray-500">Метка (необязательно)</label>
              <input id="newLabel" type="text"
                     class="w-36 px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                     placeholder="Канал команды A">
            </div>
            <button id="addMappingBtn" onclick="addMapping()"
                    class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white text-sm font-semibold rounded-lg transition-colors disabled:opacity-50">
              Добавить
            </button>
          </div>
          <p id="addMappingMsg" class="hidden mt-2 text-xs"></p>
        </div>
      </div>
    </section>

  </main>

  <footer class="max-w-6xl mx-auto px-4 py-4 text-xs text-gray-400 text-center">
    Монитор Bitrix Bot
  </footer>
</div>

<!-- ═══ JAVASCRIPT ══════════════════════════════════════════════════════════ -->
<script>
'use strict';

let AUTH_HEADER = '';

// ── Auth ─────────────────────────────────────────────────────────────────────
function b64(user, pass) {
  return 'Basic ' + btoa(unescape(encodeURIComponent(user + ':' + pass)));
}

async function doLogin() {
  const user = document.getElementById('loginUser').value.trim();
  const pass = document.getElementById('loginPass').value;
  if (!user || !pass) { showLoginErr('Введите имя пользователя и пароль'); return; }

  AUTH_HEADER = b64(user, pass);
  try {
    const r = await fetch('/monitor/api/status', {
      headers: { 'Authorization': AUTH_HEADER }
    });
    if (r.ok) {
      document.getElementById('loginOverlay').classList.add('hidden');
      document.getElementById('app').classList.remove('hidden');
      startPolling();
      loadMappings();
    } else if (r.status === 401) {
      AUTH_HEADER = '';
      showLoginErr('Неверные учётные данные');
    } else {
      AUTH_HEADER = '';
      const e = await r.json().catch(() => ({}));
      showLoginErr(e.detail || ('Ошибка сервера ' + r.status));
    }
  } catch (err) {
    AUTH_HEADER = '';
    showLoginErr('Ошибка подключения: ' + err.message);
  }
}

function showLoginErr(msg) {
  const el = document.getElementById('loginError');
  el.textContent = msg;
  el.classList.remove('hidden');
}

function doLogout() {
  AUTH_HEADER = '';
  document.getElementById('app').classList.add('hidden');
  document.getElementById('loginError').classList.add('hidden');
  document.getElementById('loginPass').value = '';
  document.getElementById('loginOverlay').classList.remove('hidden');
}

// ── Generic fetch wrapper ────────────────────────────────────────────────────
async function apiFetch(url, opts = {}) {
  return fetch(url, {
    ...opts,
    headers: { 'Authorization': AUTH_HEADER, ...(opts.headers || {}) }
  });
}

// ── Polling ──────────────────────────────────────────────────────────────────
async function loadStatus() {
  try {
    const r = await apiFetch('/monitor/api/status');
    if (!r.ok) return;
    const data = await r.json();
    renderServices(data.services || {});
    renderBitrixBridge(data.bitrix_bridge || {});
    renderTelegramWebhook(data.telegram_webhook || {});
    renderStats(data.db || {});
    const t = new Date(data.ts * 1000).toLocaleTimeString();
    document.getElementById('lastUpdated').textContent = 'Обновлено: ' + t;
  } catch (_) { /* network hiccup – ignore */ }
}

function renderBitrixBridge(info) {
  const el = document.getElementById('bitrixBridgeCard');
  const enabled = !!info.enabled;
  const reachable = !!info.reachable;
  const verified = !!info.verified;
  const badge = !enabled
    ? 'bg-gray-100 text-gray-600'
    : verified
      ? 'bg-green-100 text-green-800'
      : reachable
        ? 'bg-amber-100 text-amber-800'
        : 'bg-red-100 text-red-800';
  const badgeLabel = !enabled
    ? 'Disabled'
    : verified
      ? 'Bridge OK'
      : reachable
        ? 'Config mismatch'
        : 'Unreachable';
  const error = info.error || '—';
  const healthUrl = info.health_url || '—';
  const eventUrl = info.expected_event_url || '—';
  const mainEnabled = info.mirror_bridge_enabled ? 'true' : 'false';
  const reachableText = reachable ? 'yes' : 'no';

  el.innerHTML = `
    <div class="flex items-start justify-between gap-4 mb-4">
      <div>
        <p class="text-xs text-gray-500 uppercase tracking-wide font-medium">Mode</p>
        <p class="font-semibold text-gray-800 mt-0.5">${enabled ? 'bridge' : 'disabled'}</p>
      </div>
      <span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium ${badge}">${escHtml(badgeLabel)}</span>
    </div>
    <dl class="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-3 text-sm text-gray-600">
      <div>
        <dt class="font-medium text-gray-500 text-xs uppercase tracking-wide mb-1">Main health URL</dt>
        <dd class="font-mono break-all text-xs">${escHtml(healthUrl)}</dd>
      </div>
      <div>
        <dt class="font-medium text-gray-500 text-xs uppercase tracking-wide mb-1">Forward target</dt>
        <dd class="font-mono break-all text-xs">${escHtml(eventUrl)}</dd>
      </div>
      <div>
        <dt class="font-medium text-gray-500 text-xs uppercase tracking-wide mb-1">Reachable</dt>
        <dd>${escHtml(reachableText)}</dd>
      </div>
      <div>
        <dt class="font-medium text-gray-500 text-xs uppercase tracking-wide mb-1">Bridge enabled in main</dt>
        <dd>${escHtml(mainEnabled)}</dd>
      </div>
      <div class="md:col-span-2">
        <dt class="font-medium text-gray-500 text-xs uppercase tracking-wide mb-1">Last error</dt>
        <dd class="break-all text-xs">${escHtml(error)}</dd>
      </div>
    </dl>
  `;
}

function renderTelegramWebhook(info) {
  const el = document.getElementById('telegramWebhookCard');
  const enabled = !!info.enabled;
  const verified = info.verified === true;
  const mode = enabled ? 'webhook' : 'polling';
  const badge = !enabled
    ? 'bg-gray-100 text-gray-600'
    : verified
      ? 'bg-green-100 text-green-800'
      : 'bg-amber-100 text-amber-800';
  const badgeLabel = !enabled ? 'Polling' : verified ? 'Webhook OK' : 'Webhook mismatch';
  const actualUrl = info.actual_url || '—';
  const expectedUrl = info.expected_url || '—';
  const error = info.last_error_message || info.error || '—';
  const pending = info.pending_update_count ?? '—';
  const ip = info.ip_address || '—';

  el.innerHTML = `
    <div class="flex items-start justify-between gap-4 mb-4">
      <div>
        <p class="text-xs text-gray-500 uppercase tracking-wide font-medium">Mode</p>
        <p class="font-semibold text-gray-800 mt-0.5">${escHtml(mode)}</p>
      </div>
      <span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium ${badge}">${escHtml(badgeLabel)}</span>
    </div>
    <dl class="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-3 text-sm text-gray-600">
      <div>
        <dt class="font-medium text-gray-500 text-xs uppercase tracking-wide mb-1">Expected URL</dt>
        <dd class="font-mono break-all text-xs">${escHtml(expectedUrl)}</dd>
      </div>
      <div>
        <dt class="font-medium text-gray-500 text-xs uppercase tracking-wide mb-1">Actual URL</dt>
        <dd class="font-mono break-all text-xs">${escHtml(actualUrl)}</dd>
      </div>
      <div>
        <dt class="font-medium text-gray-500 text-xs uppercase tracking-wide mb-1">Pending updates</dt>
        <dd>${escHtml(String(pending))}</dd>
      </div>
      <div>
        <dt class="font-medium text-gray-500 text-xs uppercase tracking-wide mb-1">Telegram IP</dt>
        <dd class="font-mono text-xs">${escHtml(ip)}</dd>
      </div>
      <div class="md:col-span-2">
        <dt class="font-medium text-gray-500 text-xs uppercase tracking-wide mb-1">Last error</dt>
        <dd class="break-all text-xs">${escHtml(error)}</dd>
      </div>
    </dl>
  `;
}

function startPolling() {
  loadStatus();
}

// ── Services ─────────────────────────────────────────────────────────────────
const SERVICE_LABELS = {
  mirror:  'Сервис зеркалирования (Telegram ↔ Bitrix)',
  webhook: 'Бот вебхуков Bitrix'
};

function stateStyle(state) {
  if (state === 'active')                          return 'bg-green-100 text-green-800';
  if (state === 'failed')                          return 'bg-red-100  text-red-700';
  if (state === 'activating' || state === 'deactivating') return 'bg-yellow-100 text-yellow-700';
  if (state === 'unavailable')                     return 'bg-gray-100 text-gray-500';
  return 'bg-gray-100 text-gray-600';
}

function stateIcon(state) {
  if (state === 'active')   return '🟢';
  if (state === 'failed')   return '🔴';
  if (state === 'activating' || state === 'deactivating') return '🟡';
  return '⚪';
}

function translateServiceState(state) {
  const map = {
    active: 'активен',
    failed: 'ошибка',
    activating: 'запускается',
    deactivating: 'останавливается',
    unavailable: 'недоступен',
    unknown: 'неизвестно',
    error: 'ошибка'
  };
  return map[state] || state || 'неизвестно';
}

function translateSubState(state) {
  const map = {
    running: 'работает',
    exited: 'завершён',
    dead: 'не активен',
    start: 'запуск',
    stop: 'остановка',
    auto_restart: 'автоперезапуск',
    failed: 'ошибка',
    systemctl_not_found: 'systemctl не найден'
  };
  return map[state] || state || '';
}

function renderServices(services) {
  const grid = document.getElementById('servicesGrid');
  grid.innerHTML = '';

  for (const [key, svc] of Object.entries(services)) {
    const label    = SERVICE_LABELS[key] || svc.service;
    const badge    = stateStyle(svc.active_state);
    const icon     = stateIcon(svc.active_state);
    const status   = translateServiceState(svc.active_state) + (svc.sub_state ? ' (' + translateSubState(svc.sub_state) + ')' : '');
    const since    = svc.since ? svc.since.replace('UTC ', '') : '—';
    const pid      = svc.pid && svc.pid !== '0' ? svc.pid : '—';
    const restarts = svc.restarts || '0';

    const card = document.createElement('div');
    card.className = 'bg-white rounded-xl shadow p-5 flex flex-col gap-3';
    card.innerHTML = `
      <div class="flex items-start justify-between gap-2">
        <div>
          <p class="text-xs text-gray-500 uppercase tracking-wide font-medium">${escHtml(svc.service)}</p>
          <p class="font-semibold text-gray-800 mt-0.5">${escHtml(label)}</p>
        </div>
        <span class="shrink-0 inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium ${badge}">
          ${icon} ${escHtml(status)}
        </span>
      </div>
      <dl class="grid grid-cols-2 gap-x-4 gap-y-1 text-sm text-gray-600">
        <dt class="font-medium text-gray-500 text-xs uppercase tracking-wide">С момента запуска</dt>
        <dd class="truncate text-xs" title="${escHtml(since)}">${escHtml(since)}</dd>
        <dt class="font-medium text-gray-500 text-xs uppercase tracking-wide">PID</dt>
        <dd class="text-xs font-mono">${escHtml(pid)}</dd>
        <dt class="font-medium text-gray-500 text-xs uppercase tracking-wide">Перезапусков</dt>
        <dd class="text-xs">${escHtml(restarts)}</dd>
      </dl>
      <div class="flex gap-2 mt-auto">
        <button id="restart-${key}" onclick="restartService('${key}', '${escHtml(svc.service)}')"
                class="flex-1 px-3 py-2 bg-amber-500 hover:bg-amber-600 text-white text-sm rounded-lg font-medium transition-colors disabled:opacity-50">
          ↺ Перезапустить
        </button>
        <button id="logsBtn-${key}" onclick="toggleLogs('${key}')"
                class="flex-1 px-3 py-2 bg-slate-100 hover:bg-slate-200 text-slate-700 text-sm rounded-lg font-medium transition-colors">
          📋 Логи
        </button>
      </div>
      <div id="logs-${key}" class="hidden">
        <div class="flex items-center justify-between mb-1">
          <span class="text-xs text-gray-500">Последние 60 строк</span>
          <button onclick="refreshLogs('${key}')" class="text-xs text-blue-600 hover:underline" >Обновить</button>
        </div>
        <pre id="logsText-${key}" class="log-pre text-xs bg-slate-900 text-green-400 p-3 rounded-lg overflow-auto max-h-72 font-mono">Загрузка…</pre>
      </div>
    `;
    grid.appendChild(card);
  }
}

async function restartService(key, name) {
  if (!confirm('Перезапустить "' + name + '"?\\nСервис будет недоступен несколько секунд.')) return;
  const btn = document.getElementById('restart-' + key);
  btn.disabled = true;
  btn.textContent = 'Перезапуск…';
  try {
    const r = await apiFetch('/monitor/api/services/' + key + '/restart', { method: 'POST' });
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      alert('Ошибка: ' + (e.detail || r.status));
    } else {
      setTimeout(loadStatus, 2000);
    }
  } catch (err) {
    alert('Ошибка: ' + err.message);
  } finally {
    setTimeout(() => {
      if (btn) { btn.disabled = false; btn.innerHTML = '↺ Перезапустить'; }
    }, 4000);
  }
}

async function toggleLogs(key) {
  const box = document.getElementById('logs-' + key);
  if (box.classList.contains('hidden')) {
    box.classList.remove('hidden');
    await refreshLogs(key);
  } else {
    box.classList.add('hidden');
  }
}

async function refreshLogs(key) {
  const pre = document.getElementById('logsText-' + key);
  pre.textContent = 'Загрузка…';
  try {
    const r = await apiFetch('/monitor/api/journal/' + key + '?lines=60');
    const d = await r.json();
    pre.textContent = (d.lines || []).join('\\n') || '(empty)';
    pre.scrollTop = pre.scrollHeight;
  } catch (err) {
    pre.textContent = 'Ошибка: ' + err.message;
  }
}

// ── Stats ────────────────────────────────────────────────────────────────────
function timeAgo(unix) {
  if (!unix) return '—';
  const s = Math.floor(Date.now() / 1000) - unix;
  if (s < 60)    return s + ' сек назад';
  if (s < 3600)  return Math.floor(s / 60) + ' мин назад';
  if (s < 86400) return Math.floor(s / 3600) + ' ч назад';
  return Math.floor(s / 86400) + ' дн назад';
}

function renderStats(db) {
  document.getElementById('totalLinks').textContent =
    (db.total_links ?? 0).toLocaleString();

  const tbody = document.getElementById('chatStatsBody');
  tbody.innerHTML = '';
  const rows = db.per_chat || [];
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="3" class="px-5 py-4 text-center text-gray-400 text-sm">Пока нет данных</td></tr>';
  } else {
    for (const row of rows) {
      const tr = document.createElement('tr');
      tr.className = 'hover:bg-gray-50';
      tr.innerHTML = `
        <td class="px-5 py-2.5 text-sm font-mono text-gray-700">${escHtml(String(row.telegram_chat_id))}</td>
        <td class="px-5 py-2.5 text-sm text-gray-700 text-right font-medium">${(row.count || 0).toLocaleString()}</td>
        <td class="px-5 py-2.5 text-sm text-gray-500">${timeAgo(row.last_activity)}</td>
      `;
      tbody.appendChild(tr);
    }
  }

}

// ── Mappings ─────────────────────────────────────────────────────────────────
async function loadMappings() {
  try {
    const r = await apiFetch('/monitor/api/mappings');
    if (!r.ok) return;
    renderDbMappings(await r.json());
  } catch (_) {}
}

function renderDbMappings(mappings) {
  const tbody = document.getElementById('dbMappingsBody');
  tbody.innerHTML = '';
  if (!mappings.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="px-5 py-4 text-sm text-gray-400 text-center">В базе нет связок. Используйте форму ниже, чтобы добавить новую.</td></tr>';
    return;
  }
  for (const m of mappings) {
    const tr = document.createElement('tr');
    tr.className = 'hover:bg-gray-50';
    tr.innerHTML = `
      <td class="px-5 py-2.5 text-sm font-mono text-gray-700">${escHtml(String(m.tg_chat_id))}</td>
      <td class="px-5 py-2.5 text-sm font-mono text-gray-700">${escHtml(m.bitrix_dialog_id)}</td>
      <td class="px-5 py-2.5 text-sm text-gray-500">${escHtml(m.label || '—')}</td>
      <td class="px-5 py-2.5">
        <button onclick="deleteMapping(${m.tg_chat_id})"
                class="px-2.5 py-1 bg-red-50 hover:bg-red-100 text-red-600 text-xs font-medium rounded-lg transition-colors">
          Удалить
        </button>
      </td>
    `;
    tbody.appendChild(tr);
  }
}

async function deleteMapping(tgChatId) {
  if (!confirm('Удалить связку для TG-чата ' + tgChatId + '?\\nПосле этого нужно будет перезапустить сервис Mirror.')) return;
  const r = await apiFetch('/monitor/api/mappings/' + tgChatId, { method: 'DELETE' });
  if (r.ok) {
    loadMappings();
  } else {
    const e = await r.json().catch(() => ({}));
    alert('Ошибка: ' + (e.detail || r.status));
  }
}

async function addMapping() {
  const tgRaw    = document.getElementById('newTgChatId').value.trim();
  const dialogId = document.getElementById('newBitrixDialogId').value.trim();
  const label    = document.getElementById('newLabel').value.trim();
  const msgEl    = document.getElementById('addMappingMsg');

  msgEl.className = 'hidden mt-2 text-xs';

  if (!tgRaw || !dialogId) {
    showAddMsg('TG Chat ID и Bitrix Dialog ID обязательны', 'error');
    return;
  }
  const tgChatId = parseInt(tgRaw, 10);
  if (isNaN(tgChatId)) { showAddMsg('TG Chat ID должен быть целым числом', 'error'); return; }

  const btn = document.getElementById('addMappingBtn');
  btn.disabled = true;
  try {
    const r = await apiFetch('/monitor/api/mappings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tg_chat_id: tgChatId, bitrix_dialog_id: dialogId, label })
    });
    if (r.ok) {
      document.getElementById('newTgChatId').value = '';
      document.getElementById('newBitrixDialogId').value = '';
      document.getElementById('newLabel').value = '';
      showAddMsg('Связка добавлена. Перезапустите сервис Mirror, чтобы она заработала.', 'ok');
      loadMappings();
    } else {
      const e = await r.json().catch(() => ({}));
      showAddMsg('Ошибка: ' + (e.detail || r.status), 'error');
    }
  } catch (err) {
    showAddMsg('Ошибка: ' + err.message, 'error');
  } finally {
    btn.disabled = false;
  }
}

function showAddMsg(msg, type) {
  const el = document.getElementById('addMappingMsg');
  el.textContent = msg;
  el.className = 'mt-2 text-xs ' + (type === 'error' ? 'text-red-600' : 'text-green-700');
}

// ── Utilities ────────────────────────────────────────────────────────────────
function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ── Keyboard shortcuts ───────────────────────────────────────────────────────
document.getElementById('loginPass').addEventListener('keydown', e => {
  if (e.key === 'Enter') doLogin();
});
</script>
</body>
</html>
"""

# Inject the actual DB path into the static HTML
DASHBOARD_HTML = DASHBOARD_HTML.replace("${DB_PATH}", DB_PATH)


@app.get("/monitor", response_class=HTMLResponse)
@app.get("/monitor/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(DASHBOARD_HTML)
