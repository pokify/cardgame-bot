from __future__ import annotations

import asyncio
import logging
import random
from pathlib import Path
from datetime import datetime, timedelta, timezone

from telegram import InputFile, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus
from telegram.ext import ContextTypes
from telegram.error import RetryAfter, TimedOut, NetworkError, TelegramError

from bot import db
from bot.config import AUTO_INTERVAL_SECONDS, GAME_CHAT_IDS, LOBBY_SECONDS, MAX_PLAYERS, MIN_PLAYERS, WIN_POINTS, CARDS
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
    pick_flavor,
    reset_keyboard,
    winner_keyboard,
)
from bot.images import render_deal

log = logging.getLogger(__name__)


async def _send_resilient(bot, method: str, *args, retries: int = 3, **kwargs):
    """Send a Telegram message/photo with retries on rate-limit and transient errors.

    Used for every post-lobby message so a user post (or rate limit) during
    the dealing/outcome window cannot abort the game cycle.
    """
    last_exc = None
    for attempt in range(retries):
        try:
            fn = getattr(bot, method)
            return await fn(*args, **kwargs)
        except RetryAfter as exc:
            last_exc = exc
            wait = float(getattr(exc, "retry_after", 5)) + 0.5
            log.warning("Rate limited on %s (attempt %s/%s), waiting %.1fs", method, attempt + 1, retries, wait)
            await asyncio.sleep(wait)
        except (TimedOut, NetworkError) as exc:
            last_exc = exc
            wait = 1.5 * (attempt + 1)
            log.warning("Transient error on %s (attempt %s/%s): %s", method, attempt + 1, retries, exc)
            await asyncio.sleep(wait)
        except TelegramError as exc:
            # Non-retryable Telegram error (bad request, etc.) — log and give up on this send
            log.exception("Non-retryable Telegram error on %s: %s", method, exc)
            return None
        except Exception as exc:
            last_exc = exc
            log.exception("Unexpected error on %s (attempt %s/%s)", method, attempt + 1, retries)
            await asyncio.sleep(1.0)
    log.error("Failed to %s after %s attempts: %s", method, retries, last_exc)
    return None


BUMP_SECONDS = 30
RESET_TIMEOUT = 12
NEW_GAME_IMAGE = Path(__file__).resolve().parent.parent / "assets" / "gameimages" / "newgame.png"

_TOMOCHI_HOUSE_EMOJI_IDS = ("5933745718857574033", "5962888046918506770")
_tomochi_house_emoji_index = 0

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


async def _effective_game_mode(chat_id: int) -> dict:
    mode = await db.get_game_mode(chat_id)
    if mode and (mode.get("active") or mode.get("pending")):
        return mode
    return {"mode": "luck", "active": True, "pending": False, "target": 0, "days": 0,
            "bump_alert_minutes": await db.tomochi_luck_bump_minutes(chat_id)}


_tomochi_luck_previous_ranks: dict[int, dict[int, int]] = {}
_tomochi_luck_rank_snapshot_initialized: set[int] = set()


async def _tomochi_luck_lb(chat_id: int) -> str:
    if not await db.tomochi_luck_leaderboard_visible(chat_id):
        return "<b>Tomochi Luck leaderboard will return after the next completed Tomochi Luck game.</b>"
    rank_limit = await db.get_leaderboard_rank_limit(chat_id)
    rows = await db.tomochi_luck_leaderboard(chat_id, rank_limit if rank_limit > 0 else 10000)
    if not await db.tomochi_luck_house_on_lb(chat_id):
        rows = [row for row in rows if str(row.get("username") or "").lower() != "house" and int(row.get("user_id") or 0) != 0]
    lines = [
        "<b>Tomochi Luck Leaderboard</b>",
        "",
        'Who’s the luckiest of them all?<tg-emoji emoji-id="5933961609683673255">😏</tg-emoji>',
        "",
    ]
    luck_rank_emojis = {
        1: ("🍀", "5936069085711310532"),
        2: ("🎰", "5934046559841820192"),
        3: ("✌️", "5936143423005269735"),
        4: ("🧲", "5936076971271266289"),
        5: ("😌", "5962918390862453991"),
        6: ("🎻", "5934055407474449435"),
        7: ("🤕", "5935798193534016200"),
        8: ("💘", "5933989045934776675"),
        9: ("💀", "5936276811804581914"),
        10: ("🎭", "5935885467269472061"),
    }
    last_rank = len(rows)
    current_ranks = {int(row.get("user_id") or 0): n for n, row in enumerate(rows, 1)}
    # First snapshot after this update: nobody has prev_rank yet, so don't mark everyone NEW.
    baseline = not any(row.get("prev_rank") is not None for row in rows)
    for n, row in enumerate(rows, 1):
        user_id = int(row.get("user_id") or 0)
        raw_name = "House" if user_id == 0 else (row.get("username") or row.get("first_name") or user_id)
        name = str(raw_name).replace("&", "&").replace("<", "<").replace(">", ">").lstrip("@")
        if user_id == 0:
            emoji_fallback, emoji_id = ("🧛‍♂️", "5936040004487749083")
        elif n >= 10 or (n == last_rank and n != 1):
            emoji_fallback, emoji_id = ("🎭", "5935885467269472061")
        else:
            emoji_fallback, emoji_id = luck_rank_emojis.get(n, luck_rank_emojis[10])
        rank_emoji = f'<tg-emoji emoji-id="{emoji_id}">{emoji_fallback}</tg-emoji>'
        old_rank = row.get("prev_rank")
        new_player_emoji = '<tg-emoji emoji-id="5911212056974991641">🆕</tg-emoji>'
        if old_rank is None and not baseline:
            # Truly new player since the last snapshot.
            movement_prefix = f"{new_player_emoji} "
        elif old_rank is None:
            # Baseline snapshot — no arrow.
            movement_prefix = "     "
        elif int(old_rank) > n:
            movement_prefix = "↑ "
        elif int(old_rank) < n:
            movement_prefix = "↓ "
        else:
            movement_prefix = "     "
        lines.append(
            f"     {n}. <b>{name}</b>\n{movement_prefix}{rank_emoji} W:{int(row['wins'])} | P:{int(row['played'])} | W rate: {float(row['win_rate']):g}%"
        )
    if not rows:
        lines.append("No games played yet.")
    # Persist current ranks so arrows survive bot restarts.
    await db.set_tomochi_luck_prev_ranks(chat_id, current_ranks)
    return "\n".join(lines)



def _plain_player_name(player) -> str:
    return str(player.get("username") or player.get("first_name") or "player").lstrip("@").replace("<", "").replace(">", "")


async def _lobby_text_for_chat(chat_id: int, players: list, flavor: str | None) -> str:
    settings = await db.get_pvp_settings(chat_id)
    mode = await _effective_game_mode(chat_id)
    settings = dict(settings or {})
    settings["_game_mode"] = mode
    settings["_min_players"] = (await db.get_gamemode_config(chat_id))["min_players"]
    # Hide the auto-play House line in Tomochi Luck lobby when house is disabled
    if mode.get("mode") == "luck":
        settings["_house_enabled"] = await db.tomochi_luck_house_enabled(chat_id)
    return lobby_text(players, flavor, settings)


async def _render_deal_for_chat(chat_id: int, assignments: list[dict]):
    theme = await db.get_table_colour(chat_id)
    return render_deal(assignments, theme=theme)


