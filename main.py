from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from telegram import Update
from telegram.ext import AIORateLimiter, Application, CommandHandler, MessageHandler, MessageReactionHandler, filters
from telegram.request import HTTPXRequest
import uvicorn

from bitrix_client import BitrixClient
from handlers import cmd_start, cmd_whereami, on_edited_message, on_message, on_message_reaction
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
    return [Update.MESSAGE, Update.EDITED_MESSAGE, Update.MESSAGE_REACTION]


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
    application.add_handler(MessageHandler(filters.ALL & ~filters.UpdateType.EDITED_MESSAGE & ~filters.COMMAND, on_message))
    application.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & ~filters.COMMAND, on_edited_message))
    application.add_handler(MessageReactionHandler(on_message_reaction))
    return application


def _build_http_app(settings: Settings, application: Application, mirror: MirrorService) -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    async def health() -> dict[str, object]:
        webhook_status = application.bot_data.get("telegram_webhook_status", {})
        return {
            "ok": True,
            "telegram_webhook_enabled": settings.telegram_webhook_enabled,
            "bitrix_webhook_bridge_enabled": settings.bitrix_webhook_bridge_enabled,
            "telegram_webhook_status": webhook_status,
        }

    @app.post(settings.mirror_internal_event_path)
    async def bitrix_event_bridge(request: Request) -> dict[str, object]:
        if not settings.bitrix_webhook_bridge_enabled:
            raise HTTPException(status_code=404, detail="Bitrix webhook bridge is disabled")

        expected_secret = settings.mirror_internal_webhook_secret or ""
        provided_secret = request.headers.get("X-Internal-Webhook-Secret", "")
        if expected_secret != provided_secret:
            raise HTTPException(status_code=403, detail="Forbidden")

        payload = await request.json()
        dialog_id = str(payload.get("dialog_id") or "").strip()
        if not dialog_id:
            raise HTTPException(status_code=400, detail="dialog_id is required")

        event_name = str(payload.get("event") or "bitrix-webhook").strip() or "bitrix-webhook"
        accepted = await mirror.schedule_bitrix_dialog_sync(dialog_id, trigger=event_name)
        return {"ok": True, "accepted": accepted}

    @app.post(settings.telegram_webhook_path)
    async def telegram_webhook(request: Request) -> dict[str, object]:
        if not settings.telegram_webhook_enabled:
            raise HTTPException(status_code=404, detail="Telegram webhook is disabled")

        expected_secret = settings.telegram_webhook_secret or ""
        provided_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if expected_secret != provided_secret:
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
        "Combined runtime started. host=%s port=%s bitrix_bridge=%s telegram_webhook=%s",
        settings.mirror_http_host,
        settings.mirror_http_port,
        settings.bitrix_webhook_bridge_enabled,
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
        "Starting bot. chat_mappings=%s proxy=%s tg_to_bitrix=%s bitrix_to_tg=%s tg_webhook=%s bitrix_bridge=%s",
        [(m.tg_chat_id, m.bitrix_dialog_id) for m in settings.chat_mappings],
        settings.socks5_proxy_url,
        settings.sync_telegram_to_bitrix,
        settings.sync_bitrix_to_telegram,
        settings.telegram_webhook_enabled,
        settings.bitrix_webhook_bridge_enabled,
    )

    if settings.telegram_webhook_enabled or settings.bitrix_webhook_bridge_enabled:
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

