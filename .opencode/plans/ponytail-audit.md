# Ponytail Audit — BitrixMirroring

Date: 2026-07-01
Branch: optimization/hot-path-refactor

Findings ranked biggest cut first:

```
native  1044-line DASHBOARD_HTML string literal (HTML+JS) embedded in Python. FastAPI FileResponse("dashboard.html") / StaticFiles serves a static asset natively. [server-side/monitor_app.py:950-1993]
yagni   step_configure_ssh (~111-line SSH hardening wizard: sshd_config drop-in, authorized_keys paste, PasswordAuthentication toggle) — scope creep for a bot installer, can lock users out. Replace with nothing (document separately). [install.sh:339-450]
yagni   _ensure_chat_mappings_table duplicates the chat_mappings CREATE + legacy-id migration + topic_ids ADD COLUMN already owned by mirror_state_store._initialize_sync on the same DB. Drop the monitor's copy; it's a client, not the schema owner. [server-side/monitor_app.py:118-181]
yagni   step_restore_db_from_backup embeds a 2nd Python DB-restore script that is a near-verbatim copy of monitor_app._restore_db_from_backup. One restore path suffices. [install.sh:1330-1370]
delete  README sections documenting removed env-var mapping config (CHAT_MAPPING_1/CHAT_MAPPINGS/BITRIX_DIALOG_ID/ALLOWED_TELEGRAM_CHAT_ID/PREFIX_WITH_TIMESTAMP/BITRIX_USER_CACHE_TTL_SECONDS/BITRIX_CURSOR_STATE_PATH) — settings.py only loads mappings from the DB now. Replace with "mappings come from /monitor". [README.md:145-148,207-211,219-221,263-300]
native  _get_journal errors_only hand-rolled log-line state machine (ERROR/WARNING/Exception/Traceback tracking). journalctl --priority=warning -n{lines} filters by level natively. [server-side/monitor_app.py:304-318]
native  _rotate_log_if_needed reinvents size-based log rotation. logging.handlers.RotatingFileHandler(maxBytes=50MB, backupCount=1). [server-side/app.py:63-74]
delete  DEPLOYMENT stale refs to CHAT_MAPPINGS/BITRIX_DIALOG_ID/ALLOWED_TELEGRAM_CHAT_ID legacy mode and bitrix_cursor_state.json tree entry — none exist in code. [DEPLOYMENT.md:102,159,185-190,217]
shrink  download_file_by_id tries fallback_url twice (once at line 266, again in candidate_urls at 276-283). Collapse to one ordered candidate list tried once: [primary_url, fallback_url] then raise. [bitrix_client.py:263-290]
shrink  _normalize_topic_ids reimplements _parse_topic_ids (settings.py:60-78) — both dedup a list of ints. Reuse _parse_topic_ids. [server-side/monitor_app.py:619-626]
stdlib   _webhook_reply_cache manual LRU (len>1000 → sorted keys → pop 500). collections.OrderedDict.move_to_end + popitem(last=False) is the idiomatic bounded LRU. [mirror_service.py:378-382]
delete  _shorten method — defined, never called anywhere. [mirror_service.py:1314-1318]
delete  prefix_with_chat_title field + PREFIX_WITH_CHAT_TITLE parse — parsed in settings.py but never read by mirror_service/handlers; only the sibling prefix_with_sender is used. [settings.py:155,241; env.example:23; install.sh:714]
delete  BITRIX_SEND_WORKERS written to .env by install.sh and env.example but never read by any Python module (no such setting in settings.py). [install.sh:736; env.example:75; README.md:268]
delete  get_mappings_for_telegram_chat (plural) — defined, never called; only the singular get_mapping_for_telegram_chat is used. [mirror_service.py:91-92]

net: -~1380 lines movable/cut, -0 deps possible.
```

## Skipped (not over-engineering)

- `_bbcode_to_html` — no stdlib BBCode parser; 14 lines of regex is the lazy choice
- `MirrorStateStore` async-to-sync wrappers — correct no-dep way to run blocking sqlite3 in asyncio
- Per-method retry loops in `bitrix_client` — no async retry in stdlib/httpx
- `CursorState` one-field dataclass — touched widely, not worth churn
- Deps list (`httpx`, `fastapi`, `uvicorn`, `python-telegram-bot`, `python-dotenv`, `python-multipart`) — all 6 used, minimal