async def _schedule_expire(context: ContextTypes.DEFAULT_TYPE, game_id: int, chat_id: int) -> None:
    jq = context.job_queue
    name = f"expire:{game_id}"
    for job in jq.get_jobs_by_name(name):
        job.schedule_removal()
    cfg = await db.get_gamemode_config(chat_id)
    jq.run_once(
        expire_job,
        when=int(cfg["lobby_minutes"]) * 60,
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

async def _delete_after_seconds(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int | None,
    seconds: int,
    name: str,
) -> None:
    if not message_id:
        return
    context.job_queue.run_once(
        delete_message_job,
        when=max(1, int(seconds)),
        data={"chat_id": chat_id, "message_id": message_id},
        name=name,
        chat_id=chat_id,
    )


async def delete_message_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data or {}
    await _delete_quietly(
        context.bot,
        data.get("chat_id"),
        data.get("message_id"),
    )


# Telegram custom emoji IDs used by the leaderboard.
LB_RANK_UP = "5908854201533867501"
LB_RANK_DOWN = "5908957778965177126"
LB_CROWN = "5962786385042610279"
LB_NEW = "5911212056974991641"
LB_WAVE = "5881746330761568743"
LB_NON_MOVER = "5883953450030472158"
LB_LAST = "5882040570381081275"

LB_UP = {
    1: "5882040965518074383",
    2: "5962888046918506770",
    3: "5884318255962660623",
    4: "5933527315475601401",
    5: "5963084567442102698",
    6: "5962984795351816242",
    7: "5963323595257026790",
    8: "5962823192912336897",
    9: "5962825168597293123",
    10: "5962923265650334847",
}
LB_DOWN = {
    1: "5882178030809391269",
    2: "5881869398754467619",
    3: "5882216496536493419",
    4: "5934000315928942445",
    5: "5933873287976208902",
    6: "5962930348051404923",
    7: "5962930348051404923",
    8: "5962930348051404923",
    9: "5962930348051404923",
    10: "5962930348051404923",
}

SUPERSCRIPTS = {
    1: "¹", 2: "²", 3: "³", 4: "⁴", 5: "⁵",
    6: "⁶", 7: "⁷", 8: "⁸", 9: "⁹", 10: "¹⁰",
}


def _tg_emoji(emoji_id: str, fallback: str) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


def _movement_prefix(movement: int, lb_id: str, lb_fallback: str, position: str) -> str:
    if movement:
        amount = min(10, abs(movement))
        sign = "+" if movement > 0 else "-"
        superscript = SUPERSCRIPTS[amount]
        superscript_text = f"⁺{superscript}" if sign == "+" else f"⁻{superscript}"
        return (
            f"{superscript_text}"
            f"{_tg_emoji(lb_id, lb_fallback)}"
            f"{position}"
        )
    # U+2007 FIGURE SPACE keeps the LB emoji aligned with rows that have
    # a superscript, without inserting an HTML entity or a normal space.
    return f"\u2007{_tg_emoji(lb_id, lb_fallback)}{position}"


def _leaderboard_name(row) -> str:
    username = row["username"]
    if username:
        return str(username).lstrip("@").replace("<", "").replace(">", "")
    return str(row["first_name"] or "player").replace("<", "").replace(">", "")


def _leaderboard_html(rows, show_house: bool = True) -> str:
    if not rows:
        return "No games yet. Start one with /cards."

    lines = ["<b>Tomochi Cards Leaderboard</b>", ""]
    lines.append("(<b>S</b>core) (<b>H</b>ouse) (<b>W</b>ins) (<b>P</b>layed)")
    lines.append("")

    last_rank = len(rows)
    for i, row in enumerate(rows, start=1):
        prev_rank = row["prev_rank"]
        score = int(row["score"])
        prev_rank_int = int(prev_rank) if prev_rank is not None else None

        if prev_rank_int is None:
            movement = 0
            is_new = True
        else:
            # Positive means the player moved up (e.g. 5 -> 2).
            movement = prev_rank_int - i
            is_new = False

        # The top spot always gets the crown. If they moved into #1, the
        # superscript still shows how far they climbed.
        if i == 1:
            lb_id, lb_fallback = LB_CROWN, "👑"
        elif i == last_rank:
            lb_id, lb_fallback = LB_LAST, "😵"
        elif is_new:
            lb_id, lb_fallback = LB_WAVE, "👋"
        elif movement > 0:
            amount = min(10, movement)
            lb_id, lb_fallback = LB_UP[amount], "⚡️"
        elif movement < 0:
            amount = min(10, -movement)
            lb_id, lb_fallback = LB_DOWN[amount], "🪖"
        else:
            lb_id, lb_fallback = LB_NON_MOVER, "🥱"

        if is_new and i == 1:
            prefix = f"{_tg_emoji(LB_CROWN, '👑')}{_tg_emoji(LB_NEW, '🆕')}"
        elif is_new and i == last_rank:
            prefix = f"\u2007{_tg_emoji(LB_LAST, '😵')}{_tg_emoji(LB_NEW, '🆕')}"
        elif is_new:
            # New players use the waving LB emoji followed immediately by
            # the Telegram new-player emoji instead of an up/down/<> marker.
            prefix = f"\u2007{_tg_emoji(LB_WAVE, '👋')}{_tg_emoji(LB_NEW, '🆕')}"
        else:
            if movement > 0:
                position = _tg_emoji(LB_RANK_UP, "⬆️")
            elif movement < 0:
                position = _tg_emoji(LB_RANK_DOWN, "⬇️")
            else:
                position = "&lt;&gt;"
            prefix = _movement_prefix(movement, lb_id, lb_fallback, position)

        who = _leaderboard_name(row)
        wins = int(row["wins"])
        played = int(row["played"])
        house_pts = int(row["house_points"] or 0)
        stats = f"Sc:{score} (H:{house_pts}) | W:{wins} | P:{played}"
        lines.append(f"<b>    {i}.</b> {who}\n{prefix}<b>{stats}</b>")

    return "\n".join(lines)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type == "private":
        args = context.args or []
        if args and args[0].startswith("gamemode_"):
            raw_gid = args[0][len("gamemode_"):]
            try:
                gid = int(raw_gid)
            except ValueError:
                gid = None

            if gid is not None and await _is_admin(
                context, gid, update.effective_user.id
            ):
                context.user_data["gamemode_chat_id"] = gid
                mode = await db.get_game_mode(gid)
                await update.message.reply_html(
                    _gm_menu_text(mode),
                    reply_markup=_gm_menu_keyboard(gid, mode),
                )
                return

            await update.message.reply_text("Admins only.")
            return

        await update.message.reply_text(
            "Add me to a group, then use /cards to start a lobby and /cardslb for scores."
        )
        return
    await cards_cmd(update, context)


async def _join_existing_lobby(update: Update, context: ContextTypes.DEFAULT_TYPE, game) -> None:
    """Add the caller to an open lobby and jump the chat to the lobby message."""
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    game_id = game["id"]
    lock = await _lobby_lock(game_id)

    async with lock:
        ok, reason, _ = await db.add_player(
            game_id, user.id, user.username, user.first_name
        )
        players = await db.list_players(game_id)
        fresh = await db.get_game(game_id)
        mid = fresh["message_id"] if fresh else game["message_id"]

        if not ok and reason not in ("already_in",):
            query = update.callback_query
            if reason == "full":
                msg = "Lobby is full."
            elif reason == "not_waiting":
                msg = "A game is already in progress. Join next game."
            else:
                msg = "Can't join the lobby right now."
            if query is not None:
                try:
                    await query.answer(msg, show_alert=True)
                except Exception:
                    if message:
                        await message.reply_text(msg)
            elif message:
                await message.reply_text(msg)
            return

        # Telegram cannot force a client to scroll to an older message.
        # Bump the lobby to the bottom of the chat so it becomes the latest
        # message the user sees after pressing Play.
        who = f"@{user.username}" if user.username else (user.first_name or "player")
        try:
            house_on = await db.house_enabled(chat.id)
            lobby_text = await _lobby_text_for_chat(chat.id, players, game["flavor"])
            markup = lobby_keyboard(
                game_id,
                house_available=house_on and len(players) <= 1,
                luck_mode=(await _effective_game_mode(chat.id)).get("mode") == "luck",
            )
            new_msg = await context.bot.send_message(
                chat.id,
                lobby_text,
                parse_mode="HTML",
                reply_markup=markup,
                disable_web_page_preview=True,
            )
            await db.set_message_id(game_id, new_msg.message_id)
            if mid and mid != new_msg.message_id:
                await _delete_quietly(context.bot, chat.id, mid)
        except Exception:
            log.exception("Could not bump lobby after play-button join")
            # Fallback: still tell the user they joined.
            if message:
                await message.reply_text(f"{who} joined the lobby.")

        # Alert popup when added via Play button (callback).
        query = update.callback_query
        if query is not None:
            try:
                await query.answer(f"{who} added to current game.", show_alert=True)
            except Exception:
                log.exception("Could not show join alert")

        # Start immediately if the lobby just filled.
        if ok and len(players) >= MAX_PLAYERS:
            # Release lock before starting the game.
            pass
        else:
            return

    if len(players) >= MAX_PLAYERS:
        await _run_game(context, game_id, chat.id)


async def cards_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if chat.type not in ("group", "supergroup"):
        await message.reply_text("Start a game in a group with /cards.")
        return

    existing = await db.active_game(chat.id)
    if existing:
        if existing["status"] == "waiting":
            await _join_existing_lobby(update, context, existing)
            return
        # Game has left the lobby (running or other active state).
        query = update.callback_query
        if query is not None:
            try:
                await query.answer("A game is already in progress. Join next game.", show_alert=True)
            except Exception:
                log.exception("Could not show in-progress alert")
                if message:
                    await message.reply_text("A game is already in progress. Join next game.")
        else:
            # Direct /cards while a game is already running: delete the command
            # and send no reply.
            if message:
                await _delete_quietly(context.bot, chat.id, message.message_id)
        return

    # Plain answer for Play button when we are creating a fresh lobby
    # (alerts are only used for join / already-in-progress).
    query = update.callback_query
    if query is not None:
        try:
            await query.answer()
        except Exception:
            pass

    standing = await db.caller_standing(chat.id, user.id)
    flavor = pick_flavor(standing)
    lobby_cfg = await db.get_gamemode_config(chat.id)
    game = await db.create_game(chat.id, "manual", expires_at(seconds=int(lobby_cfg["lobby_minutes"]) * 60), flavor=flavor)
    ok, _, players = await db.add_player(
        game["id"], user.id, user.username, user.first_name
    )
    if not ok:
        await db.set_status(game["id"], "expired")
        await message.reply_text("Could not create the lobby. Try again.")
        return

    mode_was_pending = False
    mode_before = await db.get_game_mode(chat.id)
    if mode_before and mode_before.get("pending") and not mode_before.get("active"):
        activated = await db.activate_game_mode(chat.id)
        mode_was_pending = bool(activated)
        if activated and activated.get("mode") == "highest":
            _schedule_mode_end(context, chat.id, activated)

    try:
        house_on = await db.house_enabled(chat.id)
    except Exception:
        log.exception("Could not load House availability while creating lobby")
        house_on = False

    try:
        msg = await message.reply_html(
            await _lobby_text_for_chat(chat.id, players, game["flavor"]),
            reply_markup=lobby_keyboard(game["id"], house_available=house_on, luck_mode=(await _effective_game_mode(chat.id)).get("mode") == "luck"),
            disable_web_page_preview=True,
        )
    except Exception:
        log.exception("Could not send cards lobby message")
        await db.set_status(game["id"], "expired")
        if mode_was_pending:
            await db.reset_game_mode_for_restart(chat.id)
        await message.reply_text("Could not create the lobby. Try again.")
        return

    await db.set_message_id(game["id"], msg.message_id)
    await _schedule_expire(context, game["id"], chat.id)


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
            text = await _lobby_text_for_chat(chat.id, players, game["flavor"])
            house_on = await db.house_enabled(chat.id)
            markup = lobby_keyboard(game_id, house_available=house_on and len(players) <= 1, luck_mode=(await _effective_game_mode(chat.id)).get("mode") == "luck")

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
            f"House points: lose -{risk}/win +{reward}\nDaily challenge limit: {max_plays} \n"
            f"{who} {remaining} remaining \n\nClick Challenge House to continue"
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


async def _house_confirmation_alert(chat_id: int, user, max_plays, pvp, remaining) -> str:
    mode = await db.get_game_mode(chat_id)
    if mode and (mode.get("active") or mode.get("pending")) and mode.get("mode") in ("first", "highest"):
        cfg = await db.get_gamemode_config(chat_id)
        risk = int(cfg.get("default_house_risk") or 0)
        reward = int(cfg.get("default_house_reward") or 0)
        standings = await db.leaderboard(chat_id, 10000)
        scores = {int(r["user_id"]): int(r.get("score") or 0) for r in standings}
        own_score = scores.get(int(user.id), 0)
        leader_score = max(scores.values(), default=own_score)
        gap = max(0, leader_score - own_score)
        for boost in await db.get_house_gap_boosts(chat_id):
            start, end = int(boost.get("gap_from") or 0), int(boost.get("gap_to") or 0)
            br, bw = int(boost.get("risk") or 0), int(boost.get("reward") or 0)
            if end > 0 and start > 0 and start <= gap <= end and (br > 0 or bw > 0):
                return (
                    f"House gap boost offer:\n"
                    f"-{br} risk/reward +{bw} win\n"
                    "Click Challenge House to continue."
                )
        return (
            f"Fixed House points: -{risk} loss/+{reward} win\n"
            "Click Challenge House to continue."
        )
    return _house_confirm_text(user, max_plays, pvp, remaining)


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
    active_mode = await db.get_game_mode(group_chat.id)
    if active_mode and (active_mode.get("active") or active_mode.get("pending")) and active_mode.get("mode") in ("first", "highest"):
        house_cfg = await db.get_gamemode_config(group_chat.id)
        if not house_cfg["house_points_enabled"]:
            await query.answer("House points challenges are disabled for this game mode.", show_alert=True)
            return
        disable_gap = int(house_cfg["leader_disable_gap"])
        if disable_gap > 0:
            standings = await db.leaderboard(group_chat.id, 10000)
            scores = sorted([(int(r["score"] or 0), int(r["user_id"])) for r in standings], reverse=True)
            own_score = next((score for score, uid in scores if uid == user.id), 0)
            next_score = max((score for score, uid in scores if uid != user.id), default=own_score)
            if own_score - next_score >= disable_gap and own_score >= next_score:
                await query.answer("House challenges are temporarily disabled for the leader.", show_alert=True)
                return

    required_pvp = max(0, int(settings.get("pvp_before_unlock", 1)))
    pvp_played = await db.pvp_games_played(group_chat.id, user.id)
    if pvp_played < required_pvp:
        game_word = "game" if required_pvp == 1 else "games"
        await query.answer(
            f"You must complete at least {required_pvp} PvP {game_word} "
            "(2–4 players) in this group before you can challenge the House.",
            show_alert=True,
        )
        return

    max_plays = settings["max_plays_per_day"]
    pvp = await db.get_pvp_settings(group_chat.id)
    rr_on = db.house_rr_active(pvp)
    if active_mode and (active_mode.get("active") or active_mode.get("pending")) and active_mode.get("mode") in ("first", "highest"):
        # First/Highest modes always show the configured fixed/gap House offer confirmation.
        rr_on = True

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
            await _house_confirmation_alert(group_chat.id, user, max_plays, pvp, remaining),
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
    global _tomochi_house_emoji_index
    house_emoji_id = _TOMOCHI_HOUSE_EMOJI_IDS[_tomochi_house_emoji_index % len(_TOMOCHI_HOUSE_EMOJI_IDS)]
    _tomochi_house_emoji_index += 1

    # House risk/reward is configured separately from PvP points for the
    # Highest Score and First-to-X modes. Tomochi Luck retains its own rules.
    mode_cfg = await db.get_game_mode(chat_id)
    game_mode = mode_cfg.get("mode") if mode_cfg and (mode_cfg.get("active") or mode_cfg.get("pending")) else "luck"
    house_cfg = await db.get_gamemode_config(chat_id)
    house_risk = house_cfg["default_house_risk"]
    house_reward = house_cfg["default_house_reward"]
    gap = 0
    boosted = False
    if game_mode in ("first", "highest") and house_cfg["house_points_enabled"]:
        standings = await db.leaderboard(chat_id, 10000)
        leader_score = max((int(r["score"] or 0) for r in standings), default=0)
        player_score = next((int(r["score"] or 0) for r in standings if int(r["user_id"]) == int(user_id)), 0)
        gap = max(0, leader_score - player_score)
        for boost in await db.get_house_gap_boosts(chat_id):
            if int(boost["gap_to"]) > 0 and int(boost["gap_from"]) <= gap <= int(boost["gap_to"]):
                house_risk, house_reward = int(boost["risk"]), int(boost["reward"])
                boosted = True
                break
    else:
        pvp_cfg = await db.get_pvp_settings(chat_id)
        house_risk, house_reward = int(pvp_cfg["house_risk"]), int(pvp_cfg["house_reward"])

    if game_mode in ("first", "highest"):
        if boosted:
            announcement = (f"{player_tag} has challenged\nthe house! <tg-emoji emoji-id=\"{house_emoji_id}\">⚔️</tg-emoji>\n\n"
                            f"-{gap} points behind leader\n\nHouse boost offer:\nRisk: -{house_risk} / Reward: +{house_reward}")
        else:
            announcement = (f"{player_tag} has challenged\nthe house! <tg-emoji emoji-id=\"{house_emoji_id}\">⚔️</tg-emoji>\n\n"
                            f"Fixed House points :\nRisk: -{house_risk} / Reward: +{house_reward}")
    else:
        announcement = f"{player_tag} has challenged\nthe house! <tg-emoji emoji-id=\"{house_emoji_id}\">⚔️</tg-emoji>"
    await _send_resilient(context.bot, "send_message", chat_id, announcement, parse_mode="HTML", disable_web_page_preview=True)

    await _send_resilient(context.bot, "send_message", chat_id, "Dealing...")
    await asyncio.sleep(5)

    banned = await db.banned_cards(chat_id, "house")
    assignments, winner, _house = deal_house(player, banned)
    await db.record_deal_memory(chat_id, "house", [a["card_key"] for a in assignments])

    photo = await _render_deal_for_chat(chat_id, assignments)
    await _send_resilient(
        context.bot, "send_photo",
        chat_id,
        photo=InputFile(photo, filename="house_challenge.png"),
    )

    player_won = winner["user_id"] == player["user_id"]
    pvp = await db.get_pvp_settings(chat_id)
    if game_mode in ("first", "highest"):
        rr_on = bool(house_cfg["house_points_enabled"] and (house_risk or house_reward))
        risk_value, reward_value = house_risk, house_reward
    else:
        rr_on = db.house_rr_active(pvp)
        risk_value, reward_value = int(pvp["house_risk"]), int(pvp["house_reward"])
    delta = 0
    if rr_on:
        delta = reward_value if player_won else -risk_value

    # House challenges count toward the same combined leaderboard W/P totals
    # as PvP. Score and H are both changed by the House result.
    #
    # Keep the game recoverable even if a leaderboard/mode database operation
    # fails: the House outcome must still be posted and the game must not stay
    # stuck in "running".
    try:
        await db.apply_house_result(
            chat_id,
            player,
            delta,
            won=player_won,
        )
    except Exception:
        log.exception("Could not record House result for game %s", game_id)

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

    # Mark the game finished before optional mode bookkeeping so a failed
    # operation can never leave /cards blocked by a stale running game.
    await db.set_status(game_id, "finished")

    await _send_resilient(
        context.bot, "send_message",
        chat_id,
        result,
        parse_mode="HTML",
        reply_markup=_blank_outcome_markup(),
        disable_web_page_preview=True,
    )

    # House results participate in active game modes, but game mode is
    # completely optional and must never be required for a House outcome.
    try:
        mode = await db.get_game_mode(chat_id)
        if mode and mode.get("active") and not mode.get("winner_user_id"):
            await db.game_mode_participation(chat_id, mode["mode"], [player])
            if mode.get("mode") == "first":
                winner_mode = await db.check_first_to_winner(chat_id)
                if winner_mode:
                    await _mode_announce_winner(context, chat_id, winner_mode)
            elif mode.get("mode") == "highest":
                ends_at = mode.get("ends_at")
                if ends_at and ends_at <= datetime.now(timezone.utc):
                    winner_mode = await db.highest_score_winner(chat_id)
                    if winner_mode and winner_mode.get("winner_user_id"):
                        await _mode_announce_winner(context, chat_id, winner_mode)
    except Exception:
        log.exception("Could not process House game-mode bookkeeping for game %s", game_id)

    await _schedule_game_bump(context, chat_id)
    await asyncio.sleep(5)
    lb_text = await _cards_lb(chat_id)
    await _send_resilient(
        context.bot, "send_message",
        chat_id, lb_text, parse_mode="HTML",
        reply_markup=_leaderboard_markup(
            (await db.get_game_mode(chat_id) or {}).get("mode")
            if (await db.get_game_mode(chat_id) or {}).get("active") else None
        ),
        disable_web_page_preview=True,
    )


async def expire_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data or {}
    game_id = data["game_id"]
    chat_id = data["chat_id"]
    game = await db.get_game(game_id)
    if game is None or game["status"] != "waiting":
        return

    players = await db.list_players(game_id)
    min_required = (await db.get_gamemode_config(chat_id))["min_players"]
    if len(players) < min_required:
        mode = await db.get_game_mode(chat_id)
        luck_default = (not mode or not (mode.get("active") or mode.get("pending")) or mode.get("mode") == "luck")
        if luck_default and len(players) == 1 and await db.tomochi_luck_house_enabled(chat_id):
            await _run_game(context, game_id, chat_id)
            return
        await db.set_status(game_id, "expired")
        _cancel_chat_jobs(context, chat_id, game_id)
        await _delete_quietly(context.bot, chat_id, game["message_id"])
        return

    await _run_game(context, game_id, chat_id)


async def _run_tomochi_luck(context, game_id: int, chat_id: int, players: list[dict]) -> None:
    from bot.config import CARDS
    import random
    global _tomochi_house_emoji_index
    is_house = len(players) == 1
    if is_house:
        house_emoji_id = _TOMOCHI_HOUSE_EMOJI_IDS[
            _tomochi_house_emoji_index % len(_TOMOCHI_HOUSE_EMOJI_IDS)
        ]
        _tomochi_house_emoji_index += 1
        started_text = (
            "Game started!\n\n"
            f"{mention(players[0].get('username'), players[0].get('first_name'), players[0].get('user_id'))} "
            f"vs The House <tg-emoji emoji-id=\"{house_emoji_id}\">⚔️</tg-emoji>"
        )
    else:
        started_text = "Game started!\n\nPlayers: " + ", ".join(
            mention(p.get('username'), p.get('first_name'), p.get('user_id')) for p in players
        )
    await _send_resilient(context.bot, "send_message", chat_id, started_text, parse_mode="HTML")
    await _send_resilient(context.bot, "send_message", chat_id, "Dealing...")
    await asyncio.sleep(5)

    joker_cfg = await db.tomochi_luck_joker_settings(chat_id)
    chance = joker_cfg["chance"]
    allow_house_joker = joker_cfg["in_house"]
    penalty = joker_cfg["wins_penalty"]  # negative number, e.g. -1

    # 2-player (or 1vHouse treated as 2-card) Joker cooldown override: 5% for next 2 deals after a Joker
    is_two_card = is_house or len(players) == 2
    cooldown = await db.get_joker_cooldown(chat_id) if is_two_card else 0
    if cooldown > 0:
        chance = 5  # overrides configured joker_chance

    # Roll for joker inclusion (Tomochi Luck exclusive)
    include_joker = (chance > 0) and (random.random() * 100 < chance)
    if is_house and not allow_house_joker:
        include_joker = False

    # Banned cards only for 2-player sequences (including 1vHouse as consecutive 2-card deals)
    banned = []
    if is_house or len(players) == 2:
        banned = await db.banned_cards(chat_id, "luck2")

    base_pool = [k for k in CARDS if k != "joker" and k not in banned]
    if len(base_pool) < (len(players) + (1 if is_house else 0)):
        base_pool = [k for k in CARDS if k != "joker"]  # fallback if too many banned

    n_cards = len(players) + (1 if is_house else 0)
    if include_joker and len(base_pool) >= n_cards - 1:
        # Force one joker into the deal, rest from normal cards, but never let joker be the unique highest
        normal_needed = n_cards - 1
        normal_keys = random.sample(base_pool, normal_needed)
        keys = normal_keys + ["joker"]
        random.shuffle(keys)
    else:
        keys = random.sample(base_pool, n_cards)

    assignments = []
    for i, player in enumerate(players):
        key = keys[i]
        face = CARDS[key]["score"]
        assignments.append({
            "user_id": player["user_id"],
            "username": player.get("username"),
            "first_name": player.get("first_name"),
            "card_key": key,
            "score": face,
            "display_score": face,
            "label": CARDS[key]["label"],
        })
    if is_house:
        key = keys[-1]
        face = CARDS[key]["score"]
        assignments.append({
            "user_id": -1,
            "username": None,
            "first_name": "House",
            "card_key": key,
            "score": face,
            "display_score": face,
            "label": CARDS[key]["label"],
        })

    # Winner is always the highest non-joker card (joker can never win the hand)
    contenders = [a for a in assignments if a["card_key"] != "joker"]
    if not contenders:
        contenders = assignments  # safety
    winner = max(contenders, key=lambda a: int(a["score"]))

    # Record memory for 2-card deals so ban works across user-vs-user 2p and user-vs-house
    if is_house or len(players) == 2:
        await db.record_deal_memory(chat_id, "luck2", [a["card_key"] for a in assignments])
        joker_appeared = any(a.get("card_key") == "joker" for a in assignments)
        await db.update_joker_cooldown(chat_id, joker_appeared)

    # For Tomochi Luck joker, replace the bottom "Score: -5" with the win penalty text.
    for a in assignments:
        if a.get("card_key") == "joker":
            a["bottom_text"] = f"{penalty} Win"
    await _send_resilient(context.bot, "send_photo", chat_id, photo=InputFile(await _render_deal_for_chat(chat_id, assignments), filename="deal.png"))
    await db.save_deal(game_id, assignments, winner["user_id"])

    winner_id = 0 if int(winner["user_id"]) == -1 else int(winner["user_id"])

    # Apply joker penalty to whoever drew it (can be player or House)
    joker_holder = next((a for a in assignments if a["card_key"] == "joker"), None)
    penalties = {}
    if joker_holder is not None:
        hid = 0 if int(joker_holder["user_id"]) == -1 else int(joker_holder["user_id"])
        penalties[hid] = penalty

    await db.tomochi_luck_record_game(chat_id, players, winner_id, penalties)
    await db.set_tomochi_luck_leaderboard_visible(chat_id, True)

    # Outcome messages
    joker_line = ""
    if joker_holder is not None:
        if int(joker_holder["user_id"]) == -1:
            joker_tag = "House"
        else:
            joker_tag = mention(joker_holder.get("username"), joker_holder.get("first_name"), joker_holder.get("user_id"))
        joker_line = f"\n\nNyaaaaaa-<tg-emoji emoji-id=\"5936079376452951179\">🤪</tg-emoji>\n{joker_tag}, Joker pulled: {penalty} Win"

    if winner_id == 0:
        player = players[0] if players else {}
        player_tag = mention(player.get("username"), player.get("first_name"), player.get("user_id", 0))
        if joker_holder is not None:
            msg = f"Highest card: House\n\nNyaaaaaa-<tg-emoji emoji-id=\"5936079376452951179\">🤪</tg-emoji>\n{player_tag}, Joker pulled: {penalty} Win"
        else:
            msg = f"Highest card: House\n\n{player_tag} You lose!\nNever bet against the House! Better luck next time! <tg-emoji emoji-id='5936040004487749083'>🧛‍♂️</tg-emoji>"
    else:
        stats = next((r for r in await db.tomochi_luck_leaderboard(chat_id, 10000)
                      if int(r["user_id"]) == int(winner_id)), None)
        wins = int(stats["wins"]) if stats else 0
        rate = float(stats["win_rate"]) if stats else 0.0
        msg = (
            f"Highest card: {mention(winner.get('username'), winner.get('first_name'), winner.get('user_id'))}\n"
            f"Wins: {wins}\n"
            f"Win rate: {rate:g}%{joker_line}"
        )
    await _send_resilient(context.bot, "send_message", chat_id, msg, parse_mode="HTML", reply_markup=_blank_outcome_markup())

    await asyncio.sleep(5)
    await _send_resilient(
        context.bot, "send_message",
        chat_id, await _tomochi_luck_lb(chat_id), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Play", callback_data="playcards")]]),
    )
    await _schedule_game_bump(context, chat_id)


async def _run_game(context: ContextTypes.DEFAULT_TYPE, game_id: int, chat_id: int) -> None:
    game = await db.get_game(game_id)
    if game is None or game["status"] != "waiting":
        return

    players = await db.list_players(game_id)
    current_mode = await db.get_game_mode(chat_id)
    min_required = (await db.get_gamemode_config(chat_id))["min_players"]
    luck_default = (not current_mode or not (current_mode.get("active") or current_mode.get("pending")) or current_mode.get("mode") == "luck")
    if len(players) < min_required:
        if not (luck_default and len(players) == 1 and await db.tomochi_luck_house_enabled(chat_id)):
            await db.set_status(game_id, "expired")
            return

    await db.set_status(game_id, "running")
    _cancel_chat_jobs(context, chat_id, game_id)

    if game["message_id"]:
        await _delete_quietly(context.bot, chat_id, game["message_id"])

    current_mode = await db.get_game_mode(chat_id)
    if not current_mode or not (current_mode.get("active") or current_mode.get("pending")):
        try:
            await _run_tomochi_luck(context, game_id, chat_id, players)
        finally:
            # Tomochi Luck path is responsible for its own messages; we still
            # guarantee the game cannot stay stuck in "running".
            try:
                await db.set_status(game_id, "finished")
            except Exception:
                log.exception("Could not finalize Tomochi Luck game %s", game_id)
        return

    try:
        names = ", ".join(mention(p["username"], p["first_name"], p["user_id"]) for p in players)
        pvp = await db.get_pvp_settings(chat_id)
        base_points, join_bonus = pvp_base_bonus_for(pvp, len(players))
        if join_bonus > 0:
            points_line = f"Points to win: {base_points} (+{join_bonus})"
        else:
            points_line = f"Points to win: {base_points}"
        await _send_resilient(
            context.bot, "send_message",
            chat_id,
            f"Game started! ({len(players)} players)\n\n{points_line}\n\nPlayers: {names}",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

        await _send_resilient(context.bot, "send_message", chat_id, "Dealing...")
        await asyncio.sleep(5)

        banned = await db.banned_cards(chat_id, "pvp2") if len(players) == 2 else []
        # 2-player Joker cooldown: after a Joker, next 2 deals have only 5% chance (overrides normal equal-pool odds)
        if len(players) == 2:
            cooldown = await db.get_joker_cooldown(chat_id)
            if cooldown > 0:
                # Force joker out of the normal pool and decide inclusion with 5% roll
                banned = list(set(banned) | {"joker"})
                include_joker = random.random() * 100 < 5
            else:
                include_joker = None  # let deal() decide normally
        else:
            include_joker = None

        if current_mode and current_mode.get("mode") in ("first", "highest"):
            mode_joker = await db.get_mode_joker_settings(chat_id, current_mode["mode"])
            include_joker = random.random() * 100 < int(mode_joker["chance"])
            if not include_joker:
                banned = list(set(banned) | {"joker"})

        if include_joker is True:
            # Build assignments manually with forced joker chance
            pool = [k for k in CARDS if k != "joker" and k not in banned]
            if len(pool) < len(players) - 1:
                pool = [k for k in CARDS if k != "joker"]
            normal_keys = random.sample(pool, len(players) - 1)
            keys = normal_keys + ["joker"]
            random.shuffle(keys)
            assignments = []
            for player, key in zip(players, keys):
                face = CARDS[key]["score"]
                assignments.append({
                    "user_id": player["user_id"],
                    "username": player["username"],
                    "first_name": player["first_name"],
                    "card_key": key,
                    "score": face,
                    "display_score": -5 if key == "joker" else face,
                    "label": CARDS[key]["label"],
                })
            joker = next((a for a in assignments if a["card_key"] == "joker"), None)
            contenders = [a for a in assignments if a["card_key"] != "joker"]
            winner = max(contenders, key=lambda a: a["score"])
        else:
            assignments, winner, joker = deal(players, banned)

        if current_mode and current_mode.get("mode") in ("first", "highest"):
            joker_points = int((await db.get_mode_joker_settings(chat_id, current_mode["mode"]))["penalty"])
        else:
            joker_points = int(pvp["joker_points"])
        if joker:
            joker["display_score"] = joker_points
        if len(players) == 2:
            await db.record_deal_memory(chat_id, "pvp2", [a["card_key"] for a in assignments])
            joker_appeared = joker is not None
            await db.update_joker_cooldown(chat_id, joker_appeared)
        photo = await _render_deal_for_chat(chat_id, assignments)
        await _send_resilient(context.bot, "send_photo", chat_id, photo=InputFile(photo, filename="deal.png"))

        winner_tag = mention(winner["username"], winner["first_name"], winner["user_id"])
        points = db.pvp_points_for(pvp, len(players))
        result = f"{winner_tag} wins! {points} points!"
        if joker:
            joker_tag = mention(joker["username"], joker["first_name"], joker["user_id"])
            sign = "+" if joker_points > 0 else ""
            result += f"\n\n{joker_tag} Joker pulled {sign}{joker_points} points \U0001F62D"
        await _send_resilient(
            context.bot, "send_message",
            chat_id,
            result,
            parse_mode="HTML",
            reply_markup=_blank_outcome_markup(),
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
        await _record_mode_after_cards(context, chat_id, players)
        await _schedule_game_bump(context, chat_id)
        await asyncio.sleep(5)
        lb_text = await _cards_lb(chat_id)
        await _send_resilient(
            context.bot, "send_message",
            chat_id, lb_text, parse_mode="HTML",
            reply_markup=_leaderboard_markup(
                (await db.get_game_mode(chat_id) or {}).get("mode")
                if (await db.get_game_mode(chat_id) or {}).get("active") else None
            ),
            disable_web_page_preview=True,
        )
    finally:
        # Always clear the running state so a user message or rate-limit
        # during any phase cannot leave the chat permanently stuck.
        try:
            await db.set_status(game_id, "finished")
        except Exception:
            log.exception("Could not finalize game %s", game_id)


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
    if game is None:
        await _delete_quietly(context.bot, chat.id, message.message_id if message else None)
        await context.bot.send_message(chat.id, "There is no active game to be cancelled.")
        return

    # Allow admins to recover an invisible/stuck game even if it was already
    # marked running before its Telegram messages were successfully posted.
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
    unlock = max(0, int(draft.get("pvp_before_unlock", 1)))
    return (
        "<b>House Config</b>\n\n"
        f"House is currently {state}.\n"
        f"User max plays per day: {plays_label}\n"
        f"PvP before unlock: {unlock}\n\n"
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
            [
                InlineKeyboardButton("−", callback_data="hc:unlockminus"),
                InlineKeyboardButton(
                    f"PvP before unlock: {max(0, int(draft.get('pvp_before_unlock', 1)))}",
                    callback_data="hc:noop",
                ),
                InlineKeyboardButton("+", callback_data="hc:unlockplus"),
            ],
            [InlineKeyboardButton("Save", callback_data="hc:save")],
        ]
    )


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
        "pvp_before_unlock": max(0, int(settings.get("pvp_before_unlock", 1))),
    }
    await context.bot.send_message(
        chat.id,
        _house_cfg_text(_house_cfg_draft[chat.id]),
        parse_mode="HTML",
        reply_markup=_house_cfg_keyboard(_house_cfg_draft[chat.id]),
    )
    await _delete_quietly(
        context.bot,
        chat.id,
        message.message_id if message else None,
    )


async def houseconfig_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.data == "hc:noop":
        await query.answer()
        return
    chat = query.message.chat
    user = query.from_user
    target_gid = int(context.user_data.get("gamemode_chat_id", chat.id)) if chat.type == "private" else chat.id
    if not await _is_admin(context, target_gid, user.id):
        await query.answer("Admins only.", show_alert=True)
        return
    draft = _house_cfg_draft.setdefault(
        target_gid, {"house_enabled": True, "max_plays_per_day": None, "pvp_before_unlock": 1}
    )
    action = query.data.split(":", 1)[1]
    if action == "noop":
        await query.answer()
        return
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
    elif action == "unlockplus":
        draft["pvp_before_unlock"] = min(20, max(0, int(draft.get("pvp_before_unlock", 1))) + 1)
    elif action == "unlockminus":
        draft["pvp_before_unlock"] = max(0, int(draft.get("pvp_before_unlock", 1)) - 1)
    elif action == "save":
        await db.save_house_settings(
            target_gid,
            draft["house_enabled"],
            draft.get("max_plays_per_day"),
            draft.get("pvp_before_unlock", 1),
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
        "<b>PvP Points Config</b>\n\n"
        "Per Match + join bonus:\n"
        f"2P[+{draft['base_2']}] bonus [+{bonus(2)}]\n"
        f"3P[+{draft['base_3']}] bonus [+{bonus(3)}]\n"
        f"4P[+{draft['base_4']}] bonus [+{bonus(4)}]"
    )


def _pvp_cfg_keyboard(draft: dict):
    def pair(prefix: str) -> list:
        return [InlineKeyboardButton("−", callback_data=f"pc:{prefix}-"),
                InlineKeyboardButton("+", callback_data=f"pc:{prefix}+")]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("2P", callback_data="pc:noop")] + pair("b2") + [InlineKeyboardButton("B", callback_data="pc:noop")] + pair("n2"),
        [InlineKeyboardButton("3P", callback_data="pc:noop")] + pair("b3") + [InlineKeyboardButton("B", callback_data="pc:noop")] + pair("n3"),
        [InlineKeyboardButton("4P", callback_data="pc:noop")] + pair("b4") + [InlineKeyboardButton("B", callback_data="pc:noop")] + pair("n4"),
        [InlineKeyboardButton("Save", callback_data="pc:save")],
    ])


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
    await _delete_quietly(
        context.bot,
        chat.id,
        message.message_id if message else None,
    )


