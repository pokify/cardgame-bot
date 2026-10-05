from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from telegram import InputFile, Update, InlineKeyboardButton, InlineKeyboardMarkup
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


async def _lobby_text_for_chat(chat_id: int, players: list, flavor: str | None) -> str:
    settings = await db.get_pvp_settings(chat_id)
    mode = await db.get_game_mode(chat_id)
    settings = dict(settings or {})
    settings["_game_mode"] = mode
    return lobby_text(players, flavor, settings)


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
        return "@" + str(username).lstrip("@")
    name = row["first_name"] or "player"
    return "@" + str(name).replace("<", "").replace(">", "")


def _leaderboard_html(rows, show_house: bool = True) -> str:
    if not rows:
        return "No games yet. Start one with /cards."

    lines = ["<b>Tomochi Cards Leaderboard</b>", ""]
    lines.append("H(House) W(Wins) P(Plays)")
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
        stats = f"Score: {score} (H:{house_pts}) | W:{wins} | P:{played}"
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

    try:
        msg = await update.message.reply_html(
            lobby_text(players, game["flavor"], pvp_settings),
            reply_markup=lobby_keyboard(game["id"], house_available=house_on),
            disable_web_page_preview=True,
        )
    except Exception:
        log.exception("Could not send cards lobby message")
        await db.set_status(game["id"], "expired")
        await update.message.reply_text("Could not create the lobby. Try again.")
        return

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
            text = await _lobby_text_for_chat(chat.id, players, game["flavor"])
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

    # House challenges count toward the same combined leaderboard W/P totals
    # as PvP. Score and H are both changed by the House result.
    await db.apply_house_result(
        chat_id,
        player,
        delta,
        won=player_won,
    )

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
        reply_markup=winner_keyboard(),
        disable_web_page_preview=True,
    )

    await db.set_status(game_id, "finished")

    # House results use the same combined Score/W/P as PvP, so they also
    # participate in both active game modes.
    mode = await db.get_game_mode(chat_id)
    if mode and mode.get("active"):
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
    await _record_mode_after_cards(context, chat_id, players)


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


async def houseconfig_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat = query.message.chat
    user = query.from_user
    if not await _is_admin(context, chat.id, user.id):
        await query.answer("Admins only.", show_alert=True)
        return
    draft = _house_cfg_draft.setdefault(
        chat.id, {"house_enabled": True, "max_plays_per_day": None, "pvp_before_unlock": 1}
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
            chat.id,
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



_GAME_MODE_DRAFT: dict[tuple[int,int],dict] = {}
_MODE_WINNER_EMOJI = '<tg-emoji emoji-id="5882196778341637440">🕺</tg-emoji>'


def _gm_cb(group_id:int, action:str)->str:
    return f"gm:{group_id}:{action}"


def _gm_menu_text(mode):
    if mode and mode.get("active"):
        label="First to x points" if mode["mode"]=="first" else "Highest Score Wins"
        return f"<b>Start Tomochi Cards game mode</b>\n\nGame mode active: {label}"
    if mode and mode.get("pending"):
        label="First to x points" if mode["mode"]=="first" else "Highest Score Wins"
        return f"<b>Start Tomochi Cards game mode</b>\n\nSaved: {label}\nGame begins after next completed /cards game."
    return "<b>Start Tomochi Cards game mode</b>"


def _gm_menu_keyboard(group_id:int,mode):
    if mode and mode.get("active"):
        rows=[[InlineKeyboardButton("Highest Score Wins",callback_data=_gm_cb(group_id,"highest")),
               InlineKeyboardButton("First to x Score",callback_data=_gm_cb(group_id,"first"))]]
    else:
        rows=[[InlineKeyboardButton("Start New Game",callback_data=_gm_cb(group_id,"new"))]]
    return InlineKeyboardMarkup(rows)


def _gm_control_text(mode):
    label="First to x points" if mode["mode"]=="first" else "Highest Score Wins"
    value=f"Target: {int(mode['target'])}" if mode["mode"]=="first" else f"Days: {int(mode['days'])}"
    return f"<b>{label}</b>\n\nControls:\n{value}"


async def _gm_control_keyboard(group_id:int,mode):
    rows=[]
    if mode.get("active"):
        rows.append([InlineKeyboardButton("End Current Game",callback_data=_gm_cb(group_id,"end"))])
        label="Reset game keep current target" if mode["mode"]=="first" else "Restart game clear leaderboard"
        rows.append([InlineKeyboardButton(label,callback_data=_gm_cb(group_id,"restart"))])
        if await db.game_mode_history_exists(group_id,mode["mode"]):
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
    if mode.get("mode") == "highest":
        text = "New Highest Score Wins game has started"
    else:
        text = f"New First to {int(mode.get('target') or 0)} Points game has started"
    await context.bot.send_message(
        chat_id,
        text,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Play Cards", callback_data="playcards")]]
        ),
    )


