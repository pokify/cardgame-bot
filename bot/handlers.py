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
    pvp_base_bonus_for,
    mention,
    house_result_keyboard,
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


def _leaderboard_html(rows, show_house: bool = False) -> str:
    if not rows:
        return "No games yet. Start one with /cards."
    lines = ["<b>Tomochi Cards Leaderboard</b>", ""]
    if show_house:
        lines.append("H(House) W(Wins) L(Loss) P(Plays)")
        lines.append("")
    ranked = list(enumerate(rows, start=1))
    top_score = int(rows[0]["score"]) if rows else 0
    sole_leader = len(rows) == 1 or top_score > int(rows[1]["score"])
    for i, row in ranked:
        prev_rank = row["prev_rank"]
        prev_score = row["prev_score"]
        score = int(row["score"])

        if i == 1 and sole_leader:
            prefix = "\U0001F451"
        elif prev_rank is None:
            prefix = "\u2014"
        elif i < prev_rank and prev_score is not None and score > int(prev_score):
            prefix = "\u2B06\uFE0F"
        elif i > prev_rank and prev_score is not None:
            dropped_points = score < int(prev_score)
            passed_from_below = any(
                other["prev_score"] is not None
                and int(other["prev_score"]) < int(prev_score)
                for j, other in ranked
                if j < i
            )
            prefix = "\u2B07\uFE0F" if dropped_points or passed_from_below else "\u2014"
        else:
            prefix = "\u2014"

        who = mention(row["username"], row["first_name"], row["user_id"])
        wins = int(row["wins"])
        played = int(row["played"])
        losses = max(0, played - wins)
        if show_house:
            wins += int(row["house_wins"] or 0)
            losses += int(row["house_losses"] or 0)
            played += int(row["house_played"] or 0)
            house_pts = int(row["house_points"] or 0)
            stats = (
                f"Score: {score} (H: {house_pts}) | W: {wins} | L: {losses} | P: {played}"
            )
        else:
            stats = f"Score: {score} | W: {wins} | L: {losses} | P: {played}"
        lines.append(f"{prefix} {who}\n{stats}")
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

    # Create the lobby message even if an optional settings lookup fails.
    # Otherwise the game can remain active in the DB with no visible lobby.
    try:
        pvp_settings = await db.get_pvp_settings(chat.id)
    except Exception:
        log.exception("Could not load PvP settings while creating lobby")
        pvp_settings = db._default_pvp_settings()

    try:
        house_on = await db.house_enabled(chat.id)
    except Exception:
        log.exception("Could not load House availability while creating lobby")
        house_on = False

    msg = await update.message.reply_html(
        lobby_text(players, game["flavor"], pvp_settings),
        reply_markup=lobby_keyboard(game["id"], house_available=house_on),
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
            text = lobby_text(players, game["flavor"], await db.get_pvp_settings(chat.id))
            house_on = await db.house_enabled(chat.id)
            markup = lobby_keyboard(game_id, house_available=house_on and len(players) <= 1)

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



HOUSE_CONFIRM_TIMEOUT = 300

# First click on Challenge The House shows a private Telegram alert. The
# second click by the same user confirms the challenge and starts the game.
# The pending state is deliberately tied to both the game and user so nobody
# else can complete another user's confirmation.
_house_confirmed_clicks: dict[tuple[int, int], float] = {}


def _house_display_name(user) -> str:
    if user.username:
        return f"@{user.username}"
    return user.first_name or "player"


def _house_confirm_text(
    user,
    max_plays: int | None,
    pvp: dict | None = None,
    remaining: int | None = None,
) -> str:
    who = _house_display_name(user)
    rr_on = bool(pvp and db.house_rr_active(pvp))
    if max_plays is not None and rr_on:
        risk = int(pvp["house_risk"])
        reward = int(pvp["house_reward"])
        remaining = max(0, int(remaining if remaining is not None else max_plays))
        return (
            f"House: -{risk} loss/+{reward} win ♠ daily limit: {max_plays} ♣ "
            f"{who} {remaining} house challenges remaining ♦ click Challenge House to continue"
        )
    if rr_on:
        risk = int(pvp["house_risk"])
        reward = int(pvp["house_reward"])
        return (
            f"House: -{risk} loss/+{reward} win\n"
            "Click Challenge House to continue."
        )
    if max_plays is None:
        limit = "with no daily limit"
    else:
        limit = f"up to {max_plays} time(s) a day"
    return (
        f"You can challenge the House {limit} as {who}. "
        "Click Challenge House to continue."
    )

async def _house_cooldown_text(user, used: int) -> str:
    """Return the remaining time until the database's CURRENT_DATE resets."""
    row = await db.pool().fetchrow(
        """
        SELECT (
            date_trunc('day', CURRENT_TIMESTAMP) + INTERVAL '1 day'
            - CURRENT_TIMESTAMP
        ) AS remaining
        """
    )
    remaining = row["remaining"]
    total_seconds = max(0, int(remaining.total_seconds()))
    hours, rem = divmod(total_seconds, 3600)
    minutes = rem // 60
    who = _house_display_name(user)
    return (
        f"You have used your House Challenge limit for today! "
        f"You {who} can challenge house again in {hours} hours and {minutes} minutes."
    )


async def house_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """First click shows a private alert; second click by that same user starts."""
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

    settings = await db.get_house_settings(group_chat.id)
    if not settings["house_enabled"]:
        await query.answer("House challenges are disabled in this group.", show_alert=True)
        return

    max_plays = settings["max_plays_per_day"]
    pvp = await db.get_pvp_settings(group_chat.id)
    rr_on = db.house_rr_active(pvp)

    if max_plays is None and not rr_on:
        _house_confirmed_clicks.pop((game_id, user.id), None)
        await query.answer()
        await _run_house_challenge(context, game_id, group_chat.id, user.id)
        return

    used = 0
    remaining = None
    if max_plays is not None:
        used = await db.house_challenges_used(group_chat.id, user.id)
        remaining = max(0, int(max_plays) - used)
        if used >= max_plays:
            _house_confirmed_clicks.pop((game_id, user.id), None)
            await query.answer(
                await _house_cooldown_text(user, used),
                show_alert=True,
            )
            return

    key = (game_id, user.id)
    now_ts = datetime.now(timezone.utc).timestamp()
    confirmed_at = _house_confirmed_clicks.get(key)
    if confirmed_at is None or now_ts - confirmed_at > HOUSE_CONFIRM_TIMEOUT:
        _house_confirmed_clicks[key] = now_ts
        await query.answer(
            _house_confirm_text(user, max_plays, pvp, remaining),
            show_alert=True,
        )
        return

    if max_plays is not None:
        consumed, _remaining = await db.consume_house_challenge(
            group_chat.id, user.id, max_plays
        )
        if not consumed:
            _house_confirmed_clicks.pop(key, None)
            await query.answer(
                await _house_cooldown_text(user, max_plays),
                show_alert=True,
            )
            return

    _house_confirmed_clicks.pop(key, None)
    await query.answer()
    await _run_house_challenge(context, game_id, group_chat.id, user.id)


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

    banned = await db.banned_cards(chat_id, "house")
    assignments, winner, _house = deal_house(player, banned)
    await db.record_deal_memory(chat_id, "house", [a["card_key"] for a in assignments])

    photo = render_deal(assignments)
    await context.bot.send_photo(
        chat_id,
        photo=InputFile(photo, filename="house_challenge.png"),
    )

    player_won = winner["user_id"] == player["user_id"]
    pvp = await db.get_pvp_settings(chat_id)
    rr_on = db.house_rr_active(pvp)
    delta = 0
    if rr_on:
        delta = int(pvp["house_reward"]) if player_won else -int(pvp["house_risk"])

    if player_won:
        result = f"{player_tag} highest score, you win! 😤\n\nHouse will get you next time!"
        if rr_on:
            result += f"\n\n+{delta} points!"
    else:
        result = (
            f"{player_tag} you lose! Never bet against the House! "
            "Better luck next time! 😗"
        )
        if rr_on:
            result += f"\n\n{delta} points!"

    await context.bot.send_message(
        chat_id,
        result,
        parse_mode="HTML",
        reply_markup=winner_keyboard() if rr_on else house_result_keyboard(),
        disable_web_page_preview=True,
    )

    await db.bump_house_stats(chat_id, player, player_won)
    if rr_on and delta:
        await db.apply_pvp_score_delta(chat_id, player, delta)
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
    pvp = await db.get_pvp_settings(chat_id)
    base_points, join_bonus = pvp_base_bonus_for(pvp, len(players))
    if join_bonus > 0:
        points_line = f"Points to win: {base_points} (+{join_bonus})"
    else:
        points_line = f"Points to win: {base_points}"
    await context.bot.send_message(
        chat_id,
        f"Game started! ({len(players)} players)\n\n{points_line}\n\nPlayers: {names}",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    await context.bot.send_message(chat_id, "Dealing...")
    await asyncio.sleep(5)

    banned = await db.banned_cards(chat_id, "pvp2") if len(players) == 2 else []
    assignments, winner, joker = deal(players, banned)
    joker_points = int(pvp["joker_points"])
    if joker:
        joker["display_score"] = joker_points
    if len(players) == 2:
        await db.record_deal_memory(chat_id, "pvp2", [a["card_key"] for a in assignments])
    photo = render_deal(assignments)
    await context.bot.send_photo(chat_id, photo=InputFile(photo, filename="deal.png"))

    winner_tag = mention(winner["username"], winner["first_name"], winner["user_id"])
    points = db.pvp_points_for(pvp, len(players))
    result = f"{winner_tag} wins! {points} points!"
    if joker:
        joker_tag = mention(joker["username"], joker["first_name"], joker["user_id"])
        sign = "+" if joker_points > 0 else ""
        result += f"\n\n{joker_tag} Joker pulled {sign}{joker_points} points \U0001F62D"
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
        points,
        joker["user_id"] if joker else None,
        joker_points,
    )


async def cancelcards_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel the current waiting lobby. Admins only."""
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if chat.type not in ("group", "supergroup"):
        return

    if not await _is_admin(context, chat.id, user.id):
        await _delete_quietly(context.bot, chat.id, message.message_id if message else None)
        return

    game = await db.active_game(chat.id)
    if game is None or game["status"] != "waiting":
        await _delete_quietly(context.bot, chat.id, message.message_id if message else None)
        await context.bot.send_message(chat.id, "There is no game waiting to be cancelled.")
        return

    await db.set_status(game["id"], "expired")
    _cancel_chat_jobs(context, chat.id, game["id"])
    await _delete_quietly(context.bot, chat.id, game["message_id"])
    await _delete_quietly(context.bot, chat.id, message.message_id if message else None)
    await context.bot.send_message(chat.id, "Current game cancelled by an admin.")


_house_cfg_draft: dict[int, dict] = {}


def _house_cfg_text(draft: dict) -> str:
    plays = draft.get("max_plays_per_day")
    plays_label = "No limit" if plays is None else str(plays)
    state = "enabled" if draft.get("house_enabled") else "disabled"
    return (
        "<b>House Config</b>\n\n"
        f"House is currently {state}.\n"
        f"User max plays per day: {plays_label}\n\n"
        "  "
    )


def _house_cfg_keyboard(draft: dict):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    toggle = "Enable House" if not draft.get("house_enabled") else "Disable House"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(toggle, callback_data="hc:toggle")],
            [
                InlineKeyboardButton("−", callback_data="hc:minus"),
                InlineKeyboardButton("No limit", callback_data="hc:nolimit"),
                InlineKeyboardButton("+", callback_data="hc:plus"),
            ],
            [InlineKeyboardButton("Save", callback_data="hc:save")],
        ]
    )


def _house_lb_html(rows) -> str:
    if not rows:
        return "No House games yet."
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = ["<b>House Leaderboard</b>", ""]
    for i, row in enumerate(rows, start=1):
        prefix = medals.get(i, f"{i}.")
        who = mention(row["username"], row["first_name"], row["user_id"])
        lines.append(
            f"{prefix} {who}\n"
            f"Wins: {row['wins']} | Losses: {row['losses']} | Played: {row['played']}"
        )
    return "\n".join(lines)


async def houselb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Use /houselb in the group.")
        return
    html = _house_lb_html(await db.house_leaderboard(chat.id))
    await update.message.reply_html(html, disable_web_page_preview=True)


async def showhouselb_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat = query.message.chat
    html = _house_lb_html(await db.house_leaderboard(chat.id))
    await context.bot.send_message(
        chat.id, html, parse_mode="HTML", disable_web_page_preview=True
    )


async def resethouse_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if chat.type not in ("group", "supergroup"):
        return
    await _delete_quietly(context.bot, chat.id, message.message_id if message else None)
    if not await _is_admin(context, chat.id, user.id):
        return
    await db.reset_house_stats(chat.id)
    await context.bot.send_message(chat.id, "House Leaderboard Reset!")


async def houseconfig_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if chat.type not in ("group", "supergroup"):
        return
    if not await _is_admin(context, chat.id, user.id):
        await _delete_quietly(context.bot, chat.id, message.message_id if message else None)
        return
    settings = await db.get_house_settings(chat.id)
    _house_cfg_draft[chat.id] = {
        "house_enabled": settings["house_enabled"],
        "max_plays_per_day": settings["max_plays_per_day"],
    }
    await context.bot.send_message(
        chat.id,
        _house_cfg_text(_house_cfg_draft[chat.id]),
        parse_mode="HTML",
        reply_markup=_house_cfg_keyboard(_house_cfg_draft[chat.id]),
    )


async def houseconfig_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat = query.message.chat
    user = query.from_user
    if not await _is_admin(context, chat.id, user.id):
        await query.answer("Admins only.", show_alert=True)
        return
    draft = _house_cfg_draft.setdefault(
        chat.id, {"house_enabled": True, "max_plays_per_day": None}
    )
    action = query.data.split(":", 1)[1]
    if action == "toggle":
        draft["house_enabled"] = not draft["house_enabled"]
    elif action == "plus":
        current = draft.get("max_plays_per_day")
        draft["max_plays_per_day"] = 1 if current is None else min(20, current + 1)
    elif action == "minus":
        current = draft.get("max_plays_per_day")
        if current is None or current <= 1:
            draft["max_plays_per_day"] = None
        else:
            draft["max_plays_per_day"] = current - 1
    elif action == "nolimit":
        draft["max_plays_per_day"] = None
    elif action == "save":
        await db.save_house_settings(
            chat.id, draft["house_enabled"], draft.get("max_plays_per_day")
        )
        await query.answer("Saved.")
        await query.edit_message_text(
            "House config saved.",
            reply_markup=None,
        )
        return
    await query.answer()
    await query.edit_message_text(
        _house_cfg_text(draft),
        parse_mode="HTML",
        reply_markup=_house_cfg_keyboard(draft),
    )


_pvp_cfg_draft: dict[int, dict] = {}


def _pvp_cfg_text(draft: dict) -> str:
    def bonus(n: int) -> int:
        return 0 if draft[f"base_{n}"] <= 0 else draft[f"bonus_{n}"]

    return (
        "<b>Points Config</b>\n\n"
        "Per Match + join bonus:\n"
        f"2P[+{draft['base_2']}] bonus [+{bonus(2)}]\n"
        f"3P[+{draft['base_3']}] bonus [+{bonus(3)}]\n"
        f"4P[+{draft['base_4']}] bonus [+{bonus(4)}]\n\n"
        "House Risk/Reward:\n"
        f"Risk[{draft['house_risk']}]|Reward [{draft['house_reward']}]\n\n"
        f"Joker: [{draft['joker_points']}]"
    )


def _pvp_cfg_keyboard(draft: dict):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    def pair(prefix: str) -> list:
        return [
            InlineKeyboardButton("−", callback_data=f"pc:{prefix}-"),
            InlineKeyboardButton("+", callback_data=f"pc:{prefix}+"),
        ]

    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("2P", callback_data="pc:noop")] + pair("b2") + [InlineKeyboardButton("B", callback_data="pc:noop")] + pair("n2"),
            [InlineKeyboardButton("3P", callback_data="pc:noop")] + pair("b3") + [InlineKeyboardButton("B", callback_data="pc:noop")] + pair("n3"),
            [InlineKeyboardButton("4P", callback_data="pc:noop")] + pair("b4") + [InlineKeyboardButton("B", callback_data="pc:noop")] + pair("n4"),
            [InlineKeyboardButton("HRI", callback_data="pc:noop")] + pair("rk") + [InlineKeyboardButton("HRW", callback_data="pc:noop")] + pair("rw"),
            [InlineKeyboardButton("Joker", callback_data="pc:noop")] + pair("jk"),
            [InlineKeyboardButton("Save", callback_data="pc:save")],
        ]
    )


async def pvpconfig_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if chat.type not in ("group", "supergroup"):
        return
    if not await _is_admin(context, chat.id, user.id):
        await _delete_quietly(context.bot, chat.id, message.message_id if message else None)
        return
    draft = await db.get_pvp_settings(chat.id)
    _pvp_cfg_draft[chat.id] = draft
    await context.bot.send_message(
        chat.id,
        _pvp_cfg_text(draft),
        parse_mode="HTML",
        reply_markup=_pvp_cfg_keyboard(draft),
    )


async def pvpconfig_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat = query.message.chat
    user = query.from_user
    if not await _is_admin(context, chat.id, user.id):
        await query.answer("Admins only.", show_alert=True)
        return
    draft = _pvp_cfg_draft.setdefault(chat.id, db._default_pvp_settings())
    action = query.data.split(":", 1)[1]
    keymap = {
        "b2": "base_2",
        "n2": "bonus_2",
        "b3": "base_3",
        "n3": "bonus_3",
        "b4": "base_4",
        "n4": "bonus_4",
        "rk": "house_risk",
        "rw": "house_reward",
        "jk": "joker_points",
    }
    if action == "noop":
        await query.answer()
        return
    if action == "save":
        await db.save_pvp_settings(chat.id, draft)
        await query.answer("Saved.")
        await query.edit_message_text("points config saved.", reply_markup=None)
        return
    field = keymap.get(action[:-1])
    if field:
        step = 1 if action.endswith("+") else -1
        nxt = int(draft.get(field, 0)) + step
        if field == "joker_points":
            nxt = max(-50, min(50, nxt))
        elif field.startswith("base_") or field.startswith("bonus_") or field.startswith("house_"):
            nxt = max(0, min(50, nxt))
        draft[field] = nxt
        if field == "base_2" and nxt <= 0:
            draft["bonus_2"] = 0
        if field == "base_3" and nxt <= 0:
            draft["bonus_3"] = 0
        if field == "base_4" and nxt <= 0:
            draft["bonus_4"] = 0
    await query.answer()
    await query.edit_message_text(
        _pvp_cfg_text(draft),
        parse_mode="HTML",
        reply_markup=_pvp_cfg_keyboard(draft),
    )


async def _cards_lb(chat_id: int) -> str:
    settings = await db.get_pvp_settings(chat_id)
    rows = await db.leaderboard(chat_id)
    return _leaderboard_html(rows, show_house=db.house_rr_active(settings))


async def lb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Use /cardslb in the group.")
        return
    html = await _cards_lb(chat.id)
    await update.message.reply_html(html, disable_web_page_preview=True)


async def showlb_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat = query.message.chat
    html = await _cards_lb(chat.id)
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
                lobby_text(players, game["flavor"], await db.get_pvp_settings(chat_id)),
                parse_mode="HTML",
                reply_markup=lobby_keyboard(
                    game_id,
                    house_available=(await db.house_enabled(chat_id)) and len(players) <= 1,
                ),
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
        lobby_text([], game["flavor"], await db.get_pvp_settings(chat_id)),
        parse_mode="HTML",
        reply_markup=lobby_keyboard(game["id"], house_available=await db.house_enabled(chat_id)),
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