async def pvpconfig_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat = query.message.chat
    user = query.from_user
    target_gid = int(context.user_data.get("gamemode_chat_id", chat.id)) if chat.type == "private" else chat.id
    if not await _is_admin(context, target_gid, user.id):
        await query.answer("Admins only.", show_alert=True)
        return
    draft = _pvp_cfg_draft.setdefault(target_gid, db._default_pvp_settings())
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
        await db.save_pvp_settings(target_gid, draft)
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



_GAME_MODE_DRAFT: dict[tuple[int,int],dict] = {}
_MODE_WINNER_EMOJI = '<tg-emoji emoji-id="5882196778341637440">🕺</tg-emoji>'


def _gm_cb(group_id:int, action:str)->str:
    return f"gm:{group_id}:{action}"


def _table_colour_keyboard(group_id: int, theme: str):
    choices = [
        ("White background with black text", "white"),
        ("Green background with white text", "green"),
        ("Dark green background with white text", "dark_green"),
    ]
    rows = [[InlineKeyboardButton(("✓ " if theme == key else "") + label,
                                  callback_data=_gm_cb(group_id, f"tablecolour:{key}"))]
            for label, key in choices]
    rows.extend([
        [InlineKeyboardButton("Save", callback_data=_gm_cb(group_id, "tablecolour:save"))],
        [InlineKeyboardButton("Back", callback_data=_gm_cb(group_id, "menu"))],
    ])
    return InlineKeyboardMarkup(rows)


