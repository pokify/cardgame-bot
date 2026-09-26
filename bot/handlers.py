from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from telegram import InputFile, Update
from telegram.constants import ChatMemberStatus
from telegram.ext import ContextTypes

from bot import db
from bot.config import AUTO_INTERVAL_SECONDS, GAME_CHAT_IDS, LOBBY_SECONDS, MAX_PLAYERS, MIN_PLAYERS, WIN_POINTS
from bot.game import (
    JOKER_PENALTY,
    deal,
    expires_at,
    lobby_keyboard,
    lobby_text,
    mention,
    pick_flavor,
    reset_keyboard,
    winner_keyboard,
)
from bot.images import render_deal

log = logging.getLogger(__name__)

BUMP_SECONDS = 30
RESET_TIMEOUT = 10


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


def _cancel_chat_jobs(context: ContextTypes.DEFAULT_TYPE, chat_id: int, game_id: int | None = None) -> None:
    jq = context.job_queue
    for job in jq.get_jobs_by_name(f"bump:{chat_id}"):
        job.schedule_removal()
    if game_id is not None:
        for job in jq.get_jobs_by_name(f"expire:{game_id}"):
            job.schedule_removal()


async def _is_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


async def _delete_quietly(bot, chat_id: int, message_id: int | None) -> None:
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


def _leaderboard_html(rows) -> str:
    if not rows:
        return "No games yet. Start one with /cards."
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = ["<b>Tomochi Card Leaderboard</b>", ""]
    for i, row in enumerate(rows, start=1):
        prefix = medals.get(i, f"{i}.")
        who = mention(row["username"], row["first_name"], row["user_id"])
        lines.append(
            f"{prefix} {who}\n"
            f"Score: {row['score']} | Played: {row['played']} | Wins: {row['wins']}"
        )
    return "\n".join(lines)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type == "private":
        await update.message.reply_text(
            "Add me to a group, then use /cards to start a lobby and /lb for scores."
        )
        return
    await cards_cmd(update, context)


