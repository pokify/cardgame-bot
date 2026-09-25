from __future__ import annotations

import logging
import os

from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from bot import db
from bot.config import BOT_TOKEN, DATABASE_URL
from bot.handlers import card_cmd, join_cb, leaderboard_cmd, restore_jobs, start_cmd

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("cardgame")


async def post_init(application: Application) -> None:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    await db.connect(DATABASE_URL)
    await restore_jobs(application)
    log.info("Bot ready")


async def post_shutdown(application: Application) -> None:
    await db.close()


def main() -> None:
    token = BOT_TOKEN or os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is not set")

    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("card", card_cmd))
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))
    app.add_handler(CallbackQueryHandler(join_cb, pattern=r"^join:\d+$"))
    log.info("Polling…")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