def _gm_menu_text(mode):
    # A saved/pending mode is treated as the current game for the Game Mode
    # menu. The only "Saved..." confirmation is the message shown immediately
    # after saving; subsequent /gamemode calls use the active-game menu.
    if not mode or not (mode.get("active") or mode.get("pending")):
        return "<b>Tomochi Cards game settings</b>\n\nGame mode active: Tomochi Luck"
    if mode.get("mode") == "luck":
        return "<b>Tomochi Cards game settings</b>\n\nGame mode active: Tomochi Luck"
    label="First to x points" if mode["mode"]=="first" else "Highest Score Wins"
    return f"<b>Tomochi Cards game settings</b>\n\nGame mode active: {label}"


def _gm_menu_keyboard(group_id:int,mode):
    rows=[[InlineKeyboardButton("Highest Score Wins",callback_data=_gm_cb(group_id,"highest"))],
          [InlineKeyboardButton("First to x Points",callback_data=_gm_cb(group_id,"first"))],
          [InlineKeyboardButton("Tomochi Luck",callback_data=_gm_cb(group_id,"luck"))],
          [InlineKeyboardButton("Points Settings",callback_data=_gm_cb(group_id,"pointsinfo"))],
          [InlineKeyboardButton("House Settings 2",callback_data=_gm_cb(group_id,"house2"))],
          [InlineKeyboardButton("Game Bump Alert",callback_data=_gm_cb(group_id,"bump"))],
          [InlineKeyboardButton("config minimum players",callback_data=_gm_cb(group_id,"minplayers"))],
          [InlineKeyboardButton("lb rank limit",callback_data=_gm_cb(group_id,"lblimit"))],
          [InlineKeyboardButton("Config Table Colour",callback_data=_gm_cb(group_id,"tablecolour"))]]
    return InlineKeyboardMarkup(rows)


def _gm_control_text(mode):
    label="First to x points" if mode["mode"]=="first" else "Highest Score Wins"
    value=f"Target: {int(mode['target'])}" if mode["mode"]=="first" else f"Days: {int(mode['days'])}"
    return f"<b>{label}</b>\n\nControls:\n{value}"


async def _gm_control_keyboard(group_id:int,mode):
    rows=[]
    if mode.get("active") or mode.get("pending"):
        rows.append([InlineKeyboardButton("End Current Game",callback_data=_gm_cb(group_id,"end"))])
        label="Reset game keep current target" if mode["mode"]=="first" else "Restart game clear leaderboard"
        rows.append([InlineKeyboardButton(label,callback_data=_gm_cb(group_id,"restart"))])
        rows.append([InlineKeyboardButton("Joker config x points" if mode["mode"] == "first" else "Joker config highest score",
                                          callback_data=_gm_cb(group_id, f"modejokeropen:{mode['mode']}"))])
        if mode["mode"] == "first":
            rows.append([InlineKeyboardButton("clear winners history",callback_data=_gm_cb(group_id,f"history:{mode['mode']}"))])
    rows.append([InlineKeyboardButton("Back",callback_data=_gm_cb(group_id,"menu"))])
    return InlineKeyboardMarkup(rows)


def _gm_start_text(mode:str,value:int)->str:
    if mode=="first":
        return f"<b>Game mode: First to x points</b>\n\nPlayer to reach target point first wins.\n\nTarget: {value}"
    return f"<b>Game mode: Highest Score Wins</b>\n\nHighest score wins after set amount of days\n\nDays : {value}"


def _gm_digit_keyboard(group_id:int,mode:str):
    p=mode
    rows=[]
    for nums in (("1","2","3"),("4","5","6"),("7","8","9")):
        rows.append([InlineKeyboardButton(n,callback_data=_gm_cb(group_id,f"digit:{p}:{n}")) for n in nums])
    rows.append([
        InlineKeyboardButton("<",callback_data=_gm_cb(group_id,f"digit:{p}:del")),
        InlineKeyboardButton("0",callback_data=_gm_cb(group_id,f"digit:{p}:0")),
    ])
    rows.append([
        InlineKeyboardButton("Save",callback_data=_gm_cb(group_id,f"save:{p}")),
        InlineKeyboardButton("Cancel",callback_data=_gm_cb(group_id,"menu"))
    ])
    return InlineKeyboardMarkup(rows)


async def _post_game_mode_start(context, chat_id:int, mode:dict):
    if not mode:
        return
    caption = _game_mode_start_text(mode)
    if NEW_GAME_IMAGE.exists():
        with NEW_GAME_IMAGE.open("rb") as image:
            await context.bot.send_photo(
                chat_id,
                photo=InputFile(image, filename="newgame.png"),
                caption=caption,
                parse_mode="HTML",
                reply_markup=_game_mode_start_markup(),
            )
    else:
        await context.bot.send_message(
            chat_id,
            caption,
            reply_markup=_game_mode_start_markup(),
        )


async def _activate_pending_mode_after_cards(context,chat_id:int):
    mode=await db.get_game_mode(chat_id)
    if not mode or not mode.get("pending") or mode.get("active"):
        return
    mode=await db.activate_game_mode(chat_id)
    if mode and mode["mode"]=="highest":
        _schedule_mode_end(context,chat_id,mode)


def _schedule_mode_end(context,chat_id:int,mode:dict):
    if mode.get("winner_user_id"):
        return
    for job in context.job_queue.get_jobs_by_name(f"modeend:{chat_id}"):
        job.schedule_removal()
    if not mode.get("ends_at"):
        return
    remaining=max(1,(mode["ends_at"]-datetime.now(timezone.utc)).total_seconds())
    context.job_queue.run_once(mode_end_job,when=remaining,data={"chat_id":chat_id},name=f"modeend:{chat_id}",chat_id=chat_id)


async def _mode_announce_winner(context,chat_id:int,mode:dict):
    username=mode.get("winner_username") or str(mode.get("winner_user_id"))
    score=int(mode.get("winner_score") or 0)
    if mode["mode"]=="first":
        text=f"Target met!\nTomochi Cards Winner! {_MODE_WINNER_EMOJI}\n<b>@{username}</b> {score} points!\n\nNo1 rank locked.\n\nview /history"
    else:
        days=int(mode.get("days") or 0)
        duration=f"{days*24}hrs" if days in (1,2,3) else f"{days} days"
        text=f"Highest score game over!\nTomochi Cards Winner! {_MODE_WINNER_EMOJI}\n<b>@{username}</b> claims highest score ({score}) after {duration}.\n\nNo1 rank locked.\n\nview /history"
    await context.bot.send_message(chat_id,text,parse_mode="HTML")


async def mode_end_job(context:ContextTypes.DEFAULT_TYPE):
    chat_id=(context.job.data or {}).get("chat_id")
    if not chat_id: return
    mode=await db.highest_score_winner(chat_id)
    if mode and mode.get("winner_user_id"):
        await _mode_announce_winner(context,chat_id,mode)


async def _record_mode_after_cards(context,chat_id:int,players:list):
    mode=await db.get_game_mode(chat_id)
    if not mode or not mode.get("active") or mode.get("winner_user_id"):
        return
    await db.game_mode_participation(chat_id,mode["mode"],players)
    if mode["mode"]=="first":
        winner=await db.check_first_to_winner(chat_id)
        if winner:
            await _mode_announce_winner(context,chat_id,winner)
            await db.end_game_mode(chat_id)
            await db.set_tomochi_luck_leaderboard_visible(chat_id, False)