async def cards_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Start a game in a group with /cards.")
        return

    existing = await db.active_game(chat.id)
    if existing:
        await update.message.reply_text("A game is already in progress in this chat.")
        return

    standing = await db.caller_standing(chat.id, user.id)
    flavor = pick_flavor(standing)
    game = await db.create_game(chat.id, "manual", expires_at(), flavor)
    ok, _, players = await db.add_player(
        game["id"], user.id, user.username, user.first_name
    )
    if not ok:
        await db.set_status(game["id"], "expired")
        await update.message.reply_text("Could not create the lobby. Try again.")
        return

    msg = await update.message.reply_html(
        lobby_text(players, game["flavor"]),
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
            lobby_text(players, game["flavor"]),
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
        _cancel_chat_jobs(context, chat_id, game_id)
        await _delete_quietly(context.bot, chat_id, game["message_id"])
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
    _cancel_chat_jobs(context, chat_id, game_id)

    if game["message_id"]:
        await _delete_quietly(context.bot, chat_id, game["message_id"])

    names = ", ".join(mention(p["username"], p["first_name"], p["user_id"]) for p in players)
    await context.bot.send_message(
        chat_id,
        f"Game started! ({len(players)} players)\n\nPlayers: {names}",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    await context.bot.send_message(chat_id, "Dealing…")
    await asyncio.sleep(5)

    assignments, winner, joker = deal(players)
    photo = render_deal(assignments)
    await context.bot.send_photo(chat_id, photo=InputFile(photo, filename="deal.png"))

    winner_tag = mention(winner["username"], winner["first_name"], winner["user_id"])
    result = f"{winner_tag} wins! {WIN_POINTS} points!"
    if joker:
        joker_tag = mention(joker["username"], joker["first_name"], joker["user_id"])
        result += f"\n\n{joker_tag} Joker pulled -{JOKER_PENALTY} points 😭"
    await context.bot.send_message(
        chat_id,
        result,
        parse_mode="HTML",
        reply_markup=winner_keyboard(),
        disable_web_page_preview=True,
    )

    await db.save_deal(game_id, assignments, winner["user_id"])
    await db.bump_stats(
        chat_id,
        players,
        winner["user_id"],
        WIN_POINTS,
        joker["user_id"] if joker else None,
        JOKER_PENALTY,
    )


async def lb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Use /lb in the group.")
        return
    html = _leaderboard_html(await db.leaderboard(chat.id))
    await update.message.reply_html(html, disable_web_page_preview=True)


async def showlb_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat = query.message.chat
    html = _leaderboard_html(await db.leaderboard(chat.id))
    await context.bot.send_message(
        chat.id,
        html,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def group_activity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """If someone posts after the lobby, bump it to the bottom 30s later."""
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return
    if chat.type not in ("group", "supergroup"):
        return
    user = update.effective_user
    if user and user.is_bot:
        return
    text = message.text or message.caption or ""
    if text.startswith("/"):
        return

    game = await db.active_game(chat.id)
    if game is None or game["status"] != "waiting" or not game["message_id"]:
        return
    if message.message_id == game["message_id"]:
        return

    jq = context.job_queue
    name = f"bump:{chat.id}"
    for job in jq.get_jobs_by_name(name):
        job.schedule_removal()
    jq.run_once(
        bump_lobby_job,
        when=BUMP_SECONDS,
        data={"chat_id": chat.id, "game_id": game["id"]},
        name=name,
        chat_id=chat.id,
    )


async def bump_lobby_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data or {}
    chat_id = data["chat_id"]
    game_id = data["game_id"]
    game = await db.get_game(game_id)
    if game is None or game["status"] != "waiting":
        return
    players = await db.list_players(game_id)
    old_id = game["message_id"]
    try:
        msg = await context.bot.send_message(
            chat_id,
            lobby_text(players, game["flavor"]),
            parse_mode="HTML",
            reply_markup=lobby_keyboard(game_id),
            disable_web_page_preview=True,
        )
    except Exception:
        log.exception("Lobby bump failed")
        return
    await db.set_message_id(game_id, msg.message_id)
    await _delete_quietly(context.bot, chat_id, old_id)


async def resetlb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if chat.type not in ("group", "supergroup"):
        return
    await _delete_quietly(context.bot, chat.id, message.message_id if message else None)
    if not await _is_admin(context, chat.id, user.id):
        return

    prompt = await context.bot.send_message(
        chat.id,
        "Reset Tomochi Card Leaderboard?\n\nAdmins Only",
        reply_markup=reset_keyboard(),
    )
    jq = context.job_queue
    name = f"resetlb:{chat.id}:{prompt.message_id}"
    jq.run_once(
        reset_timeout_job,
        when=RESET_TIMEOUT,
        data={"chat_id": chat.id, "message_id": prompt.message_id},
        name=name,
        chat_id=chat.id,
    )


async def reset_timeout_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data or {}
    await _delete_quietly(context.bot, data["chat_id"], data["message_id"])


async def resetlb_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat = query.message.chat
    user = query.from_user
    choice = query.data.split(":", 1)[1]
    if not await _is_admin(context, chat.id, user.id):
        await query.answer("Admins only.", show_alert=True)
        return

    await query.answer()
    for job in context.job_queue.get_jobs_by_name(
        f"resetlb:{chat.id}:{query.message.message_id}"
    ):
        job.schedule_removal()

    if choice == "no":
        await _delete_quietly(context.bot, chat.id, query.message.message_id)
        return

    cancelled = await db.reset_group(chat.id)
    for game in cancelled:
        _cancel_chat_jobs(context, chat.id, game["id"])
        await _delete_quietly(context.bot, chat.id, game["message_id"])
    await _delete_quietly(context.bot, chat.id, query.message.message_id)
    await context.bot.send_message(chat.id, "Tomochi Card Leaderboard Reset!")


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
    flavor = pick_flavor(None)
    game = await db.create_game(chat_id, "auto", expires_at(), flavor)
    msg = await context.bot.send_message(
        chat_id,
        lobby_text([], game["flavor"]),
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
        jq.run_once(
            expire_job,
            when=max(1, remaining),
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
