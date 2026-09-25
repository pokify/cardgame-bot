from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import CARDS, LOBBY_SECONDS, MAX_PLAYERS, MIN_PLAYERS


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


def lobby_text(mode: str, players: list) -> str:
    n = len(players)
    title = "Auto Card Game!" if mode == "auto" else "Card Game"
    extra = ""
    if mode == "auto" and n == 0:
        extra = (
            f"\n\nThis game will expire in {LOBBY_SECONDS // 60} minutes "
            "if not enough players join."
        )
    return (
        f"<b>{title}</b>\n\n"
        f"Card Game starting! Need at least {MIN_PLAYERS} players "
        f"(up to {MAX_PLAYERS}).\n\n"
        f"Waiting for players… ({n}/{MAX_PLAYERS}, need {MIN_PLAYERS}+)\n\n"
        f"Players: {player_line(players)}"
        f"{extra}"
    )


def deal(players: list) -> tuple[list[dict], dict]:
    keys = random.sample(list(CARDS.keys()), len(players))
    assignments = []
    for player, key in zip(players, keys):
        assignments.append(
            {
                "user_id": player["user_id"],
                "username": player["username"],
                "first_name": player["first_name"],
                "card_key": key,
                "score": CARDS[key]["score"],
                "label": CARDS[key]["label"],
            }
        )
    winner = max(assignments, key=lambda a: a["score"])
    return assignments, winner


def expires_at(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now + timedelta(seconds=LOBBY_SECONDS)
