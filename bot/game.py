from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import CARDS, LOBBY_SECONDS, MAX_PLAYERS, MIN_PLAYERS

JOKER_PENALTY = 5

ALWAYS_LINES = [
    "Feeling lucky? 😚",
    "Balls tingling 🤧",
    "Full send, no fold 😎",
    "You win, you ape 💀",
    "One hand away from greatness 🤩",
    "Are u sure? Mototo is dealing 😭",
    "In Poki we trust 🙏",
    "Drinks are on Roz! 🐰",
    "A bit of luck from Luna! 😘",
]

NOT_FIRST_LINES = [
    "Coming for the top spot? 😏",
    "Luck owes you one 🤔",
    "No crying 😭",
]

NOT_FIRST_ON_BOARD_LINES = [
    "Back for more? 😅",
    "Down bad, doubling down 😤",
    "Ahh shh, here we go again 😂",
]


def mention(username: str | None, first_name: str | None, user_id: int) -> str:
    if username:
        return f"@{username}"
    safe = (first_name or "player").replace("<", "").replace(">", "")
    return f'<a href="tg://user?id={user_id}">{safe}</a>'


def player_line(players: list) -> str:
    parts = [mention(p["username"], p["first_name"], p["user_id"]) for p in players]
    return ", ".join(parts) if parts else "—"


def lobby_keyboard(game_id: int, house_available: bool = True) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("Join Game", callback_data=f"join:{game_id}")]
    ]
    if house_available:
        rows.append(
            [InlineKeyboardButton("Challenge The House", callback_data=f"house:{game_id}")]
        )
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
        [[InlineKeyboardButton("View Leaderboard", callback_data="showlb")]]
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


def lobby_text(players: list, flavor: str | None = None) -> str:
    n = len(players)
    flavor_block = f"{flavor}\n\n" if flavor else ""
    return (
        "<b>Tomochi Card!</b>\n\n"
        f"{flavor_block}"
        "Highest Tomochi card wins!\n\n"
        f"Waiting for players… ({n}/{MAX_PLAYERS})\n"
        f"{MIN_PLAYERS} minimum\n\n"
        f"Players: {player_line(players)}"
    )


def deal_house(player: dict) -> tuple[list[dict], dict, dict]:
    """Deal one non-Joker card to the player and one non-Joker card to the House."""
    keys = random.sample([key for key in CARDS if key != "joker"], 2)
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


def deal(players: list) -> tuple[list[dict], dict, dict | None]:
    keys = random.sample(list(CARDS.keys()), len(players))
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


def expires_at(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now + timedelta(seconds=LOBBY_SECONDS)
