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


def lobby_keyboard(game_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Join Game", callback_data=f"join:{game_id}")]]
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
