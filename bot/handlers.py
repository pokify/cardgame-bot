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
    deal_house,
    expires_at,
    house_confirm_keyboard,
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

# Serialize lobby message updates for each game.  A join and a scheduled
# lobby bump must never read/send/update the lobby concurrently, otherwise
# the bump can overwrite a freshly joined player with a stale player list.
_lobby_locks: dict[int, asyncio.Lock] = {}
_lobby_locks_guard = asyncio.Lock()


async def _lobby_lock(game_id: int) -> asyncio.Lock:
    """Return the stable per-game lock used for lobby updates."""
    async with _lobby_locks_guard:
        lock = _lobby_locks.get(game_id)
        if lock is None:
            lock = asyncio.Lock()
            _lobby_locks[game_id] = lock
        return lock


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
    lines = ["<b>Tomochi Card Leaderboard</b>", ""]
    # Build list with ranks for "passed by lower-score player" checks.
    ranked = list(enumerate(rows, start=1))
    top_score = int(rows[0]["score"]) if rows else 0
    # Crown only when strictly alone at the top (at least 1 point ahead).
    sole_leader = len(rows) == 1 or top_score > int(rows[1]["score"])
    for i, row in ranked:
        prev_rank = row["prev_rank"]
        prev_score = row["prev_score"]
        score = int(row["score"])

        if i == 1 and sole_leader:
            # Only crown when at least 1 point clear of everyone else.
            # Tied top scores -> dash; still clear after a points loss -> keep crown.
            prefix = "\U0001F451"  # crown
        elif prev_rank is None:
            # First appearance on the board.
            prefix = "\u2014"  # em dash
        elif i < prev_rank and prev_score is not None and score > int(prev_score):
            # Climbed by gaining points (not just reshuffled).
            prefix = "\u2B06\uFE0F"  # up arrow emoji
        elif i > prev_rank and prev_score is not None:
            dropped_points = score < int(prev_score)
            # Someone who had strictly fewer points before is now above us.
            passed_from_below = any(
                other["prev_score"] is not None
                and int(other["prev_score"]) < int(prev_score)
                for j, other in ranked
                if j < i
            )
            if dropped_points or passed_from_below:
                prefix = "\u2B07\uFE0F"  # down arrow emoji
            else:
                # Rank number fell only because peers left our tier - neutral.
                prefix = "\u2014"  # em dash
        else:
            # Includes tied leaders (i==1 but not sole_leader) and unchanged ranks.
            prefix = "\u2014"  # em dash

        who = mention(row["username"], row["first_name"], row["user_id"])
        lines.append(
            f"{prefix} {who}\n"
            f"Score: {row['score']} | Played: {row['played']} | Wins: {row['wins']}"
        )
    return "\n".join(lines)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type == "private":
        await update.message.reply_text(
            "Add me to a group, then use /cards to start a lobby and /cardslb for scores."
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

    lock = await _lobby_lock(game_id)

    # Serialize the join with lobby bumps.  In particular, the bump must not
    # read an old player list and replace the message after this join succeeds.
    async with lock:
        ok, reason, players = await db.add_player(
            game_id, user.id, user.username, user.first_name
        )
        if not ok:
            alerts = {
                "already_in": (
                    "Join successful. Other players have seen you have joined! "
                    "Your user interface may not be showing it for you at this moment!"
                ),
                "full": "Lobby is full.",
                "not_waiting": "This game already started.",
                "not_found": "Lobby not found.",
            }
            await query.answer(alerts.get(reason, "Can't join."), show_alert=True)
            return

        await query.answer()

        # Always build the visible lobby from the latest DB state while the
        # lobby lock is held.
        players = await db.list_players(game_id)

        if len(players) >= MAX_PLAYERS:
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            # Run the game only after the lock is released.  The DB status
            # check in _run_game prevents duplicate starts.
            run_game = True
        else:
            run_game = False
            text = lobby_text(players, game["flavor"])
            markup = lobby_keyboard(game_id, house_available=len(players) <= 1)

            # The message that contained the clicked button may already have
            # been replaced by a lobby bump.  Refresh the stored message_id
            # while holding the same lock used by bump_lobby_job.
            fresh = await db.get_game(game_id)
            mid = fresh["message_id"] if fresh else game["message_id"]

            # Edit the current lobby directly through the Bot API.  We do not
            # use query.edit_message_text() here because the mobile client has
            # intermittently shown a stale version of the edited callback
            # message even though the database join succeeded.
            #
            # The current message_id comes from the DB, so this also handles a
            # lobby that was bumped/replaced since the player tapped Join.
            if mid:
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat.id,
                        message_id=mid,
                        text=text,
                        parse_mode="HTML",
                        reply_markup=markup,
                        disable_web_page_preview=True,
                    )
                except Exception as exp:
                    log.warning("Could not refresh lobby via Bot API: %s", exp)
                    # Re-read the message pointer once and retry.  This avoids
                    # creating a new group message just because the lobby was
                    # replaced between our reads.
                    fresh_retry = await db.get_game(game_id)
                    retry_mid = fresh_retry["message_id"] if fresh_retry else None
                    if retry_mid and retry_mid != mid:
                        try:
                            await context.bot.edit_message_text(
                                chat_id=chat.id,
                                message_id=retry_mid,
                                text=text,
                                parse_mode="HTML",
                                reply_markup=markup,
                                disable_web_page_preview=True,
                            )
                        except Exception:
                            log.exception("Could not refresh lobby on retry")

    if run_game:
        await _run_game(context, game_id, chat.id)



