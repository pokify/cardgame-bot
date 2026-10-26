from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import CARDS, LOBBY_SECONDS, MAX_PLAYERS, MIN_PLAYERS

JOKER_PENALTY = 5

ALWAYS_LINES = [
    "Feeling lucky? <tg-emoji emoji-id=\"5936069085711310532\">🍀</tg-emoji>",
    "Balls tingling <tg-emoji emoji-id=\"5881767088338512130\">😉</tg-emoji>",
    "Full send, no fold <tg-emoji emoji-id=\"5963040535437383118\">😎</tg-emoji>",
    "You win, you ape <tg-emoji emoji-id=\"5936276811804581914\">💀</tg-emoji>",
    "One hand away from greatness <tg-emoji emoji-id=\"5936202100848467433\">⭐️</tg-emoji>",
    "Are u sure? Mototo is dealing <tg-emoji emoji-id=\"5936233170641889180\">😃</tg-emoji>",
    "In Poki we trust <tg-emoji emoji-id=\"5881723747823524353\">😏</tg-emoji>",
    "Drinks are on Roz! <tg-emoji emoji-id=\"5933961609683673255\">😏</tg-emoji>",
    "A bit of luck from Luna! <tg-emoji emoji-id=\"5935913023779649970\">☺️</tg-emoji>",
]

NOT_FIRST_LINES = [
    "Coming for the top spot? <tg-emoji emoji-id=\"5962786385042610279\">👑</tg-emoji>",
    "Luck owes you one <tg-emoji emoji-id=\"5934052315097994710\">💸</tg-emoji>",
    "No crying <tg-emoji emoji-id=\"5882178030809391269\">😱</tg-emoji>",
]

NOT_FIRST_ON_BOARD_LINES = [
    "Back for more? <tg-emoji emoji-id=\"5963294565573072794\">🥊</tg-emoji>",
    "Down bad, doubling down <tg-emoji emoji-id=\"5882216496536493419\">🕸️</tg-emoji>",
    "Ahh shh, here we go again <tg-emoji emoji-id=\"5933800926367192635\">😓</tg-emoji>",
]


def mention(username: str | None, first_name: str | None, user_id: int) -> str:
    if username:
        return f"@{username}"
    safe = (first_name or "player").replace("<", "").replace(">", "")
    return f'<a href="tg://user?id={user_id}">{safe}</a>'


def player_line(players: list) -> str:
    parts = [mention(p["username"], p["first_name"], p["user_id"]) for p in players]
    return ", ".join(parts) if parts else "—"


def lobby_keyboard(game_id: int, house_available: bool = True, luck_mode: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("Join Game", callback_data=f"join:{game_id}")]
    ]
    if not luck_mode and house_available:
        rows.append([InlineKeyboardButton("Challenge The House", callback_data=f"house:{game_id}")])
    if not luck_mode:
        rows.append([InlineKeyboardButton("Points", callback_data=f"points:{game_id}")])
    return InlineKeyboardMarkup(rows)


def house_confirm_keyboard(game_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Continue", callback_data=f"house_continue:{game_id}"),
            InlineKeyboardButton("Cancel", callback_data=f"house_cancel:{game_id}"),
        ]]
    )


def winner_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("View Leaderboard", callback_data="showlb")],
            [InlineKeyboardButton("Play", callback_data="playcards")],
        ]
    )


def reset_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Yes", callback_data="resetlb:yes"),
                InlineKeyboardButton("No", callback_data="resetlb:no"),
            ]
        ]
    )


def pick_flavor(standing: dict | None) -> str:
    pool = list(ALWAYS_LINES)
    if standing:
        if not standing.get("is_first"):
            pool.extend(NOT_FIRST_LINES)
            if standing.get("on_board"):
                pool.extend(NOT_FIRST_ON_BOARD_LINES)
    return random.choice(pool)


def pvp_base_bonus_for(settings: dict, n_players: int) -> tuple[int, int]:
    """Return the configured baseline and join bonus for a player count."""
    if n_players <= 1:
        return 0, 0
    if n_players == 2:
        base, bonus = settings["base_2"], settings["bonus_2"]
    elif n_players == 3:
        base, bonus = settings["base_3"], settings["bonus_3"]
    else:
        base, bonus = settings["base_4"], settings["bonus_4"]
    base = max(0, int(base))
    bonus = max(0, int(bonus)) if base > 0 else 0
    return base, bonus