def _mode_status_html(mode:dict,rows:list)->str:
    if not mode or not mode.get("active"): return ""
    if mode["mode"]=="first":
        target=int(mode["target"] or 0)
        leader=max((int(r["score"]) for r in rows),default=0)
        progress=0 if target<=0 else min(100,int(leader*100/target))
        lines=[f"<b>Game: First to score {target} points</b>",f"Progress: {progress}%"]
    else:
        if mode.get("winner_user_id"):
            end="Ended"
        else:
            ends=mode.get("ends_at")
            remaining=(ends-datetime.now(timezone.utc)).total_seconds() if ends else 0
            if remaining <= 0:
                end = "Ended"
            else:
                days = max(1, int(mode.get("days") or 1))
                initial_seconds = days * 86400
                # At the exact start show the configured duration (e.g. 24hrs).
                # On later leaderboard views show a live HH:MM countdown.
                if initial_seconds - remaining < 5:
                    end = f"{days * 24}hrs" if days <= 3 else f"{days} days"
                elif remaining <= 3 * 86400:
                    total_minutes = max(0, int(remaining // 60))
                    hours, minutes = divmod(total_minutes, 60)
                    end = f"{hours:02d}:{minutes:02d}mins"
                else:
                    whole_days = int(remaining // 86400)
                    remainder_minutes = int((remaining % 86400) // 60)
                    hours, minutes = divmod(remainder_minutes, 60)
                    end = f"{whole_days} days {hours:02d}:{minutes:02d}mins"
        lines=["<b>Game: Highest Score Wins</b>",f"Ends: {end}"]
    if mode.get("winner_user_id"):
        lines.append(f"Winner: {str(mode.get('winner_username') or mode['winner_user_id']).lstrip('@')}")
    return "\n".join(lines)


async def _history_text(chat_id:int)->str:
    first=await db.game_mode_history(chat_id,"first")
    high=await db.game_mode_history(chat_id,"highest")
    if not first and not high:
        return "No history. Game mode winners history presents here after first winner recorded."

    def plain_name(row) -> str:
        username = row["username"]
        if username:
            return str(username).lstrip("@").replace("<", "").replace(">", "")
        return str(row["user_id"])

    def section(title, rows, ranked: bool) -> str:
        if not rows:
            return ""
        lines=[title, ""]
        if ranked:
            last=None
            rank=0
            for n,row in enumerate(rows,1):
                wins=int(row["wins"])
                if wins != last:
                    rank=n
                    last=wins
                lines.append(
                    f"{rank} {plain_name(row)}\n"
                    f"Wins: {wins} | Played: {int(row['plays'])}"
                )
        else:
            for row in rows:
                wins=int(row["wins"])
                lines.append(
                    f"{plain_name(row)}\n"
                    f"Wins: {wins} | Played: {int(row['plays'])}"
                )
        return "\n".join(lines)

    sections=[]
    # Most-played mode first. If tied, keep First to x Points first.
    if first and high:
        fp=sum(int(r["plays"]) for r in first)
        hp=sum(int(r["plays"]) for r in high)
        if hp > fp:
            sections=[
                section("Highest Score game:", high, True),
                section("First to x Points game:", first, True),
            ]
        else:
            sections=[
                section("First to x Points game:", first, True),
                section("Highest Score game:", high, True),
            ]
    elif first:
        sections=[section("First to x Points game:", first, True)]
    else:
        sections=[section("Highest Score game:", high, True)]

    return "<b>Tomochi Cards Winners History</b>\n\n" + "\n\n".join(
        x for x in sections if x
    )


def _game_mode_duration(days: int) -> str:
    days = max(0, int(days or 0))
    if days in (1, 2, 3):
        return f"{days * 24}hrs"
    return f"{days} days"


def _game_mode_start_text(mode: dict) -> str:
    if mode.get("mode") == "highest":
        return (
            "<b>Tomochi Cards!</b>\n\nNew Highest Score Wins game started!\n"
            f"Ends: {_game_mode_duration(int(mode.get('days') or 0))}"
        )
    target = int(mode.get("target") or 0)
    return (
        "<b>Tomochi Cards!</b>\n\n"
        "New First to x Points game started!\n"
        f"Target: {target}"
    )


def _game_mode_start_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Play Cards", callback_data="playcards")]]
    )


def _blank_outcome_markup() -> InlineKeyboardMarkup:
    # Telegram requires button text; an invisible separator keeps it visually blank.
    return InlineKeyboardMarkup([[InlineKeyboardButton("\u2063", callback_data="hc:noop")]])


def _leaderboard_markup(mode_name: str | None = None) -> InlineKeyboardMarkup:
    # A mode-specific callback lets old leaderboard buttons be rejected after
    # that tournament has ended, instead of accidentally starting Tomochi Luck.
    callback = f"playcards:mode:{mode_name}" if mode_name in ("first", "highest") else "playcards"
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Play Cards", callback_data=callback)]]
    )


async def _cards_lb(chat_id: int) -> str:
    mode = await db.get_game_mode(chat_id)
    if not mode or not (mode.get("active") or mode.get("pending")):
        return await _tomochi_luck_lb(chat_id)
    if mode and mode.get("pending") and not mode.get("active"):
        return _game_mode_start_text(mode)
    rank_limit = await db.get_leaderboard_rank_limit(chat_id)
    rows = await db.leaderboard(chat_id, rank_limit if rank_limit > 0 else 10000)
    html = _leaderboard_html(rows)
    if mode and mode.get("active") and rows:
        status = _mode_status_html(mode, rows)
        if status:
            parts = html.split("\n\n", 1)
            html = parts[0] + "\n\n" + status + ("\n\n" + parts[1] if len(parts) == 2 else "")
    return html


async def lb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Use /cardslb in the group.")
        return
    html = await _cards_lb(chat.id)
    mode = await db.get_game_mode(chat.id)
    await update.message.reply_html(
        html,
        reply_markup=_game_mode_start_markup() if (mode and mode.get("pending") and not mode.get("active")) else _leaderboard_markup(),
        disable_web_page_preview=True,
    )


async def playcards_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    chat = query.message.chat if query.message else None
    if data.startswith("playcards:mode:") and chat and chat.type in ("group", "supergroup"):
        expected_mode = data.rsplit(":", 1)[-1]
        current = await db.get_game_mode(chat.id)
        # Old tournament buttons must not start the default game after the
        # tournament ended, or start a different tournament's game.
        ended = (
            not current
            or not current.get("active")
            or current.get("pending")
            or current.get("mode") != expected_mode
            or current.get("winner_user_id") is not None
        )
        if expected_mode == "highest" and current and current.get("ends_at"):
            ends_at = current["ends_at"]
            if ends_at <= datetime.now(timezone.utc):
                ended = True
        if ended:
            await query.answer("This Game has Ended. Use /cards to play current game", show_alert=True)
            return
    # Do not answer here — cards_cmd / _join_existing_lobby will answer
    # with the appropriate alert (or a plain answer if no alert is needed).
    # Reuse the normal /cards path in the group where the button was pressed.
    if chat and chat.type in ("group", "supergroup"):
        await cards_cmd(update, context)
    else:
        await query.answer()


async def lucklb_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    chat = q.message.chat if q.message else None
    if chat and chat.type in ("group", "supergroup"):
        await context.bot.send_message(chat.id, await _tomochi_luck_lb(chat.id), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Play", callback_data="playcards")]]))


async def points_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat = query.message.chat if query.message else None
    if chat is None or chat.type not in ("group", "supergroup"):
        await query.answer("Use Points in a group game.", show_alert=True)
        return

    pvp = await db.get_pvp_settings(chat.id)
    house = await db.get_house_settings(chat.id)
    mode = await db.get_game_mode(chat.id)

    def join_bonus(n: int) -> int:
        base = int(pvp.get(f"base_{n}") or 0)
        bonus = int(pvp.get(f"bonus_{n}") or 0)
        return max(0, bonus) if base > 0 else 0

    lines = [
        "Current Game Points:",
        "",
        f"2P {int(pvp.get('base_2') or 0)} + {join_bonus(2)} join bonus",
        f"3P {int(pvp.get('base_3') or 0)} + {join_bonus(3)} join bonus",
        f"4P {int(pvp.get('base_4') or 0)} + {join_bonus(4)} join bonus",
    ]

    if mode and (mode.get("active") or mode.get("pending")) and mode.get("mode") in ("first", "highest"):
        cfg = await db.get_gamemode_config(chat.id)
        risk = int(cfg.get("default_house_risk") or 0)
        reward = int(cfg.get("default_house_reward") or 0)
        lines.extend(["", f"Fixed House points: risk -{risk}/reward +{reward}"])
        boosts = await db.get_house_gap_boosts(chat.id)
        active_boosts = [
            b for b in boosts
            if int(b.get("gap_from") or 0) > 0
            and int(b.get("gap_to") or 0) >= int(b.get("gap_from") or 0)
            and (int(b.get("risk") or 0) > 0 or int(b.get("reward") or 0) > 0)
        ]
        if active_boosts:
            lowest = min(int(b["gap_from"]) for b in active_boosts)
            lines.extend([
                "",
                f"House will offer different risk/reward points for players significantly behind leader by {lowest}+ points."
            ])

    max_plays = house.get("max_plays_per_day")
    if max_plays is None:
        lines.extend(["", "House daily limit per player: no limit"])
    else:
        lines.extend(["", f"House daily limit per player: {int(max_plays)}"])

    full_text = "\n".join(lines)
    # Telegram callback alerts are limited to 200 characters.
    await query.answer(full_text[:200], show_alert=True)
    if len(full_text) > 200:
        await context.bot.send_message(chat.id, full_text)

async def showlb_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat = query.message.chat
    html = await _cards_lb(chat.id)
    mode = await db.get_game_mode(chat.id)
    await context.bot.send_message(
        chat.id,
        html,
        parse_mode="HTML",
        reply_markup=_game_mode_start_markup() if (mode and mode.get("pending") and not mode.get("active")) else _leaderboard_markup(),
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

        # Determine luck mode safely so a failure here cannot abort the bump.
        luck_mode = False
        try:
            luck_mode = (await _effective_game_mode(chat_id)).get("mode") == "luck"
        except Exception:
            log.exception("Could not determine game mode for lobby bump; defaulting to non-luck")

        house_available = False
        try:
            house_available = (await db.house_enabled(chat_id)) and len(players) <= 1
        except Exception:
            log.exception("Could not load house availability for lobby bump")

        try:
            msg = await context.bot.send_message(
                chat_id,
                await _lobby_text_for_chat(chat_id, players, game["flavor"]),
                parse_mode="HTML",
                reply_markup=lobby_keyboard(
                    game_id,
                    house_available=house_available,
                    luck_mode=luck_mode,
                ),
                disable_web_page_preview=True,
            )
        except Exception:
            log.exception("Lobby bump failed for chat %s game %s", chat_id, game_id)
            return

        await db.set_message_id(game_id, msg.message_id)
        await _delete_quietly(context.bot, chat_id, old_id)



async def gamemode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message

    # /gamemode in a group posts a deep-link button. Telegram cannot force-open
    # a private chat, so the button takes the admin to the bot's private chat
    # and carries this group's id with it.
    if chat.type in ("group", "supergroup"):
        if not await _is_admin(context, chat.id, user.id):
            await _delete_quietly(context.bot, chat.id, message.message_id if message else None)
            return

        username = context.bot.username or "tomochicardbot"
        url = f"https://t.me/{username}?start=gamemode_{chat.id}"
        settings_msg = await context.bot.send_message(
            chat.id,
            "game mode settings",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Open", url="https://t.me/tomochicardbot?start=gamemode_" + str(chat.id))]
            ]),
        )
        await _delete_quietly(context.bot, chat.id, message.message_id if message else None)
        await _delete_after_seconds(
            context,
            chat.id,
            settings_msg.message_id,
            6,
            f"gamemode-open:{chat.id}:{settings_msg.message_id}",
        )
        return

    # In private chat, /gamemode operates on the group selected by the
    # deep-link. This also allows the admin to call /gamemode again privately.
    if chat.type == "private":
        gid = context.user_data.get("gamemode_chat_id")
        if gid is None:
            await update.message.reply_text(
                "Open Game Mode from a group first using the Open Game Mode button."
            )
            return
        try:
            gid = int(gid)
        except (TypeError, ValueError):
            await update.message.reply_text(
                "Open Game Mode from a group first using the Open Game Mode button."
            )
            return
        if not await _is_admin(context, gid, user.id):
            await update.message.reply_text("Admins only.")
            return

        mode = await db.get_game_mode(gid)
        menu_msg = await update.message.reply_html(
            _gm_menu_text(mode),
            reply_markup=_gm_menu_keyboard(gid, mode),
        )
        await _delete_quietly(
            context.bot,
            chat.id,
            message.message_id if message else None,
        )
        return

    return


def _bump_digit_keyboard(group_id: int) -> InlineKeyboardMarkup:
    rows = []
    for nums in (("1", "2", "3"), ("4", "5", "6"), ("7", "8", "9")):
        rows.append([InlineKeyboardButton(n, callback_data=_gm_cb(group_id, f"bumpdigit:{n}")) for n in nums])
    rows.append([
        InlineKeyboardButton("<", callback_data=_gm_cb(group_id, "bumpdigit:del")),
        InlineKeyboardButton("0", callback_data=_gm_cb(group_id, "bumpdigit:0")),
    ])
    rows.append([
        InlineKeyboardButton("Save", callback_data=_gm_cb(group_id, "bumpsave")),
        InlineKeyboardButton("Cancel", callback_data=_gm_cb(group_id, "menu")),
    ])
    return InlineKeyboardMarkup(rows)


def _bump_settings_text(minutes: int) -> str:
    return (
        "<b>Set game reminder alert bump.</b>\n\n"
        f"Alert if game inactive every: {minutes}mins\n\n"
        "Game bump alert only triggers if an active game mode has not had a completed /cards game "
        "within the set number of minutes. Set 0 for no alert."
    )


async def _schedule_game_bump(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    name = f"gamebump:{chat_id}"
    for job in context.job_queue.get_jobs_by_name(name):
        job.schedule_removal()
    mode = await db.get_game_mode(chat_id)
    luck_default = (not mode or not (mode.get("active") or mode.get("pending")) or mode.get("mode") == "luck")
    minutes = await db.tomochi_luck_bump_minutes(chat_id) if luck_default else int((mode or {}).get("bump_alert_minutes", 30) or 0)
    if (not luck_default and (not mode.get("active") or mode.get("winner_user_id"))) or minutes <= 0:
        return
    context.job_queue.run_once(
        game_bump_job, when=minutes * 60,
        data={"chat_id": chat_id}, name=name, chat_id=chat_id,
    )


async def game_bump_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = int((context.job.data or {}).get("chat_id") or 0)
    if not chat_id:
        return
    mode = await db.get_game_mode(chat_id)
    luck_default = (not mode or not (mode.get("active") or mode.get("pending")) or mode.get("mode") == "luck")
    minutes = await db.tomochi_luck_bump_minutes(chat_id) if luck_default else int((mode or {}).get("bump_alert_minutes", 30) or 0)
    if (not luck_default and (not mode.get("active") or mode.get("winner_user_id"))) or minutes <= 0:
        return
    if luck_default:
        text = '<b>Game in play:</b>\n\n<b>Tomochi Luck</b>\n\nTry your luck<tg-emoji emoji-id="5933961609683673255">😏</tg-emoji>'
        ids = context.bot_data.setdefault("game_in_play_message_ids", {})
        if ids.get(chat_id):
            await _delete_quietly(context.bot, chat_id, ids[chat_id])
        sent = await context.bot.send_message(chat_id, text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Play", callback_data="playcards")],
                [InlineKeyboardButton("View Leaderboard", callback_data="lucklb")]]))
        ids[chat_id] = sent.message_id
        await _schedule_game_bump(context, chat_id)
        return
    rows = await db.leaderboard(chat_id)
    if rows:
        leader = rows[0]
        username = str(leader.get("username") or leader.get("first_name") or leader.get("user_id") or "player").lstrip("@")
        score = int(leader.get("score") or 0)
    else:
        username, score = "No players yet", 0
    if mode.get("mode") == "highest":
        ends = mode.get("ends_at")
        remaining = max(0, int((ends - datetime.now(timezone.utc)).total_seconds() // 60)) if ends else 0
        hours, mins = divmod(remaining, 60)
        ends_text = f"{hours:02d}:{mins:02d}mins"
        text = f"<b>Game in play:</b>\n\n<b>Highest Score Wins</b>\n\nLeader: {username}\nPoints: {score}\nEnds: {ends_text}"
    else:
        text = f"<b>Game in play:</b>\n\n<b>First to x Points</b>\n\nLeader: {username}\nPoints: {score}"
    # Remove the previous Game in play reminder for this chat before posting the new one.
    previous_ids = context.bot_data.setdefault("game_in_play_message_ids", {})
    previous_id = previous_ids.get(chat_id)
    if previous_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=previous_id)
        except Exception:
            # The old reminder may already have been deleted or become inaccessible.
            pass
    sent = await context.bot.send_message(
        chat_id, text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Play", callback_data="playcards")]]),
        disable_web_page_preview=True,
    )
    previous_ids[chat_id] = sent.message_id
    await _schedule_game_bump(context, chat_id)



def _joker_config_text(cfg: dict) -> str:
    chance = int(cfg.get("chance", 8))
    house = "Yes" if cfg.get("in_house", True) else "No"
    pen = int(cfg.get("wins_penalty", -1))
    return (
        "<b>Set % chance that joker appears in a round.</b>\n\n"
        f"Chance: {chance}% (0 = none)\n"
        f"Joker in vs House: {house}\n"
        f"Wins: {pen}\n\n"
    )


def _joker_config_keyboard(gid: int, cfg: dict):
    chance = int(cfg.get("chance", 8))
    house_yes = bool(cfg.get("in_house", True))
    pen = int(cfg.get("wins_penalty", -1))
    digits = []
    for row in (["1","2","3"], ["4","5","6"], ["7","8","9"], ["0", "del"]):
        digits.append([
            InlineKeyboardButton("<" if d == "del" else d, callback_data=_gm_cb(gid, f"jokerdigit:{d}"))
            for d in row
        ])
    return InlineKeyboardMarkup(
        digits + [
            [InlineKeyboardButton("House NO" if house_yes else "House YES", callback_data=_gm_cb(gid, "jokerhouse"))],
            [
                InlineKeyboardButton("−", callback_data=_gm_cb(gid, "jokerpenalty-")),
                InlineKeyboardButton(f"Wins {pen}", callback_data="pc:noop"),
                InlineKeyboardButton("+", callback_data=_gm_cb(gid, "jokerpenalty+")),
            ],
            [InlineKeyboardButton("Save", callback_data=_gm_cb(gid, "jokersave"))],
            [InlineKeyboardButton("Back", callback_data=_gm_cb(gid, "luck"))],
        ]
    )




def _load_allowed_chats() -> set[int]:
    """Load allowed group chat IDs from allowed_chats.txt next to the project root.
    Empty file (or only comments) means everyone is allowed.
    """
    candidates = [
        Path(__file__).resolve().parent.parent / "allowed_chats.txt",
        Path.cwd() / "allowed_chats.txt",
    ]
    path = next((p for p in candidates if p.exists()), None)
    if path is None:
        return set()
    allowed: set[int] = set()
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                allowed.add(int(line))
            except ValueError:
                continue
    except Exception:
        log.exception("Could not read allowed_chats.txt")
    return allowed


async def bot_added_to_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Post a welcome message when the bot is added to a group, or leave if not whitelisted."""
    member = update.my_chat_member
    if not member:
        return
    old = member.old_chat_member.status
    new = member.new_chat_member.status
    # Only fire when the bot itself is newly added (not already present)
    if old in ("member", "administrator", "creator") or new not in ("member", "administrator"):
        return
    chat = member.chat
    if chat.type not in ("group", "supergroup"):
        return

    allowed = _load_allowed_chats()
    # Empty whitelist = everyone allowed. Non-empty = only listed groups.
    if allowed and chat.id not in allowed:
        try:
            await context.bot.send_message(
                chat.id,
                'Private Tomochi® bot sir <tg-emoji emoji-id="5962823192912336897">🦸‍♂️</tg-emoji>',
                parse_mode="HTML",
            )
        except Exception:
            log.exception("Could not send private-bot message to %s", chat.id)
        try:
            await context.bot.leave_chat(chat.id)
        except Exception:
            log.exception("Could not leave chat %s", chat.id)
        return

    text = (
        "Tomochi cards!\n\n"
        "Game of chance activated.\n\n"
        "Current game mode: <b>Tomochi Luck</b>\n\n"
        "No points just luck.\n\n"
        'Try your luck. <tg-emoji emoji-id="5933961609683673255">😏</tg-emoji>\n\n'
        "Use /cards or click play to start!"
    )
    await context.bot.send_message(
        chat.id,
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Play", callback_data="playcards")]]),
        disable_web_page_preview=True,
    )


def _minplayers_text(cfg: dict) -> str:
    return ("Config minimum players and time before a game can start.\n\n"
            f"Min Players: {cfg['min_players']}\n\n"
            f"Time before game starts: {cfg['lobby_minutes']}mins")


def _minplayers_keyboard(gid: int, cfg: dict):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("−", callback_data=_gm_cb(gid,"minminus")),
         InlineKeyboardButton(f"Min Players: {cfg['min_players']}", callback_data=_gm_cb(gid,"minnoop")),
         InlineKeyboardButton("+", callback_data=_gm_cb(gid,"minplus"))],
        [InlineKeyboardButton("−", callback_data=_gm_cb(gid,"timeminus")),
         InlineKeyboardButton(f"Time: {cfg['lobby_minutes']} mins", callback_data=_gm_cb(gid,"timenoop")),
         InlineKeyboardButton("+", callback_data=_gm_cb(gid,"timeplus"))],
        [InlineKeyboardButton("Save", callback_data=_gm_cb(gid,"minsave"))],
        [InlineKeyboardButton("Back", callback_data=_gm_cb(gid,"menu"))],
    ])


def _house2_keyboard(gid: int, enabled: bool):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Disable House" if enabled else "Enable House", callback_data=_gm_cb(gid,"house2toggle"))],
        [InlineKeyboardButton("House Points", callback_data=_gm_cb(gid,"housepoints"))],
        [InlineKeyboardButton("House Limit", callback_data=_gm_cb(gid,"houselimit"))],
        [InlineKeyboardButton("Back", callback_data=_gm_cb(gid,"menu"))],
    ])


def _house_points_text(cfg: dict, boosts: list[dict]) -> str:
    rows = [f"Points Gap {b['gap_from']}-{b['gap_to']}: risk/rwd -{b['risk']}/{b['reward']}" for b in boosts]
    return ("<b>Configure house challenge risk and reward points for all players and house boost.</b>\n\n"
            f"Default House Risk/Reward: -{cfg['default_house_risk']}/+{cfg['default_house_reward']}\n\n"
            f"Temporarily disable house for leader if ahead by: {'OFF' if int(cfg['leader_disable_gap']) < 1 else str(cfg['leader_disable_gap']) + ' points'}\n\n"
            "House R/R gap boost for players behind leader. Triggers in-game:\n\n" + "\n".join(rows))


def _house_points_keyboard(gid: int, cfg: dict, boosts: list[dict]):
    rows = [
        [InlineKeyboardButton("Default House risk/reward for all:", callback_data=_gm_cb(gid,"hpnoop"))],
        [InlineKeyboardButton("−", callback_data=_gm_cb(gid,"hp:default_house_risk:-")),
         InlineKeyboardButton("+", callback_data=_gm_cb(gid,"hp:default_house_risk:+")),
         InlineKeyboardButton("R/R", callback_data=_gm_cb(gid,"hpnoop")),
         InlineKeyboardButton("−", callback_data=_gm_cb(gid,"hp:default_house_reward:-")),
         InlineKeyboardButton("+", callback_data=_gm_cb(gid,"hp:default_house_reward:+"))],
        [InlineKeyboardButton("Gap disabling house for leader:", callback_data=_gm_cb(gid,"hpnoop"))],
        [InlineKeyboardButton("−", callback_data=_gm_cb(gid,"hp:leader_disable_gap:-")),
         InlineKeyboardButton("OFF" if int(cfg["leader_disable_gap"]) < 1 else f"{cfg['leader_disable_gap']} points", callback_data=_gm_cb(gid,"hpnoop")),
         InlineKeyboardButton("+", callback_data=_gm_cb(gid,"hp:leader_disable_gap:+"))],
        [InlineKeyboardButton("House risk/reward Gap Boost:", callback_data=_gm_cb(gid,"hpnoop"))],
    ]
    for b in boosts:
        slot = int(b["slot"])
        label = f"Gap: {b['gap_from']} to {b['gap_to']} · R/R: -{b['risk']} / +{b['reward']}"
        rows.append([InlineKeyboardButton(label, callback_data=_gm_cb(gid, f"hpedit:{slot}"))])
    rows += [
        [InlineKeyboardButton("Save", callback_data=_gm_cb(gid,"housepointssave"))],
        [InlineKeyboardButton("Cancel (discard changes)", callback_data=_gm_cb(gid,"housepointscancel"))],
        [InlineKeyboardButton("Back", callback_data=_gm_cb(gid,"house2"))],
    ]
    return InlineKeyboardMarkup(rows)


def _house_boost_edit_keyboard(gid: int, boost: dict):
    slot = int(boost["slot"])
    rows = [
        [InlineKeyboardButton(f"Gap start: {boost['gap_from']}", callback_data=_gm_cb(gid,"hpnoop"))],
        [InlineKeyboardButton("−", callback_data=_gm_cb(gid,f"hpedit:{slot}:gap_from:-")),
         InlineKeyboardButton("+", callback_data=_gm_cb(gid,f"hpedit:{slot}:gap_from:+"))],
        [InlineKeyboardButton(f"Gap end: {boost['gap_to']}", callback_data=_gm_cb(gid,"hpnoop"))],
        [InlineKeyboardButton("−", callback_data=_gm_cb(gid,f"hpedit:{slot}:gap_to:-")),
         InlineKeyboardButton("+", callback_data=_gm_cb(gid,f"hpedit:{slot}:gap_to:+"))],
        [InlineKeyboardButton(f"Risk: -{boost['risk']}", callback_data=_gm_cb(gid,"hpnoop"))],
        [InlineKeyboardButton("−", callback_data=_gm_cb(gid,f"hpedit:{slot}:risk:-")),
         InlineKeyboardButton("+", callback_data=_gm_cb(gid,f"hpedit:{slot}:risk:+"))],
        [InlineKeyboardButton(f"Reward: +{boost['reward']}", callback_data=_gm_cb(gid,"hpnoop"))],
        [InlineKeyboardButton("−", callback_data=_gm_cb(gid,f"hpedit:{slot}:reward:-")),
         InlineKeyboardButton("+", callback_data=_gm_cb(gid,f"hpedit:{slot}:reward:+"))],
        [InlineKeyboardButton("Back to House Points", callback_data=_gm_cb(gid,f"hpedit:{slot}:back"))],
    ]
    return InlineKeyboardMarkup(rows)


def _joker_mode_text(cfg: dict) -> str:
    return ("Set negative score and % chance that joker appears in a round.\n\n"
            f"Chance: {cfg['chance']}% (0 = none)\nScore: {cfg['penalty']}")


def _joker_mode_keyboard(gid: int, which: str, cfg: dict):
    rows = [[InlineKeyboardButton(str(n),callback_data=_gm_cb(gid,f"modejoker:{which}:digit:{n}")) for n in range(5)],
            [InlineKeyboardButton(str(n),callback_data=_gm_cb(gid,f"modejoker:{which}:digit:{n}")) for n in range(5,10)]]
    rows[1].append(InlineKeyboardButton("⌫",callback_data=_gm_cb(gid,f"modejoker:{which}:del")))
    rows += [[InlineKeyboardButton("joker score −",callback_data=_gm_cb(gid,f"modejoker:{which}:penalty-")),
              InlineKeyboardButton(f"{cfg['penalty']}",callback_data=_gm_cb(gid,"hpnoop")),
              InlineKeyboardButton("joker score +",callback_data=_gm_cb(gid,f"modejoker:{which}:penalty+"))],
             [InlineKeyboardButton("Save",callback_data=_gm_cb(gid,f"modejoker:{which}:save"))],
             [InlineKeyboardButton("Back",callback_data=_gm_cb(gid,"menu"))]]
    return InlineKeyboardMarkup(rows)


async def gamemode_cb(update:Update,context:ContextTypes.DEFAULT_TYPE)->None:
    q=update.callback_query; p=q.data.split(":",2)
    if len(p)<3: await q.answer(); return
    try: gid=int(p[1])
    except ValueError: await q.answer(); return
    context.user_data["gamemode_chat_id"] = gid
    if not await _is_admin(context,gid,q.from_user.id):
        await q.answer("Admins only.",show_alert=True); return
    action=p[2]; mode=await db.get_game_mode(gid)
    if action == "minplayers":
        cfg = await db.get_gamemode_config(gid)
        _GAME_MODE_DRAFT[(gid,q.from_user.id)] = {"mode":"minplayers", **cfg}
        await q.answer()
        await q.edit_message_text(_minplayers_text(cfg), reply_markup=_minplayers_keyboard(gid,cfg))
        return
    if action in ("minplus","minminus","timeplus","timeminus"):
        cfg = _GAME_MODE_DRAFT.setdefault((gid,q.from_user.id), {"mode":"minplayers", **(await db.get_gamemode_config(gid))})
        if action == "minplus": cfg["min_players"] = min(4,int(cfg["min_players"])+1)
        elif action == "minminus": cfg["min_players"] = max(2,int(cfg["min_players"])-1)
        elif action == "timeplus": cfg["lobby_minutes"] = min(10,int(cfg["lobby_minutes"])+1)
        else: cfg["lobby_minutes"] = max(3,int(cfg["lobby_minutes"])-1)
        await q.answer()
        await q.edit_message_text(_minplayers_text(cfg), reply_markup=_minplayers_keyboard(gid,cfg))
        return
    if action in ("minnoop","timenoop","hpnoop"):
        await q.answer(); return
    if action == "minsave":
        cfg = _GAME_MODE_DRAFT.pop((gid,q.from_user.id), {})
        await db.save_gamemode_config(gid, min_players=cfg.get("min_players",2), lobby_minutes=cfg.get("lobby_minutes",5))
        await q.answer("Saved.")
        await q.edit_message_text("Minimum players and lobby time saved.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back",callback_data=_gm_cb(gid,"menu"))]]))
        return
    if action == "house2":
        cfg = await db.get_gamemode_config(gid)
        await q.answer()
        await q.edit_message_text("<b>House Settings 2</b>",parse_mode="HTML",reply_markup=_house2_keyboard(gid,cfg["house_points_enabled"]))
        return
    if action == "house2toggle":
        cfg = await db.get_gamemode_config(gid)
        await db.save_gamemode_config(gid, house_points_enabled=not cfg["house_points_enabled"])
        cfg = await db.get_gamemode_config(gid)
        await q.answer("Saved.")
        await q.edit_message_text("<b>House Settings 2</b>",parse_mode="HTML",reply_markup=_house2_keyboard(gid,cfg["house_points_enabled"]))
        return
    if action == "houselimit":
        settings = await db.get_house_settings(gid)
        _house_cfg_draft[gid] = {"house_enabled":settings["house_enabled"],"max_plays_per_day":settings["max_plays_per_day"],
                                 "pvp_before_unlock":max(0,int(settings.get("pvp_before_unlock",1)))}
        await q.answer()
        await q.message.reply_text(_house_cfg_text(_house_cfg_draft[gid]),parse_mode="HTML",reply_markup=_house_cfg_keyboard(_house_cfg_draft[gid]))
        return
    if action == "housepoints":
        cfg = await db.get_gamemode_config(gid); boosts = await db.get_house_gap_boosts(gid)
        _GAME_MODE_DRAFT[(gid,q.from_user.id)] = {"mode":"housepoints","cfg":cfg,"boosts":boosts}
        await q.answer()
        await q.edit_message_text(_house_points_text(cfg,boosts),parse_mode="HTML",reply_markup=_house_points_keyboard(gid,cfg,boosts))
        return
    if action.startswith("hpedit:"):
        bits = action.split(":")
        draft = _GAME_MODE_DRAFT.setdefault(
            (gid, q.from_user.id),
            {"mode": "housepoints", "cfg": await db.get_gamemode_config(gid),
             "boosts": await db.get_house_gap_boosts(gid)}
        )
        try:
            slot = int(bits[1])
            boost = next(b for b in draft["boosts"] if int(b["slot"]) == slot)
        except (ValueError, IndexError, StopIteration):
            await q.answer("That Gap Boost row is unavailable.", show_alert=True)
            return
        if len(bits) == 2:
            await q.answer()
            await q.edit_message_text(
                f"<b>House R/R Gap Boost {slot}</b>\\n\\nSet the gap range and risk/reward for this boost.",
                parse_mode="HTML", reply_markup=_house_boost_edit_keyboard(gid, boost)
            )
            return
        sub = bits[2]
        if sub == "back":
            await q.answer()
            await q.edit_message_text(
                _house_points_text(draft["cfg"], draft["boosts"]), parse_mode="HTML",
                reply_markup=_house_points_keyboard(gid, draft["cfg"], draft["boosts"])
            )
            return
        if len(bits) == 4 and sub in ("gap_from", "gap_to", "risk", "reward"):
            step = 1 if bits[3] == "+" else -1
            boost[sub] = max(0, min(100, int(boost.get(sub, 0)) + step))
            await q.answer()
            await q.edit_message_text(
                f"<b>House R/R Gap Boost {slot}</b>\\n\\nSet the gap range and risk/reward for this boost.",
                parse_mode="HTML", reply_markup=_house_boost_edit_keyboard(gid, boost)
            )
            return
        await q.answer()
        return
    if action == "housepointscancel":
        _GAME_MODE_DRAFT.pop((gid, q.from_user.id), None)
        cfg = await db.get_gamemode_config(gid)
        boosts = await db.get_house_gap_boosts(gid)
        await q.answer("Unsaved changes discarded.")
        await q.edit_message_text(
            _house_points_text(cfg, boosts), parse_mode="HTML",
            reply_markup=_house_points_keyboard(gid, cfg, boosts)
        )
        return
    if action.startswith("hp:"):
        cfg = _GAME_MODE_DRAFT.setdefault((gid,q.from_user.id),{"mode":"housepoints","cfg":await db.get_gamemode_config(gid),"boosts":await db.get_house_gap_boosts(gid)})
        bits=action.split(":")
        step=1 if bits[-1]=="+" else -1
        if len(bits)==3:
            key=bits[1]; cfg["cfg"][key]=max(0,min(50,int(cfg["cfg"].get(key,0))+step))
        elif len(bits)==4:
            slot=int(bits[1]); key=bits[2]
            boost=cfg["boosts"][slot-1]
            boost[key]=max(0,min(100,int(boost.get(key,0))+step))
        await q.answer()
        await q.edit_message_text(_house_points_text(cfg["cfg"],cfg["boosts"]),parse_mode="HTML",reply_markup=_house_points_keyboard(gid,cfg["cfg"],cfg["boosts"]))
        return
    if action == "housepointssave":
        draft=_GAME_MODE_DRAFT.pop((gid,q.from_user.id),{})
        await db.save_gamemode_config(gid, **{k:draft.get("cfg",{}).get(k) for k in ("default_house_risk","default_house_reward","leader_disable_gap")})
        for b in draft.get("boosts",[]): await db.save_house_gap_boost(gid,int(b["slot"]),**{k:b[k] for k in ("gap_from","gap_to","risk","reward")})
        await q.answer("Saved.")
        await q.edit_message_text("House Points settings saved.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back",callback_data=_gm_cb(gid,"house2"))]]))
        return
    if action.startswith("modejokeropen:"):
        which = action.split(":", 1)[1].strip().lower()
        if which not in ("first", "highest"):
            await q.answer("Unknown game mode Joker config.", show_alert=True)
            return
        try:
            cfg = await db.get_mode_joker_settings(gid, which)
            cfg = {
                "chance": max(0, min(100, int(cfg.get("chance", 15)))),
                "penalty": max(-30, min(-1, int(cfg.get("penalty", -5)))),
            }
            _GAME_MODE_DRAFT[(gid, q.from_user.id)] = {
                "mode": "modejoker",
                "which": which,
                "chance": cfg["chance"],
                "penalty": cfg["penalty"],
            }
            # Send the panel as a new message. The source menu can be stale or
            # otherwise non-editable; opening settings must not depend on editing it.
            await q.answer()
            await context.bot.send_message(
                chat_id=q.message.chat_id,
                text=_joker_mode_text(cfg),
                reply_markup=_joker_mode_keyboard(gid, which, cfg),
            )
        except Exception:
            log.exception("Could not open %s Joker config for group %s", which, gid)
            try:
                await q.answer("Could not open Joker config. Check bot logs for details.", show_alert=True)
            except Exception:
                pass
        return
    if action.startswith("modejoker:"):
        bits=action.split(":")
        which=bits[1]; sub=bits[2]
        draft=_GAME_MODE_DRAFT.setdefault((gid,q.from_user.id),{"mode":"modejoker","which":which,"chance":15,"penalty":-5})
        if sub=="digit":
            draft["chance"]=min(100,int(draft.get("chance",0))*10+int(bits[3]))
        elif sub=="del": draft["chance"]=int(draft.get("chance",0))//10
        elif sub.startswith("penalty"):
            draft["penalty"]=max(-30,min(-1,int(draft.get("penalty",-5))+(1 if sub.endswith("+") else -1)))
        elif sub=="save":
            await db.set_mode_joker_settings(gid,which,int(draft["chance"]),int(draft["penalty"]))
            await q.answer("Saved.")
            await q.edit_message_text("Joker settings saved.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back",callback_data=_gm_cb(gid,"menu"))]]))
            return
        cfg={"chance":int(draft["chance"]),"penalty":int(draft["penalty"])}
        await q.answer()
        await q.edit_message_text(_joker_mode_text(cfg),reply_markup=_joker_mode_keyboard(gid,which,cfg))
        return
    if action == "tablecolour":
        theme = await db.get_table_colour(gid)
        _GAME_MODE_DRAFT[(gid, q.from_user.id)] = {"mode": "tablecolour", "theme": theme}
        await q.answer()
        labels = {"white": ("White", "Black"), "green": ("Green", "White"), "dark_green": ("Dark green", "White")}
        bg, fg = labels.get(theme, labels["white"])
        await q.edit_message_text(
            f"Config background table colour of cards and text scores text\n\nBackground: {bg}\nText: {fg}",
            reply_markup=_table_colour_keyboard(gid, theme),
        )
        return
    if action.startswith("tablecolour:"):
        sub = action.split(":", 1)[1]
        draft = _GAME_MODE_DRAFT.setdefault(
            (gid, q.from_user.id),
            {"mode": "tablecolour", "theme": await db.get_table_colour(gid)}
        )
        if sub in ("white", "green", "dark_green"):
            draft["theme"] = sub
            labels = {"white": ("White", "Black"), "green": ("Green", "White"), "dark_green": ("Dark green", "White")}
            bg, fg = labels[sub]
            await q.answer()
            await q.edit_message_text(
            f"Config background table colour of cards and text scores text\n\nBackground: {bg}\nText: {fg}",
                reply_markup=_table_colour_keyboard(gid, sub),
            )
            return
        if sub == "save":
            await db.set_table_colour(gid, draft.get("theme", "white"))
            _GAME_MODE_DRAFT.pop((gid, q.from_user.id), None)
            await q.answer("Saved.")
            await q.edit_message_text("Table colour settings saved.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=_gm_cb(gid, "menu"))]]))
            return
    if action == "lblimit":
        value = await db.get_leaderboard_rank_limit(gid)
        _GAME_MODE_DRAFT[(gid, q.from_user.id)] = {"mode": "lblimit", "value": value}
        await q.answer()
        await q.edit_message_text(
            f"Number of players to show on leaderboard rank.\n\nMax: 20 (0=unlimited)\n\nCurrent: {'Unlimited' if value == 0 else value}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("−", callback_data=_gm_cb(gid, "lblimitminus")),
                 InlineKeyboardButton("+", callback_data=_gm_cb(gid, "lblimitplus"))],
                [InlineKeyboardButton("Save", callback_data=_gm_cb(gid, "lblimitsave"))],
                [InlineKeyboardButton("Back", callback_data=_gm_cb(gid, "menu"))],
            ])
        )
        return
    if action in ("lblimitplus", "lblimitminus"):
        draft = _GAME_MODE_DRAFT.setdefault((gid, q.from_user.id), {"mode": "lblimit", "value": await db.get_leaderboard_rank_limit(gid)})
        value = int(draft.get("value", 20))
        if action == "lblimitplus":
            value = 1 if value == 0 else min(20, value + 1)
        else:
            value = 0 if value == 1 else max(1, value - 1)
        draft.update(mode="lblimit", value=value)
        await q.answer()
        await q.edit_message_text(
            f"Number of players to show on leaderboard rank.\n\nMax: 20 (0=unlimited)\n\nCurrent: {'Unlimited' if value == 0 else value}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("−", callback_data=_gm_cb(gid, "lblimitminus")),
                 InlineKeyboardButton("+", callback_data=_gm_cb(gid, "lblimitplus"))],
                [InlineKeyboardButton("Save", callback_data=_gm_cb(gid, "lblimitsave"))],
                [InlineKeyboardButton("Back", callback_data=_gm_cb(gid, "menu"))],
            ])
        )
        return
    if action == "lblimitsave":
        draft = _GAME_MODE_DRAFT.pop((gid, q.from_user.id), {"value": 20})
        value = max(0, min(20, int(draft.get("value", 20))))
        await db.set_leaderboard_rank_limit(gid, value)
        await q.answer("Saved.")
        await q.edit_message_text(
            f"Leaderboard rank limit saved: {'Unlimited' if value == 0 else value}.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=_gm_cb(gid, "menu"))]])
        )
        return
    if action == "pointsinfo":
        await q.answer()
        draft = await db.get_pvp_settings(gid)
        _pvp_cfg_draft[gid] = draft
        await q.message.reply_text(_pvp_cfg_text(draft), parse_mode="HTML", reply_markup=_pvp_cfg_keyboard(draft))
        return
    if action == "houseinfo":
        await q.answer()
        cfg = await db.get_gamemode_config(gid)
        await q.edit_message_text("<b>House Settings 2</b>",parse_mode="HTML",reply_markup=_house2_keyboard(gid,cfg["house_points_enabled"]))
        return
    if action == "bump":
        if not mode or not (mode.get("active") or mode.get("pending")):
            value = await db.tomochi_luck_bump_minutes(gid)
        else:
            value = int(mode.get("bump_alert_minutes", 30) or 0)
        _GAME_MODE_DRAFT[(gid, q.from_user.id)] = {"mode": "bump", "value": value}
        await q.answer()
        await q.edit_message_text(_bump_settings_text(value), parse_mode="HTML", reply_markup=_bump_digit_keyboard(gid))
        return
    if action.startswith("bumpdigit:"):
        digit = action.split(":", 1)[1]
        draft = _GAME_MODE_DRAFT.setdefault((gid, q.from_user.id), {"mode": "bump", "value": 30})
        value = int(draft.get("value", 30))
        value = value // 10 if digit == "del" else min(999999, value * 10 + int(digit))
        draft.update(mode="bump", value=value)
        await q.answer()
        await q.edit_message_text(_bump_settings_text(value), parse_mode="HTML", reply_markup=_bump_digit_keyboard(gid))
        return
    if action == "bumpsave":
        draft = _GAME_MODE_DRAFT.pop((gid, q.from_user.id), {"value": 30})
        current_mode = await db.get_game_mode(gid)
        if not current_mode or not (current_mode.get("active") or current_mode.get("pending")):
            await db.set_tomochi_luck_bump_minutes(gid, int(draft.get("value", 30)))
            updated = {"bump_alert_minutes": int(draft.get("value", 30))}
        else:
            updated = await db.set_game_mode_bump_alert(gid, int(draft.get("value", 30)))
        await _schedule_game_bump(context, gid)
        await q.answer("Saved.")
        await q.edit_message_text(_bump_settings_text(int((updated or {}).get("bump_alert_minutes", 30) or 0)), parse_mode="HTML",
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=_gm_cb(gid, "menu"))]]))
        return
    if action=="menu":
        await q.answer(); await q.edit_message_text(_gm_menu_text(mode),parse_mode="HTML",reply_markup=_gm_menu_keyboard(gid,mode)); return
    if action=="new":
        await q.answer()
        await q.edit_message_text("<b>Choose game mode</b>",parse_mode="HTML",reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Highest Score Wins",callback_data=_gm_cb(gid,"highest"))],
            [InlineKeyboardButton("First to x Score",callback_data=_gm_cb(gid,"first"))],
            [InlineKeyboardButton("Cancel",callback_data=_gm_cb(gid,"menu"))]])); return
    if action == "luck":
        enabled = await db.tomochi_luck_house_enabled(gid)
        await q.answer()
        await q.edit_message_text("<b>Tomochi Luck settings</b>", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Disable House" if enabled else "Enable House", callback_data=_gm_cb(gid,"lucktoggle"))],
                [InlineKeyboardButton("Clear Luck Leaderboard", callback_data=_gm_cb(gid,"luckclear"))],
                [InlineKeyboardButton("House on lb ON" if await db.tomochi_luck_house_on_lb(gid) else "House on lb OFF", callback_data=_gm_cb(gid,"lucklb_toggle"))],
                [InlineKeyboardButton("Joker Config", callback_data=_gm_cb(gid,"jokerconfig"))],
                [InlineKeyboardButton("Back", callback_data=_gm_cb(gid,"menu"))]]))
        return
    if action == "lucktoggle":
        enabled = await db.tomochi_luck_house_enabled(gid)
        await db.set_tomochi_luck_house_enabled(gid, not enabled)
        await q.answer("Saved.")
        await q.edit_message_text("<b>Tomochi Luck settings</b>", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Disable House" if not enabled else "Enable House", callback_data=_gm_cb(gid,"lucktoggle"))],
                [InlineKeyboardButton("Clear Luck Leaderboard", callback_data=_gm_cb(gid,"luckclear"))],
                [InlineKeyboardButton("House on lb ON" if await db.tomochi_luck_house_on_lb(gid) else "House on lb OFF", callback_data=_gm_cb(gid,"lucklb_toggle"))],
                [InlineKeyboardButton("Joker Config", callback_data=_gm_cb(gid,"jokerconfig"))],
                [InlineKeyboardButton("Back", callback_data=_gm_cb(gid,"menu"))]]))
        return
    if action == "lucklb_toggle":
        enabled = await db.tomochi_luck_house_on_lb(gid)
        await db.set_tomochi_luck_house_on_lb(gid, not enabled)
        await q.answer("Saved.")
        enabled_now = not enabled
        await q.edit_message_text("<b>Tomochi Luck settings</b>", parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Disable House" if await db.tomochi_luck_house_enabled(gid) else "Enable House", callback_data=_gm_cb(gid,"lucktoggle"))],
                [InlineKeyboardButton("Clear Luck Leaderboard", callback_data=_gm_cb(gid,"luckclear"))],
                [InlineKeyboardButton("House on lb ON" if enabled_now else "House on lb OFF", callback_data=_gm_cb(gid,"lucklb_toggle"))],
                [InlineKeyboardButton("Joker Config", callback_data=_gm_cb(gid,"jokerconfig"))],
                [InlineKeyboardButton("Back", callback_data=_gm_cb(gid,"menu"))]]))
        return
    if action == "luckclear":
        await q.answer()
        await q.edit_message_text("Completely clear Tomochi Luck Leaderboard stats and History.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Clear", callback_data=_gm_cb(gid,"luckclearconfirm")),
                                                InlineKeyboardButton("Cancel", callback_data=_gm_cb(gid,"luck"))]]))
        return
    if action == "luckclearconfirm":
        await db.clear_tomochi_luck(gid)
        await q.answer("Cleared.")
        await q.edit_message_text("Tomochi Luck leaderboard and history cleared.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=_gm_cb(gid,"menu"))]]))
        return
    if action in ("highest","first"):
        if mode and (mode.get("active") or mode.get("pending")):
            if mode["mode"]!=action:
                await q.answer("End or reset the current game before choosing another mode.",show_alert=True); return
            kb=await _gm_control_keyboard(gid,mode)
            await q.answer(); await q.edit_message_text(_gm_control_text(mode),parse_mode="HTML",reply_markup=kb); return
        await q.answer()
        label = "First to x points" if action == "first" else "Highest Score Wins"
        await q.edit_message_text(f"<b>{label}</b>",parse_mode="HTML",reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Start New Game",callback_data=_gm_cb(gid,f"startnew:{action}"))],
            [InlineKeyboardButton("Joker Config x points" if action=="first" else "Joker config highest score",
                                  callback_data=_gm_cb(gid,f"modejokeropen:{action}"))],
            [InlineKeyboardButton("Back",callback_data=_gm_cb(gid,"menu"))]]))
        return
    if action.startswith("startnew:"):
        which=action.split(":",1)[1]
        _GAME_MODE_DRAFT[(gid,q.from_user.id)]={"mode":which,"value":0}
        await q.answer()
        await q.edit_message_text(_gm_start_text(which,0),parse_mode="HTML",reply_markup=_gm_digit_keyboard(gid,which))
        return
    if action.startswith("digit:"):
        _,which,digit=action.split(":",2)
        draft=_GAME_MODE_DRAFT.setdefault((gid,q.from_user.id),{"mode":which,"value":0})
        v=int(draft["value"])
        v=v//10 if digit=="del" else min(999999, v*10+int(digit))
        draft["value"]=v
        await q.answer(); await q.edit_message_text(_gm_start_text(which,v),parse_mode="HTML",reply_markup=_gm_digit_keyboard(gid,which)); return
    if action.startswith("save:"):
        which=action.split(":",1)[1]; draft=_GAME_MODE_DRAFT.pop((gid,q.from_user.id),{"value":0})
        v=max(0,int(draft["value"]))
        cancelled = await db.reset_group(gid)
        await db.set_tomochi_luck_leaderboard_visible(gid, False)
        for game in cancelled:
            _cancel_chat_jobs(context, gid, game["id"])
            await _delete_quietly(context.bot, gid, game["message_id"])
        await db.save_game_mode_pending(
            gid,
            which,
            v if which=="first" else 0,
            v if which=="highest" else 0,
        )
        pending = await db.get_game_mode(gid)
        await _post_game_mode_start(context, gid, pending)
        await q.answer("Saved.")
        await q.edit_message_text("Saved. Game begins after next completed /cards game.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back",callback_data=_gm_cb(gid,"menu"))]])); return
    if action in ("end","restart"):
        if not mode or not (mode.get("active") or mode.get("pending")):
            await q.answer("No active game mode.",show_alert=True); return
        label="First to x points" if mode["mode"]=="first" else "Highest Score Wins"
        if action=="end": text=f"end game clear leaderboard\n\nGame mode: {label}\n\nConfirm?"; yes="confirm_end"
        else: text=("restart current game, clear leaderboard, keep target" if mode["mode"]=="first" else "restart current highest score game, clear leaderboard")+"\n\nConfirm?"; yes="confirm_restart"
        await q.answer(); await q.edit_message_text(text,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Confirm",callback_data=_gm_cb(gid,yes)),InlineKeyboardButton("Cancel",callback_data=_gm_cb(gid,"menu"))]])); return
    if action in ("confirm_end","confirm_restart"):
        await db.reset_group(gid)
        if action=="confirm_end":
            await db.end_game_mode(gid)
            await db.set_tomochi_luck_leaderboard_visible(gid, False)
            msg="Game ended and leaderboard cleared."
        else:
            await db.reset_game_mode_for_restart(gid); msg="Game restarted. Game begins after next completed /cards game."
            restarted = await db.get_game_mode(gid)
            await _post_game_mode_start(context, gid, restarted)
        for job in context.job_queue.get_jobs_by_name(f"modeend:{gid}"): job.schedule_removal()
        await q.answer(); await q.edit_message_text(msg,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back",callback_data=_gm_cb(gid,"menu"))]])); return
    if action.startswith("history:"):
        which=action.split(":",1)[1]
        if not await db.game_mode_history_exists(gid,which): await q.answer("No winner history recorded.",show_alert=True); return
        label="First to x Points" if which=="first" else "Highest Score Wins"
        await q.answer(); await q.edit_message_text(f"clear {label} history at /history?",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Yes",callback_data=_gm_cb(gid,f"clearhistory:{which}")),InlineKeyboardButton("Cancel",callback_data=_gm_cb(gid,"menu"))]])); return
    if action.startswith("clearhistory:"):
        which=action.split(":",1)[1]; await db.clear_game_mode_history(gid,which)
        await q.answer("History cleared."); await q.edit_message_text("History cleared.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back",callback_data=_gm_cb(gid,"menu"))]])); return

    if action == "jokerconfig":
        try:
            cfg = await db.tomochi_luck_joker_settings(gid)
        except Exception:
            log.exception("Could not load Tomochi Luck joker settings")
            await q.answer("Joker settings unavailable. Try again in a moment.", show_alert=True)
            return
        _GAME_MODE_DRAFT[(gid, q.from_user.id)] = {"mode": "joker", "chance": cfg["chance"], "in_house": cfg["in_house"], "penalty": cfg["wins_penalty"]}
        # Open the control panel as a fresh message. The menu may be an
        # old/non-editable message, so relying on edit_message_text can make
        # the Joker Config button appear to do nothing.
        await q.answer()
        try:
            await q.message.reply_text(
                _joker_config_text(cfg),
                parse_mode="HTML",
                reply_markup=_joker_config_keyboard(gid, cfg),
            )
        except Exception:
            log.exception("Could not send Joker Config panel")
            try:
                await context.bot.send_message(
                    chat_id=q.message.chat_id,
                    text="Could not open Joker Config. Please try again.",
                )
            except Exception:
                log.exception("Could not send Joker Config error notice")
        return
    if action.startswith("jokerdigit:"):
        digit = action.split(":", 1)[1]
        draft = _GAME_MODE_DRAFT.setdefault((gid, q.from_user.id), {"mode": "joker", "chance": 8})
        chance = int(draft.get("chance", 8))
        if digit == "del":
            chance = chance // 10
        else:
            chance = min(100, chance * 10 + int(digit))
        draft["chance"] = chance
        cfg = {"chance": chance, "in_house": draft.get("in_house", True), "wins_penalty": draft.get("penalty", -1)}
        await q.answer()
        await q.edit_message_text(_joker_config_text(cfg), parse_mode="HTML", reply_markup=_joker_config_keyboard(gid, cfg))
        return
    if action == "jokerhouse":
        draft = _GAME_MODE_DRAFT.setdefault((gid, q.from_user.id), {"mode": "joker", "chance": 8, "in_house": True, "penalty": -1})
        draft["in_house"] = not draft.get("in_house", True)
        cfg = {"chance": draft.get("chance", 8), "in_house": draft["in_house"], "wins_penalty": draft.get("penalty", -1)}
        await q.answer()
        await q.edit_message_text(_joker_config_text(cfg), parse_mode="HTML", reply_markup=_joker_config_keyboard(gid, cfg))
        return
    if action in ("jokerpenalty+", "jokerpenalty-"):
        draft = _GAME_MODE_DRAFT.setdefault((gid, q.from_user.id), {"mode": "joker", "chance": 8, "penalty": -1})
        pen = int(draft.get("penalty", -1))
        pen = max(-10, min(-1, pen + (1 if action.endswith("+") else -1)))
        draft["penalty"] = pen
        cfg = {"chance": draft.get("chance", 8), "in_house": draft.get("in_house", True), "wins_penalty": pen}
        await q.answer()
        await q.edit_message_text(_joker_config_text(cfg), parse_mode="HTML", reply_markup=_joker_config_keyboard(gid, cfg))
        return
    if action == "jokersave":
        draft = _GAME_MODE_DRAFT.get((gid, q.from_user.id), {})
        await db.set_tomochi_luck_joker_chance(gid, int(draft.get("chance", 8)))
        await db.set_tomochi_luck_joker_in_house(gid, bool(draft.get("in_house", True)))
        await db.set_tomochi_luck_joker_wins_penalty(gid, int(draft.get("penalty", -1)))
        await q.answer("Saved.")
        await q.edit_message_text("Joker config saved.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data=_gm_cb(gid, "luck"))]]))
        return

    await q.answer("Not available.",show_alert=True)


async def history_cmd(update:Update,context:ContextTypes.DEFAULT_TYPE)->None:
    chat=update.effective_chat
    if chat.type not in ("group","supergroup"):
        await update.message.reply_text("Use /history in the group."); return
    await update.message.reply_html(await _history_text(chat.id),disable_web_page_preview=True)


async def resetlb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if chat.type not in ("group", "supergroup"):
        return
    await _delete_quietly(context.bot, chat.id, message.message_id if message else None)
    if not await _is_admin(context, chat.id, user.id):
        return

    mode = await db.get_game_mode(chat.id)
    if not mode or not (mode.get("active") or mode.get("pending")):
        msg = await context.bot.send_message(chat.id, "Reset Tomochi Luck leaderboard only in /gamemode")
        await _delete_after_seconds(context, chat.id, msg.message_id, 12, f"resetlb-result:{chat.id}:{msg.message_id}")
        return
    if mode and mode.get("active"):
        label = "First to x points" if mode["mode"] == "first" else "Highest Score Wins"
        prompt_text = (
            "Reset Tomochi Cards Leaderboard?\n\nAdmins Only\n\n"
            f"Game mode: {label} is on. This will also reset lb for current game only. "
            "To cancel current game enter controls at /gamemode"
        )
    else:
        prompt_text = "Reset Tomochi Card Leaderboard?\n\nAdmins Only"
    prompt = await context.bot.send_message(chat.id,prompt_text,reply_markup=reset_keyboard())
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

    mode = await db.get_game_mode(chat.id)
    cancelled = await db.reset_group(chat.id)
    for game in cancelled:
        _cancel_chat_jobs(context, chat.id, game["id"])
        await _delete_quietly(context.bot, chat.id, game["message_id"])
    restarted_mode = None
    if mode and mode.get("active"):
        restarted_mode = await db.reset_game_mode_for_restart(chat.id)
        for job in context.job_queue.get_jobs_by_name(f"modeend:{chat.id}"):
            job.schedule_removal()
    await _delete_quietly(context.bot, chat.id, query.message.message_id)
    if restarted_mode:
        await _post_game_mode_start(context, chat.id, restarted_mode)
        msg = "Tomochi Cards Leaderboard Reset!"
    else:
        msg = "Tomochi Cards Leaderboard Reset!"
    result_msg = await context.bot.send_message(chat.id, msg, parse_mode="HTML")
    await _delete_after_seconds(
        context,
        chat.id,
        result_msg.message_id,
        12,
        f"resetlb-result:{chat.id}:{result_msg.message_id}",
    )


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
    lobby_cfg = await db.get_gamemode_config(chat_id)
    game = await db.create_game(chat_id, "auto", expires_at(seconds=int(lobby_cfg["lobby_minutes"]) * 60), flavor=flavor)
    msg = await context.bot.send_message(
        chat_id,
        await _lobby_text_for_chat(chat_id, [], game["flavor"]),
        parse_mode="HTML",
        reply_markup=lobby_keyboard(game["id"], house_available=await db.house_enabled(chat_id), luck_mode=(await _effective_game_mode(chat_id)).get("mode") == "luck"),
        disable_web_page_preview=True,
    )
    await db.set_message_id(game["id"], msg.message_id)
    await _schedule_expire(context, game["id"], chat_id)


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

    rows = await db.pool().fetch(
        "SELECT chat_id FROM game_modes WHERE active=TRUE AND mode='highest'"
    )
    for row in rows:
        mode = await db.get_game_mode(int(row["chat_id"]))
        if mode:
            _schedule_mode_end(application, int(row["chat_id"]), mode)

    if GAME_CHAT_IDS:
        jq.run_repeating(
            hourly_job,
            interval=AUTO_INTERVAL_SECONDS,
            first=AUTO_INTERVAL_SECONDS,
            name="hourly_auto",
            data={"chat_ids": GAME_CHAT_IDS},
        )
