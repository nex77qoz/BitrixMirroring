# Bitrix API 2.0 Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove legacy Bitrix webhook/API paths and leave a clean `imbot.v2` supervisor-fetch integration.

**Architecture:** `bitrix_client.py` remains the sole runtime Bitrix client. `register_bot.py` handles v2 bot lifecycle. Bitrix events enter through `Event.get`; the standalone webhook service is deleted.

**Tech Stack:** Python 3.11, Bash installer, httpx, pytest, systemd, nginx.

## Global Constraints

- Keep `supervisor + fetch` as the only supported Bitrix mode.
- Do not add dependencies.
- Preserve existing uncommitted user changes.
- Verify with targeted tests, shell syntax, and repository-wide legacy-method search.

### Task 1: Remove legacy lifecycle calls

**Files:**
- Modify: `server-side/register_bot.py`
- Test: `tests/test_register_bot.py`

- [ ] Add tests requiring v2 unregister to send `botId` and `botToken`, and rejecting legacy method names.
- [ ] Run the test and verify it fails against current code.
- [ ] Remove `imbot.bot.list` and `imbot.unregister`; pass the existing token to `imbot.v2.Bot.unregister`.
- [ ] Run the targeted tests.

### Task 2: Remove webhook bridge

**Files:**
- Delete: `server-side/app.py`
- Delete: `server-side/bitrix-bot.service`
- Modify: `install.sh`, `server-side/nginx`, `main.py`, `server-side/bitrix-bot.env.example`, `env.example`

- [ ] Add tests asserting bridge defaults are false and legacy endpoint/configuration is absent.
- [ ] Run tests red.
- [ ] Remove the service, route, bridge startup/configuration, and legacy event path.
- [ ] Run shell and targeted tests.

### Task 3: Align docs and finish verification

**Files:**
- Modify: `README.md`, `DEPLOYMENT.md`
- Test: existing Bitrix tests plus new lifecycle/configuration tests

- [ ] Update setup and migration instructions to describe only v2 fetch.
- [ ] Run targeted tests, full pytest, `bash -n install.sh`, and `git diff --check`.
- [ ] Search the repository for legacy Bitrix method/event names and review remaining matches.