async def _activate_pending_mode_after_cards(context,chat_id:int):
    mode=await db.get_game_mode(chat_id)
    if not mode or not mode.get("pending") or mode.get("active"):
        return
    mode=await db.activate_game_mode(chat_id)
    if mode:
        await _post_game_mode_start(context, chat_id, mode)
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
        text=f"Target met!\nTomochi Cards Winner! {_MODE_WINNER_EMOJI}\n<b>@{username}</b> ({score}) points!\n\nNo1 rank locked.\n\nview /history"
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
    if not mode or not mode.get("active"):
        await _activate_pending_mode_after_cards(context,chat_id)
        return
    await db.game_mode_participation(chat_id,mode["mode"],players)
    if mode["mode"]=="first":
        winner=await db.check_first_to_winner(chat_id)
        if winner:
            await _mode_announce_winner(context,chat_id,winner)


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
            if remaining<=0: end="Ended"
            elif remaining<=86400: end=f"{max(1,int((remaining+3599)//3600))}hrs"
            elif int(mode.get("days") or 0) in (1,2,3): end=f"{int(mode['days'])*24}hrs"
            else: end=f"{int((remaining+86399)//86400)} days"
        lines=["<b>Game: Highest Score Wins</b>",f"Ends: {end}"]
    if mode.get("winner_user_id"):
        lines.append(f"Winner: @{mode.get('winner_username') or mode['winner_user_id']}")
    return "\n".join(lines)


async def _history_text(chat_id:int)->str:
    first=await db.game_mode_history(chat_id,"first")
    high=await db.game_mode_history(chat_id,"highest")
    if not first and not high:
        return "No history. Game mode winners history presents here after first winner recorded."
    def section(title,rows):
        if not rows: return ""
        lines=[title,"","         Winners   |     Wins  |    Plays"]
        last=None; rank=0
        for n,row in enumerate(rows,1):
            wins=int(row["wins"])
            if wins!=last: rank=n; last=wins
            name=row["username"] or str(row["user_id"])
            lines.append(f"{rank} {name:<18} {wins:>4} {int(row['plays']):>10}")
        return "\n".join(lines)
    sections=[]
    # Most-played mode first.
    if first and high:
        fp=sum(int(r["plays"]) for r in first); hp=sum(int(r["plays"]) for r in high)
        if hp>fp: sections=[section("Highest Score game mode winners",high),section("First to x Points game mode winners",first)]
        else: sections=[section("First to x Points game mode winners",first),section("Highest Score game mode winners",high)]
    else:
        sections=[section("First to x Points game mode winners",first),section("Highest Score game mode winners",high)]
    return "<b>Tomochi Cards Winners History</b>\n\n"+"\n\n".join(x for x in sections if x)


async def _cards_lb(chat_id: int) -> str:
    rows = await db.leaderboard(chat_id)
    html = _leaderboard_html(rows)
    mode = await db.get_game_mode(chat_id)
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
    await update.message.reply_html(html, disable_web_page_preview=True)


async def playcards_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    # Reuse the normal /cards path in the group where the button was pressed.
    if query.message and query.message.chat.type in ("group", "supergroup"):
        await cards_cmd(update, context)


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
                await _lobby_text_for_chat(chat_id, players, game["flavor"]),
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
        await context.bot.send_message(
            chat.id,
            "game mode settings",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Open", url="https://t.me/tomochicardbot?start=gamemode_" + str(chat.id))]
            ]),
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
        await update.message.reply_html(
            _gm_menu_text(mode),
            reply_markup=_gm_menu_keyboard(gid, mode),
        )
        return

    return