def lobby_text(
    players: list,
    flavor: str | None = None,
    pvp_settings: dict | None = None,
) -> str:
    n = len(players)
    flavor_block = f"{flavor}\n\n" if flavor else ""
    settings = pvp_settings or {}
    if settings:
        base, bonus = pvp_base_bonus_for(settings, n)
    else:
        base, bonus = 0, 0

    mode = settings.get("_game_mode") or {}
    mode_line = None
    luck_mode = mode.get("active") and mode.get("mode") == "luck"
    if mode.get("active"):
        if mode.get("mode") == "luck":
            mode_line = "<b>Game mode: Tomochi Luck</b>"
        elif mode.get("mode") == "highest":
            mode_line = "<b>Game mode: Highest Score Wins</b>"
        elif mode.get("mode") == "first":
            mode_line = f"<b>Game mode: First to {int(mode.get('target') or 0)} points</b>"

    if luck_mode:
        return "\n".join(line for line in [
            mode_line, "", "<b>New hand has started!</b>", "",
            flavor_block.rstrip("\n") if flavor_block else None,
            "", "",
            "Highest Tomochi card wins!",
            "", "", "",
            f"♢ Waiting for players… ({n}/{MAX_PLAYERS}) Min. {int(settings.get('_min_players', MIN_PLAYERS))}", "",
            "1 player auto-plays the House" if (n == 1 and settings.get("_house_enabled", True)) else None,
            "", f"Players: {player_line(players)}"
        ] if line is not None)

    lines = [
        mode_line,
        "" if mode_line else None,
        "" if mode_line else None,
        "<b>New hand has started!</b>",
        "",
        flavor_block.rstrip("\n") if flavor_block else None,
        "\nHighest Tomochi card wins!\n",
        "",
        f"♢ Waiting for players… ({n}/{MAX_PLAYERS}) Min. {int(settings.get('_min_players', MIN_PLAYERS))}",
        f"♤ Points to win: {base}",
    ]
    if bonus > 0:
        lines.append(f"♧ Join bonus: +{bonus}")
    lines.extend(["", f"Players: {player_line(players)}"])
    return "\n".join(line for line in lines if line is not None)


def deal_house(player: dict, banned: list[str] | None = None) -> tuple[list[dict], dict, dict]:
    """Deal one non-Joker card to the player and one non-Joker card to the House."""
    banned_set = set(banned or [])
    pool = [key for key in CARDS if key != "joker" and key not in banned_set]
    if len(pool) < 2:
        pool = [key for key in CARDS if key != "joker"]
    keys = random.sample(pool, 2)
    player_key, house_key = keys

    assignments = [
        {
            "user_id": player["user_id"],
            "username": player["username"],
            "first_name": player["first_name"],
            "card_key": player_key,
            "score": CARDS[player_key]["score"],
            "display_score": CARDS[player_key]["score"],
            "label": CARDS[player_key]["label"],
        },
        {
            "user_id": 0,
            "username": "House",
            "first_name": None,
            "card_key": house_key,
            "score": CARDS[house_key]["score"],
            "display_score": CARDS[house_key]["score"],
            "label": CARDS[house_key]["label"],
        },
    ]

    winner = assignments[0] if assignments[0]["score"] > assignments[1]["score"] else assignments[1]
    return assignments, winner, assignments[1]


def deal(players: list, banned: list[str] | None = None) -> tuple[list[dict], dict, dict | None]:
    banned_set = set(banned or [])
    pool = [key for key in CARDS if key not in banned_set]
    if len(pool) < len(players):
        pool = list(CARDS.keys())
    keys = random.sample(pool, len(players))
    assignments = []
    for player, key in zip(players, keys):
        face = CARDS[key]["score"]
        assignments.append(
            {
                "user_id": player["user_id"],
                "username": player["username"],
                "first_name": player["first_name"],
                "card_key": key,
                "score": face,
                "display_score": -JOKER_PENALTY if key == "joker" else face,
                "label": CARDS[key]["label"],
            }
        )
    joker = next((a for a in assignments if a["card_key"] == "joker"), None)
    contenders = [a for a in assignments if a["card_key"] != "joker"]
    winner = max(contenders, key=lambda a: a["score"])
    return assignments, winner, joker


def expires_at(now: datetime | None = None, seconds: int = LOBBY_SECONDS) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now + timedelta(seconds=max(1, int(seconds)))
