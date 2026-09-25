from __future__ import annotations

import logging
from datetime import datetime, timezone

from telegram import InputFile, Update
from telegram.ext import ContextTypes

from bot import db
from bot.config import AUTO_INTERVAL_SECONDS, GAME_CHAT_IDS, LOBBY_SECONDS, MAX_PLAYERS, MIN_PLAYERS, WIN_POINTS
from bot.game import deal, expires_at, lobby_keyboard, lobby_text, mention
from bot.images import render_deal

log = logging.getLogger(__name__)


def _schedule_expire(context: ContextTypes.DEFAULT_TYPE, game_id: int, chat_id: int) -> None:
    jq = context.job_queue
    name = f"expire:{game_id}"
    for job in jq.get_jobs_by_name(name):
        job.schedule_removal()
    jq.run_once(
        expire_job,
        when=LOBBY_SECONDS,
        data={"game_id": game_id, "chat_id": chat_id},
        name=name,
        chat_id=chat_id,
    )


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type == "private":
        await update.message.reply_text(
            "Add me to a group, then use /card to start a lobby and /leaderboard for scores."
        )
        return
    await card_cmd(update, context)


async def card_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Start a game in a group with /card.")
        return

    existing = await db.active_game(chat.id)
    if existing:
        await update.message.reply_text("A game is already in progress in this chat.")
        return

    game = await db.create_game(chat.id, "manual", expires_at())
    ok, _, players = await db.add_player(
        game["id"], user.id, user.username, user.first_name
    )
    if not ok:
        await db.set_status(game["id"], "expired")
        await update.message.reply_text("Could not create the lobby. Try again.")
        return

    msg = await update.message.reply_html(
        lobby_text("manual", players),
        reply_markup=lobby_keyboard(game["id"]),
        disable_web_page_preview=True,
    )
    await db.set_message_id(game["id"], msg.message_id)
    _schedule_expire(context, game["id"], chat.id)


async def join_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    chat = query.message.chat
    try:
        game_id = int(query.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await query.answer()
        return

    game = await db.get_game(game_id)
    if game is None or game["chat_id"] != chat.id:
        await query.answer("This lobby is gone.", show_alert=True)
        return

    ok, reason, players = await db.add_player(
        game_id, user.id, user.username, user.first_name
    )
    if not ok:
        alerts = {
            "already_in": "You already joined.",
            "full": "Lobby is full.",
            "not_waiting": "This game already started.",
            "not_found": "Lobby not found.",
        }
        await query.answer(alerts.get(reason, "Can't join."), show_alert=True)
        return

    await query.answer()

    if len(players) >= MAX_PLAYERS:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await _run_game(context, game_id, chat.id)
        return

    try:
        await query.edit_message_text(
            lobby_text(game["mode"], players),
            parse_mode="HTML",
            reply_markup=lobby_keyboard(game_id),
            disable_web_page_preview=True,
        )
    except Exception as exc:
        log.warning("Could not edit lobby: %s", exc)


async def expire_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data or {}
    game_id = data["game_id"]
    chat_id = data["chat_id"]
    game = await db.get_game(game_id)
    if game is None or game["status"] != "waiting":
        return

    players = await db.list_players(game_id)
    if len(players) < MIN_PLAYERS:
        await db.set_status(game_id, "expired")
        if game["message_id"]:
            try:
                await context.bot.delete_message(chat_id, game["message_id"])
            except Exception:
                try:
                    await context.bot.edit_message_text(
                        "Not enough players. Game cancelled.",
                        chat_id=chat_id,
                        message_id=game["message_id"],
                    )
                except Exception:
                    pass
        return

    await _run_game(context, game_id, chat_id)


async def _run_game(context: ContextTypes.DEFAULT_TYPE, game_id: int, chat_id: int) -> None:
    game = await db.get_game(game_id)
    if game is None or game["status"] != "waiting":
        return

    players = await db.list_players(game_id)
    if len(players) < MIN_PLAYERS:
        await db.set_status(game_id, "expired")
        return

    await db.set_status(game_id, "running")
    for job in context.job_queue.get_jobs_by_name(f"expire:{game_id}"):
        job.schedule_removal()

    if game["message_id"]:
        try:
            await context.bot.delete_message(chat_id, game["message_id"])
        except Exception:
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=chat_id, message_id=game["message_id"], reply_markup=None
                )
            except Exception:
                pass

    names = ", ".join(mention(p["username"], p["first_name"], p["user_id"]) for p in players)
    await context.bot.send_message(
        chat_id,
        f"Game started! ({len(players)} players)\n\nPlayers: {names}",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    assignments, winner = deal(players)
    photo = render_deal(assignments)
    await context.bot.send_photo(chat_id, photo=InputFile(photo, filename="deal.png"))

    winner_tag = mention(winner["username"], winner["first_name"], winner["user_id"])
    await context.bot.send_message(
        chat_id,
        f"{winner_tag} wins! {WIN_POINTS} points!",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    await db.save_deal(game_id, assignments, winner["user_id"])
    await db.bump_stats(chat_id, players, winner["user_id"], WIN_POINTS)


async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Use /leaderboard in the group.")
        return
    rows = await db.leaderboard(chat.id)
    if not rows:
        await update.message.reply_text("No games yet. Start one with /card.")
        return

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = ["<b>Card Game Leaderboard</b>", ""]
    for i, row in enumerate(rows, start=1):
        prefix = medals.get(i, f"{i}.")
        who = mention(row["username"], row["first_name"], row["user_id"])
        lines.append(
            f"{prefix} {who}\n"
            f"Score: {row['score']} | Played: {row['played']} | Wins: {row['wins']}"
        )
    await update.message.reply_html("\n".join(lines), disable_web_page_preview=True)


async def hourly_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_ids = GAME_CHAT_IDS or (context.job.data or {}).get("chat_ids") or []
    for chat_id in chat_ids:
        try:
            await _open_auto_lobby(context, int(chat_id))
        except Exception:
            log.exception("Auto lobby failed for %s", chat_id)


async def _open_auto_lobby(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    existing = await db.active_game(chat_id)
    if existing:
        return
    game = await db.create_game(chat_id, "auto", expires_at())
    msg = await context.bot.send_message(
        chat_id,
        lobby_text("auto", []),
        parse_mode="HTML",
        reply_markup=lobby_keyboard(game["id"]),
        disable_web_page_preview=True,
    )
    await db.set_message_id(game["id"], msg.message_id)
    _schedule_expire(context, game["id"], chat_id)


async def restore_jobs(application) -> None:
    jq = application.job_queue
    now = datetime.now(timezone.utc)
    for game in await db.waiting_games():
        remaining = (game["expires_at"] - now).total_seconds()
        name = f"expire:{game['id']}"
        if remaining <= 0:
            jq.run_once(
                expire_job,
                when=1,
                data={"game_id": game["id"], "chat_id": game["chat_id"]},
                name=name,
                chat_id=game["chat_id"],
            )
        else:
            jq.run_once(
                expire_job,
                when=remaining,
                data={"game_id": game["id"], "chat_id": game["chat_id"]},
                name=name,
                chat_id=game["chat_id"],
            )

    if GAME_CHAT_IDS:
        jq.run_repeating(
            hourly_job,
            interval=AUTO_INTERVAL_SECONDS,
            first=AUTO_INTERVAL_SECONDS,
            name="hourly_auto",
            data={"chat_ids": GAME_CHAT_IDS},
        )
