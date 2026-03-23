from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import AIORateLimiter, Application, CommandHandler, MessageHandler, MessageReactionHandler, filters
from telegram.request import HTTPXRequest

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

async def post_init(application: Application) -> None:
    logger = logging.getLogger("tg-bitrix-mirror")
    logger.info("Bot is starting")
    mirror: MirrorService = application.bot_data["mirror_service"]
    await mirror.start(application)


async def post_shutdown(application: Application) -> None:
    logger = logging.getLogger("tg-bitrix-mirror")
    bitrix: BitrixClient = application.bot_data["bitrix_client"]
    mirror: MirrorService = application.bot_data["mirror_service"]
    await mirror.stop()
    await bitrix.close()
    logger.info("Bot is stopped")


def main() -> None:
    load_dotenv()
    _configure_logging()

    settings = Settings.from_env()
    bitrix = BitrixClient(settings)
    state_store = MirrorStateStore(settings.mirror_state_db_path)
    mirror = MirrorService(settings, bitrix, state_store)

    telegram_request = HTTPXRequest(
        proxy=settings.socks5_proxy_url,
        connect_timeout=settings.request_timeout_seconds,
        read_timeout=settings.request_timeout_seconds,
        write_timeout=settings.request_timeout_seconds,
        pool_timeout=settings.request_timeout_seconds,
    )

    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .request(telegram_request)
        .get_updates_request(telegram_request)
        .rate_limiter(AIORateLimiter())
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.bot_data["bitrix_client"] = bitrix
    application.bot_data["mirror_service"] = mirror

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("whereami", cmd_whereami))
    application.add_handler(MessageHandler(filters.ALL & ~filters.UpdateType.EDITED_MESSAGE & ~filters.COMMAND, on_message))
    application.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & ~filters.COMMAND, on_edited_message))
    application.add_handler(MessageReactionHandler(on_message_reaction))

    logging.getLogger("tg-bitrix-mirror").info(
        "Starting polling. chat_mappings=%s proxy=%s tg_to_bitrix=%s bitrix_to_tg=%s",
        [(m.tg_chat_id, m.bitrix_dialog_id) for m in settings.chat_mappings],
        settings.socks5_proxy_url,
        settings.sync_telegram_to_bitrix,
        settings.sync_bitrix_to_telegram,
    )
    application.run_polling(
        allowed_updates=[Update.MESSAGE, Update.EDITED_MESSAGE, Update.MESSAGE_REACTION],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()

