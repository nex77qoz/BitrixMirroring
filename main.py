from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from telegram import Update
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    MessageReactionHandler,
    filters,
)
from telegram.request import HTTPXRequest

from bitrix_client import BitrixClient
from handlers import (
    cmd_clear,
    cmd_connect,
    cmd_disconnect,
    cmd_start,
    cmd_whereami,
    on_admin_callback,
    on_edited_message,
    on_message,
    on_message_reaction,
    on_private_admin_message,
)
from mirror_service import MirrorService
from mirror_state_store import MirrorStateStore
from settings import Settings


def _configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

def _allowed_updates() -> list[str]:
    return [Update.MESSAGE, Update.EDITED_MESSAGE, Update.MESSAGE_REACTION, Update.CALLBACK_QUERY]


def _build_application(settings: Settings, bitrix: BitrixClient, mirror: MirrorService, *, with_callbacks: bool) -> Application:
    telegram_request = HTTPXRequest(
        proxy=settings.socks5_proxy_url,
        connect_timeout=settings.request_timeout_seconds,
        read_timeout=settings.request_timeout_seconds,
        write_timeout=settings.request_timeout_seconds,
        pool_timeout=settings.request_timeout_seconds,
    )

    builder = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .request(telegram_request)
        .get_updates_request(telegram_request)
        .rate_limiter(AIORateLimiter())
    )

    if with_callbacks:
        async def post_init(application: Application) -> None:
            logger = logging.getLogger("tg-bitrix-mirror")
            logger.info("Bot is starting")
            app_mirror: MirrorService = application.bot_data["mirror_service"]
            await app_mirror.start(application)

        async def post_shutdown(application: Application) -> None:
            logger = logging.getLogger("tg-bitrix-mirror")
            app_bitrix: BitrixClient = application.bot_data["bitrix_client"]
            app_mirror: MirrorService = application.bot_data["mirror_service"]
            await app_mirror.stop()
            await app_bitrix.close()
            logger.info("Bot is stopped")

        builder = builder.post_init(post_init).post_shutdown(post_shutdown)

    application = builder.build()
    application.bot_data["bitrix_client"] = bitrix
    application.bot_data["mirror_service"] = mirror

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("whereami", cmd_whereami))
    application.add_handler(CommandHandler("connect", cmd_connect))
    application.add_handler(CommandHandler("disconnect", cmd_disconnect))
    application.add_handler(CommandHandler("clear", cmd_clear))
    application.add_handler(CallbackQueryHandler(on_admin_callback, pattern=r"^admin:"))
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, on_private_admin_message))
    application.add_handler(MessageHandler(filters.ALL & ~filters.UpdateType.EDITED_MESSAGE & ~filters.COMMAND, on_message))
    application.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & ~filters.COMMAND, on_edited_message))
    application.add_handler(MessageReactionHandler(on_message_reaction))
    return application


def _build_http_app(settings: Settings, application: Application, mirror: MirrorService) -> FastAPI:
    app = FastAPI()

    @app.get("/health", response_model=None)
    async def health() -> dict[str, object] | JSONResponse:
        webhook_status = application.bot_data.get("telegram_webhook_status", {})
        checks: dict[str, object] = {}

        try:
            state_store = getattr(mirror, "state_store", None)
            if state_store is not None:
                await state_store.load_bitrix_event_offset(settings.bitrix_bot_id)
                checks["db"] = "ok"
            else:
                checks["db"] = "no_state_store"
        except Exception as exc:
            checks["db"] = {"error": str(exc) or type(exc).__name__}

        bitrix_event_task = getattr(mirror, "_bitrix_event_task", None)
        if bitrix_event_task is not None:
            checks["bitrix_event_fetcher_alive"] = not bitrix_event_task.done()
        elif settings.sync_bitrix_to_telegram:
            checks["bitrix_event_fetcher_alive"] = "not_started"
        else:
            checks["bitrix_event_fetcher_alive"] = "disabled"

        fetcher_status = checks.get("bitrix_event_fetcher_alive")
        healthy = checks.get("db") == "ok" and (fetcher_status is True or fetcher_status == "disabled")
        response = {
            "ok": healthy,
            "telegram_webhook_enabled": settings.telegram_webhook_enabled,
            "forwarding_enabled": mirror.is_forwarding_enabled(),
            "telegram_webhook_status": webhook_status,
            "checks": checks,
        }
        if not healthy:
            return JSONResponse(status_code=503, content=response)
        return response

    def _verify_internal_secret(request: Request) -> None:
        expected_secret = settings.mirror_internal_webhook_secret or ""
        if not expected_secret:
            raise HTTPException(status_code=503, detail="Internal control secret is not configured")
        if not hmac.compare_digest(expected_secret, request.headers.get("X-Internal-Webhook-Secret", "")):
            raise HTTPException(status_code=403, detail="Forbidden")

    @app.get("/internal/forwarding")
    async def forwarding_status(request: Request) -> dict[str, object]:
        _verify_internal_secret(request)
        return {"ok": True, "forwarding_enabled": mirror.is_forwarding_enabled()}

    @app.post("/internal/forwarding")
    async def set_forwarding(request: Request) -> dict[str, object]:
        _verify_internal_secret(request)
        payload = await request.json()
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise HTTPException(status_code=400, detail="enabled must be a boolean")
        return {"ok": True, "forwarding_enabled": await mirror.set_forwarding_enabled(enabled)}

    @app.post("/internal/mappings/reload")
    async def reload_mappings(request: Request) -> dict[str, object]:
        _verify_internal_secret(request)
        await mirror.reload_mappings()
        return {"ok": True}

    @app.post(settings.telegram_webhook_path)
    async def telegram_webhook(request: Request) -> dict[str, object]:
        if not settings.telegram_webhook_enabled:
            raise HTTPException(status_code=404, detail="Telegram webhook is disabled")

        expected_secret = settings.telegram_webhook_secret or ""
        if not expected_secret:
            raise HTTPException(status_code=503, detail="Telegram webhook secret is not configured")
        provided_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(expected_secret, provided_secret):
            raise HTTPException(status_code=403, detail="Forbidden")

        payload = await request.json()
        update = Update.de_json(payload, application.bot)
        if update is not None:
            await application.process_update(update)
        return {"ok": True}

    return app


