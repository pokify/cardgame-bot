import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
CARDS_DIR = ROOT / "assets" / "cards"

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
WIN_POINTS = int(os.environ.get("WIN_POINTS", "5"))
LOBBY_SECONDS = int(os.environ.get("LOBBY_SECONDS", "300"))
AUTO_INTERVAL_SECONDS = int(os.environ.get("AUTO_INTERVAL_SECONDS", "3600"))

_raw_chats = os.environ.get("GAME_CHAT_IDS", "").strip()
GAME_CHAT_IDS: list[int] = []
if _raw_chats:
    for part in _raw_chats.split(","):
        part = part.strip()
        if part:
            GAME_CHAT_IDS.append(int(part))

MAX_PLAYERS = 4
MIN_PLAYERS = 2

# Unique cards. Score is unique so a hand cannot tie.
CARDS: dict[str, dict] = {
    "pip2": {"file": "pip2.png", "label": "2", "score": 2},
    "pip3": {"file": "pip3.png", "label": "3", "score": 3},
    "pip4": {"file": "pip4.png", "label": "4", "score": 4},
    "pip5": {"file": "pip5.png", "label": "5", "score": 5},
    "pip6": {"file": "pip6.png", "label": "6", "score": 6},
    "pip7": {"file": "pip7.png", "label": "7", "score": 7},
    "pip8": {"file": "pip8.png", "label": "8", "score": 8},
    "pip9": {"file": "pip9.png", "label": "9", "score": 9},
    "pip10": {"file": "pip10.png", "label": "10", "score": 10},
    "ace": {"file": "ace.png", "label": "Ace", "score": 11},
    "jack": {"file": "jack.png", "label": "Jack", "score": 12},
    "king": {"file": "king.png", "label": "King", "score": 13},
    "queen": {"file": "queen.png", "label": "Queen", "score": 14},
    "joker": {"file": "joker.png", "label": "Joker", "score": 15},
}


def normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://") :]
    return url
