from __future__ import annotations

import logging
import os

from telegram import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from bot import db
from bot.config import BOT_TOKEN, DATABASE_URL
from bot.handlers import (
    cards_cmd,
    cancelcards_cmd,
    group_activity,
    house_cb,
    houseconfig_cb,
    houseconfig_cmd,
    join_cb,
    lb_cmd,
    pvpconfig_cb,
    pvpconfig_cmd,
    resetlb_cb,
    resetlb_cmd,
    restore_jobs,
    showlb_cb,
    start_cmd,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("cardgame")


async def post_init(application: Application) -> None:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    await db.connect(DATABASE_URL)
    private_commands = [
        BotCommand("cards", "Start Tomochi Cards"),
        BotCommand("cardslb", "Show cards leaderboard"),
    ]
    group_commands = [
        BotCommand("cards", "Start Tomochi Cards"),
        BotCommand("cancelcards", "Cancel new game"),
        BotCommand("cardslb", "Show cards leaderboard"),
        BotCommand("resetlb", "Reset cards leaderboard"),
        BotCommand("confighouse", "Config house"),
        BotCommand("pvpconfig", "Config game points"),
    ]
    await application.bot.set_my_commands(
        private_commands,
        scope=BotCommandScopeAllPrivateChats(),
    )
    await application.bot.set_my_commands(
        group_commands,
        scope=BotCommandScopeAllGroupChats(),
    )
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
    app.add_handler(CommandHandler("cards", cards_cmd))
    app.add_handler(CommandHandler("cancelcards", cancelcards_cmd))
    app.add_handler(CommandHandler("cardslb", lb_cmd))
    app.add_handler(CommandHandler("resetlb", resetlb_cmd))
    app.add_handler(CommandHandler(["confighouse", "houseconfig"], houseconfig_cmd))
    app.add_handler(CommandHandler("pvpconfig", pvpconfig_cmd))
    app.add_handler(CallbackQueryHandler(join_cb, pattern=r"^join:\d+$"))
    app.add_handler(CallbackQueryHandler(house_cb, pattern=r"^house:\d+$"))
    app.add_handler(CallbackQueryHandler(showlb_cb, pattern=r"^showlb$"))
    app.add_handler(CallbackQueryHandler(resetlb_cb, pattern=r"^resetlb:(yes|no)$"))
    app.add_handler(CallbackQueryHandler(houseconfig_cb, pattern=r"^hc:(toggle|plus|minus|nolimit|unlockplus|unlockminus|save|noop)$"))
    app.add_handler(CallbackQueryHandler(pvpconfig_cb, pattern=r"^pc:"))
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL,
            group_activity,
        ),
        group=1,
    )
    log.info("Polling...")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