HOUSE_CONFIRM_TIMEOUT = 30


def _house_confirm_text(user) -> str:
    who = mention(user.username, user.first_name, user.id)
    return (
        f"You can challenge the House twice a day as {who}.\n\n"
        "Wins/Losses will not go to leaderboard."
    )


async def house_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the House challenge confirmation privately to the caller."""
    query = update.callback_query
    user = query.from_user
    group_chat = query.message.chat

    try:
        game_id = int(query.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await query.answer()
        return

    game = await db.get_game(game_id)
    if game is None or game["chat_id"] != group_chat.id:
        await query.answer("This lobby is gone.", show_alert=True)
        return

    if game["status"] != "waiting":
        await query.answer("This game has already started.", show_alert=True)
        return

    players = await db.list_players(game_id)

    if len(players) > 1:
        await query.answer(
            "Can not challenge house once another player has joined.",
            show_alert=True,
        )
        return

    if not players or players[0]["user_id"] != user.id:
        await query.answer(
            "Only game caller can challenge the House.",
            show_alert=True,
        )
        return

    used = await db.house_challenges_used(group_chat.id, user.id)
    if used >= 2:
        await query.answer(
            "You have already used both House challenges today.",
            show_alert=True,
        )
        return

    try:
        # Telegram does not allow an inline-keyboard alert, so the private
        # Continue/Cancel confirmation is sent to the caller's private chat.
        prompt = await context.bot.send_message(
            chat_id=user.id,
            text=_house_confirm_text(user),
            parse_mode="HTML",
            reply_markup=house_confirm_keyboard(game_id),
            disable_web_page_preview=True,
        )
    except Exception:
        # Bots cannot start a private chat with a user who has never opened
        # them. Keep the group UI private in that case and tell the caller
        # exactly what is required before trying again.
        await query.answer(
            "Please open a private chat with the bot and press Start first, "
            "then try Challenge The House again.",
            show_alert=True,
        )
        return

    await query.answer()

    jq = context.job_queue
    jq.run_once(
        house_prompt_timeout_job,
        when=HOUSE_CONFIRM_TIMEOUT,
        data={"chat_id": user.id, "message_id": prompt.message_id},
        name=f"house_prompt:{prompt.message_id}",
        chat_id=user.id,
    )


async def house_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    for job in context.job_queue.get_jobs_by_name(
        f"house_prompt:{query.message.message_id}"
    ):
        job.schedule_removal()
    await _delete_quietly(
        context.bot, query.message.chat.id, query.message.message_id
    )


async def house_continue_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user

    try:
        game_id = int(query.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await query.answer()
        return

    game = await db.get_game(game_id)
    if game is None:
        await query.answer("This lobby is gone.", show_alert=True)
        return

    group_chat_id = game["chat_id"]

    if game["status"] != "waiting":
        await query.answer("This game has already started.", show_alert=True)
        return

    players = await db.list_players(game_id)

    if len(players) > 1:
        await query.answer(
            "Can not challenge house once another player has joined.",
            show_alert=True,
        )
        return

    if not players or players[0]["user_id"] != user.id:
        await query.answer(
            "Only game caller can challenge the House.",
            show_alert=True,
        )
        return

    consumed, _remaining = await db.consume_house_challenge(group_chat_id, user.id)
    if not consumed:
        await query.answer(
            "You have already used both House challenges today.",
            show_alert=True,
        )
        return

    await query.answer()
    for job in context.job_queue.get_jobs_by_name(
        f"house_prompt:{query.message.message_id}"
    ):
        job.schedule_removal()
    await _delete_quietly(
        context.bot, query.message.chat.id, query.message.message_id
    )

    await _run_house_challenge(context, game_id, group_chat_id, user.id)


async def house_prompt_timeout_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data or {}
    await _delete_quietly(
        context.bot, data.get("chat_id"), data.get("message_id")
    )


async def _run_house_challenge(
    context: ContextTypes.DEFAULT_TYPE,
    game_id: int,
    chat_id: int,
    user_id: int,
) -> None:
    game = await db.get_game(game_id)
    if game is None or game["status"] != "waiting":
        return

    players = await db.list_players(game_id)
    if len(players) != 1 or players[0]["user_id"] != user_id:
        return

    await db.set_status(game_id, "running")
    _cancel_chat_jobs(context, chat_id, game_id)

    if game["message_id"]:
        await _delete_quietly(context.bot, chat_id, game["message_id"])

    player = players[0]
    player_tag = mention(
        player["username"], player["first_name"], player["user_id"]
    )

    # This announcement is intentionally permanent and visible to everyone.
    await context.bot.send_message(
        chat_id,
        f"{player_tag} has challenged the house! ⚔️",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    await context.bot.send_message(chat_id, "Dealing...")
    await asyncio.sleep(5)

    assignments, winner, _house = deal_house(player)

    photo = render_deal(assignments)
    await context.bot.send_photo(
        chat_id,
        photo=InputFile(photo, filename="house_challenge.png"),
    )

    if winner["user_id"] == player["user_id"]:
        result = f"{player_tag} highest score, you win! 😤\n\nHouse will get you next time!"
    else:
        result = (
            f"{player_tag} you lose! Never bet against the House! "
            "Better luck next time! 😗"
        )

    await context.bot.send_message(
        chat_id,
        result,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    # House challenge results intentionally never touch the leaderboard.
    await db.set_status(game_id, "finished")


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

    await context.bot.send_message(chat_id, "Dealing...")
    await asyncio.sleep(5)

    assignments, winner, joker = deal(players)
    photo = render_deal(assignments)
    await context.bot.send_photo(chat_id, photo=InputFile(photo, filename="deal.png"))

    winner_tag = mention(winner["username"], winner["first_name"], winner["user_id"])
    result = f"{winner_tag} wins! {WIN_POINTS} points!"
    if joker:
        joker_tag = mention(joker["username"], joker["first_name"], joker["user_id"])
        result += f"\n\n{joker_tag} Joker pulled -{JOKER_PENALTY} points \U0001F62D"
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
        await update.message.reply_text("Use /cardslb in the group.")
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

    lock = await _lobby_lock(game_id)

    # A bump and a Join Game operation share this lock.  This prevents a bump
    # from reading a stale player list and replacing a newly updated lobby.
    async with lock:
        game = await db.get_game(game_id)
        if game is None or game["status"] != "waiting":
            return

        # Re-read players while holding the lock, immediately before creating
        # the replacement message.
        players = await db.list_players(game_id)
        old_id = game["message_id"]

        try:
            msg = await context.bot.send_message(
                chat_id,
                lobby_text(players, game["flavor"]),
                parse_mode="HTML",
                reply_markup=lobby_keyboard(game_id, house_available=len(players) <= 1),
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