async def _verify_telegram_webhook(application: Application, settings: Settings) -> dict[str, object]:
    expected_url = (settings.telegram_webhook_public_url or "").rstrip("/") + settings.telegram_webhook_path
    info = await application.bot.get_webhook_info()
    status = {
        "expected_url": expected_url,
        "actual_url": info.url,
        "pending_update_count": info.pending_update_count,
        "last_error_date": info.last_error_date,
        "last_error_message": info.last_error_message,
        "max_connections": info.max_connections,
        "ip_address": info.ip_address,
        "has_custom_certificate": info.has_custom_certificate,
    }
    application.bot_data["telegram_webhook_status"] = status

    if info.url != expected_url:
        message = f"Telegram webhook URL mismatch: expected {expected_url}, got {info.url or '<empty>'}"
        if settings.telegram_webhook_strict_verify:
            raise RuntimeError(message)
        logging.getLogger("tg-bitrix-mirror").warning(message)
    return status


async def _run_combined_runtime(settings: Settings, bitrix: BitrixClient, mirror: MirrorService) -> None:
    logger = logging.getLogger("tg-bitrix-mirror")
    application = _build_application(settings, bitrix, mirror, with_callbacks=False)
    web_app = _build_http_app(settings, application, mirror)
    server = uvicorn.Server(
        uvicorn.Config(
            web_app,
            host=settings.mirror_http_host,
            port=settings.mirror_http_port,
            log_level=os.getenv("UVICORN_LOG_LEVEL", os.getenv("LOG_LEVEL", "info")).lower(),
        )
    )

    await application.initialize()
    await application.start()
    await mirror.start(application)
    logger.info(
        "Combined runtime started. host=%s port=%s telegram_webhook=%s",
        settings.mirror_http_host,
        settings.mirror_http_port,
        settings.telegram_webhook_enabled,
    )

    try:
        if settings.telegram_webhook_enabled:
            assert settings.telegram_webhook_public_url is not None
            assert settings.telegram_webhook_secret is not None
            await application.bot.set_webhook(
                url=settings.telegram_webhook_public_url.rstrip("/") + settings.telegram_webhook_path,
                allowed_updates=_allowed_updates(),
                secret_token=settings.telegram_webhook_secret,
                drop_pending_updates=settings.telegram_webhook_drop_pending_updates,
            )
            status = await _verify_telegram_webhook(application, settings)
            logger.info(
                "Telegram webhook enabled at %s%s pending_updates=%s ip=%s",
                settings.telegram_webhook_public_url.rstrip("/"),
                settings.telegram_webhook_path,
                status["pending_update_count"],
                status["ip_address"] or "-",
            )
        else:
            if application.updater is None:
                raise RuntimeError("python-telegram-bot updater is not available for polling mode")
            await application.updater.start_polling(
                allowed_updates=_allowed_updates(),
                drop_pending_updates=True,
            )
            logger.info("Telegram polling started inside combined runtime")

        await server.serve()
    finally:
        if settings.telegram_webhook_enabled:
            with contextlib.suppress(Exception):
                await application.bot.delete_webhook(drop_pending_updates=False)
        elif application.updater is not None and application.updater.running:
            with contextlib.suppress(Exception):
                await application.updater.stop()

        with contextlib.suppress(Exception):
            await mirror.stop()
        with contextlib.suppress(Exception):
            await application.stop()
        with contextlib.suppress(Exception):
            await application.shutdown()
        with contextlib.suppress(Exception):
            await bitrix.close()
        logger.info("Combined runtime stopped")


def main() -> None:
    load_dotenv()
    _configure_logging()

    settings = Settings.from_env()
    bitrix = BitrixClient(settings)
    state_store = MirrorStateStore(settings.mirror_state_db_path)
    mirror = MirrorService(settings, bitrix, state_store)

    logger = logging.getLogger("tg-bitrix-mirror")
    logger.info(
        "Starting bot. chat_mappings=%s proxy=%s tg_to_bitrix=%s bitrix_to_tg=%s tg_webhook=%s",
        [(m.tg_chat_id, m.bitrix_dialog_id) for m in settings.chat_mappings],
        settings.socks5_proxy_url,
        settings.sync_telegram_to_bitrix,
        settings.sync_bitrix_to_telegram,
        settings.telegram_webhook_enabled,
    )

    if settings.telegram_webhook_enabled:
        asyncio.run(_run_combined_runtime(settings, bitrix, mirror))
        return

    application = _build_application(settings, bitrix, mirror, with_callbacks=True)
    logger.info("Starting legacy polling runtime")
    application.run_polling(
        allowed_updates=_allowed_updates(),
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()