async def gamemode_cb(update:Update,context:ContextTypes.DEFAULT_TYPE)->None:
    q=update.callback_query; p=q.data.split(":",2)
    if len(p)<3: await q.answer(); return
    try: gid=int(p[1])
    except ValueError: await q.answer(); return
    if not await _is_admin(context,gid,q.from_user.id):
        await q.answer("Admins only.",show_alert=True); return
    action=p[2]; mode=await db.get_game_mode(gid)
    if action=="menu":
        await q.answer(); await q.edit_message_text(_gm_menu_text(mode),parse_mode="HTML",reply_markup=_gm_menu_keyboard(gid,mode)); return
    if action=="new":
        await q.answer()
        await q.edit_message_text("<b>Choose game mode</b>",parse_mode="HTML",reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Highest Score Wins",callback_data=_gm_cb(gid,"highest"))],
            [InlineKeyboardButton("First to x Score",callback_data=_gm_cb(gid,"first"))],
            [InlineKeyboardButton("Cancel",callback_data=_gm_cb(gid,"menu"))]])); return
    if action in ("highest","first"):
        if mode and mode.get("active"):
            if mode["mode"]!=action:
                await q.answer("End or reset the current game before choosing another mode.",show_alert=True); return
            kb=await _gm_control_keyboard(gid,mode)
            await q.answer(); await q.edit_message_text(_gm_control_text(mode),parse_mode="HTML",reply_markup=kb); return
        _GAME_MODE_DRAFT[(gid,q.from_user.id)]={"mode":action,"value":0}
        await q.answer(); await q.edit_message_text(_gm_start_text(action,0),parse_mode="HTML",reply_markup=_gm_digit_keyboard(gid,action)); return
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
        await db.save_game_mode_pending(gid,which,v if which=="first" else 0,v if which=="highest" else 0)
        pending = await db.get_game_mode(gid)
        await _post_game_mode_start(context, gid, pending)
        await q.answer("Saved.")
        await q.edit_message_text("Saved. Game begins after next completed /cards game.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back",callback_data=_gm_cb(gid,"menu"))]])); return
    if action in ("end","restart"):
        if not mode or not mode.get("active"): await q.answer("No active game mode.",show_alert=True); return
        label="First to x points" if mode["mode"]=="first" else "Highest Score Wins"
        if action=="end": text=f"end game clear leaderboard\n\nGame mode: {label}\n\nConfirm?"; yes="confirm_end"
        else: text=("restart current game, clear leaderboard, keep target" if mode["mode"]=="first" else "restart current highest score game, clear leaderboard")+"\n\nConfirm?"; yes="confirm_restart"
        await q.answer(); await q.edit_message_text(text,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Confirm",callback_data=_gm_cb(gid,yes)),InlineKeyboardButton("Cancel",callback_data=_gm_cb(gid,"menu"))]])); return
    if action in ("confirm_end","confirm_restart"):
        await db.reset_group(gid)
        if action=="confirm_end":
            await db.end_game_mode(gid); msg="Game ended and leaderboard cleared."
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
    if mode and mode.get("active"):
        await db.reset_game_mode_for_restart(chat.id)
        for job in context.job_queue.get_jobs_by_name(f"modeend:{chat.id}"):
            job.schedule_removal()
    await _delete_quietly(context.bot, chat.id, query.message.message_id)
    if mode and mode.get("active"):
        label = "Highest Score Wins" if mode["mode"] == "highest" else "First to x Points"
        msg = (
            "Tomochi Cards Leaderboard Reset!\n\n"
            f"<b>Game mode ({label})</b> will begin again after the next completed /cards game."
        )
    else:
        msg = "Tomochi Cards Leaderboard Reset!"
    await context.bot.send_message(chat.id, msg, parse_mode="HTML")


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
        await _lobby_text_for_chat(chat_id, [], game["flavor"]),
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