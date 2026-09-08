# Optimization Plan — `optimization/hot-path-refactor`

Branch: `optimization/hot-path-refactor` (from `TgSettings`)
Date: 2026-07-01
Status: **approved by user, blocked by edit permissions**

## Blocker

Edit tool is denied by permission rules (only `.opencode/plans/*.md` writable).
User must grant write access to repo files to proceed with implementation.

## Phase 0 — Foundation (branch + CI + test infra)

- [x] Create branch `optimization/hot-path-refactor`
- [ ] `requirements-dev.txt`: pytest==8.3.4, pytest-asyncio==0.24.0, starlette==0.41.0, ruff==0.8.4, mypy==1.14.0
- [ ] `pyproject.toml`: ruff (E,F,W,I,UP,B,S,ASYNC,RUF; per-file ignores for tests/monitor_app/install.sh), mypy (py311, check_untyped_defs), pytest (asyncio_mode=auto, pythonpath=[.,server-side])
- [ ] `.github/workflows/ci.yml`: lint (ruff) + typecheck (mypy) + test (pytest) jobs
- [ ] `tests/conftest.py`: autouse fixture to isolate env vars (monkeypatch.delenv), tmp_db_path fixture
- [ ] `.gitignore`: add `test_config.env`, `graphify-out/`, `opencode.json`
- [ ] Delete `install.sh.bak`
- [ ] Remove `requests==2.32.3` from `requirements.txt`

## Phase 1 — Correctness (TDD, #1-6)

| # | File:line | Test | Fix |
|---|---|---|---|
| 5 | `mirror_state_store.py:568` | link updated recently but created old → not deleted | use `updated_at_unix` + add index |
| 6 | `mirror_service.py:680-743` | 3 failed forwards → skip + DLQ + cursor advances | skip-after-N + dead-letter |
| 4 | `mirror_service.py:625` | Bitrix send fails → message in retry queue, not dropped | retry/DLQ |
| 3 | `mirror_service.py:725-743` | TG send ok, upsert fails → cursor not advanced (re-mirror ok) | persist link before cursor |
| 2 | `mirror_service.py:1051-1063` | reaction sync fails → last_seen_bitrix_likes NOT updated | update_reaction_state in else |
| 1 | `mirror_service.py:543-548` | 3 consecutive poll errors → interval grows | apply bitrix_poll_error_backoff_seconds |

## Phase 2 — Security (#7-12)

| # | File | Fix |
|---|---|---|
| 8 | `main.py:104,153` | hmac.compare_digest + fail-closed on empty secret |
| 7 | `server-side/app.py:243` | shared-secret header on /bitrix/bot |
| 9 | `install.sh:327-330` | remove wildcard, add --no-pager, visudo -c |
| 10 | `monitor_app.py:809` | redact secrets in backup |
| 11 | `install.sh:1849,1835` | chmod 600 log + expand mask regex (CLIENT_ID, BOT_ID, CODE) |
| 12 | `server-side/*.service` | User=bitrix-bot + NoNewPrivileges, ProtectSystem, PrivateTmp, MemoryMax, StartLimitBurst |

## Phase 3 — Performance hot-path refactor (#13-23)

- #17: persistent SQLite connection (thread-local) + remove WAL re-issue + drop redundant index
- #13: eliminate double fetch (derive incremental from rescan)
- #14: batch DB lookups in single connection per sync cycle
- #16: per-dialog cursor lock + batch persist
- #22: add rate limiter (token bucket) + Retry-After
- #15: batch reply_id resolution or skip when not critical
- #19: cap get_messages_after with max pages
- #20: streaming file download + size check before load
- #21: use url_download from payload when available
- #23: move file cache cleanup to asyncio.to_thread

## Phase 4 — Tech debt (#24, #27, #30)

- #24: remove dead config bitrix_send_workers
- #27: gitignore test_config.env
- #30: real /health endpoint (DB SELECT 1, Bitrix ping, TG webhook, scheduler alive)

## Order

Phase 0 → Phase 1 (TDD) → Phase 2 → Phase 3 (#17 first) → Phase 4 → final gate
