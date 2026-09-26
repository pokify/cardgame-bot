from __future__ import annotations

import asyncpg

from bot.config import normalize_database_url

_pool: asyncpg.Pool | None = None


async def connect(database_url: str) -> asyncpg.Pool:
    global _pool
    _pool = await asyncpg.create_pool(
        dsn=normalize_database_url(database_url),
        min_size=1,
        max_size=5,
    )
    await init_schema()
    return _pool


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool is not initialised")
    return _pool


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def init_schema() -> None:
    async with pool().acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS games (
                id              BIGSERIAL PRIMARY KEY,
                chat_id         BIGINT NOT NULL,
                status          TEXT NOT NULL,
                mode            TEXT NOT NULL,
                message_id      BIGINT,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at      TIMESTAMPTZ NOT NULL
            );

            CREATE INDEX IF NOT EXISTS games_chat_status_idx
                ON games (chat_id, status);

            CREATE TABLE IF NOT EXISTS game_players (
                id              BIGSERIAL PRIMARY KEY,
                game_id         BIGINT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
                user_id         BIGINT NOT NULL,
                username        TEXT,
                first_name      TEXT,
                join_order      INTEGER NOT NULL,
                card_key        TEXT,
                score           INTEGER,
                is_winner       BOOLEAN NOT NULL DEFAULT FALSE,
                UNIQUE (game_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS player_stats (
                chat_id         BIGINT NOT NULL,
                user_id         BIGINT NOT NULL,
                username        TEXT,
                first_name      TEXT,
                score           INTEGER NOT NULL DEFAULT 0,
                played          INTEGER NOT NULL DEFAULT 0,
                wins            INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            );
            """
        )
        await conn.execute(
            "ALTER TABLE games ADD COLUMN IF NOT EXISTS flavor TEXT"
        )


async def active_game(chat_id: int) -> asyncpg.Record | None:
    return await pool().fetchrow(
        """
        SELECT * FROM games
        WHERE chat_id = $1 AND status IN ('waiting', 'running')
        ORDER BY id DESC
        LIMIT 1
        """,
        chat_id,
    )


async def waiting_games() -> list[asyncpg.Record]:
    return await pool().fetch(
        "SELECT * FROM games WHERE status = 'waiting'"
    )


async def create_game(chat_id: int, mode: str, expires_at, flavor: str | None = None) -> asyncpg.Record:
    return await pool().fetchrow(
        """
        INSERT INTO games (chat_id, status, mode, expires_at, flavor)
        VALUES ($1, 'waiting', $2, $3, $4)
        RETURNING *
        """,
        chat_id,
        mode,
        expires_at,
        flavor,
    )


async def caller_standing(chat_id: int, user_id: int) -> dict:
    rows = await pool().fetch(
        """
        SELECT user_id FROM player_stats
        WHERE chat_id = $1
        ORDER BY score DESC, wins DESC, played ASC
        """,
        chat_id,
    )
    on_board = any(r["user_id"] == user_id for r in rows)
    rank = next((i for i, r in enumerate(rows, start=1) if r["user_id"] == user_id), None)
    return {"on_board": on_board, "rank": rank, "is_first": rank == 1}


async def set_message_id(game_id: int, message_id: int) -> None:
    await pool().execute(
        "UPDATE games SET message_id = $2 WHERE id = $1",
        game_id,
        message_id,
    )


async def get_game(game_id: int) -> asyncpg.Record | None:
    return await pool().fetchrow("SELECT * FROM games WHERE id = $1", game_id)


async def set_status(game_id: int, status: str) -> None:
    await pool().execute(
        "UPDATE games SET status = $2 WHERE id = $1",
        game_id,
        status,
    )


async def add_player(
    game_id: int,
    user_id: int,
    username: str | None,
    first_name: str | None,
) -> tuple[bool, str, list[asyncpg.Record]]:
    """Add a player if the lobby is still open. Returns (ok, reason, players)."""
    async with pool().acquire() as conn:
        async with conn.transaction():
            game = await conn.fetchrow(
                "SELECT * FROM games WHERE id = $1 FOR UPDATE",
                game_id,
            )
            if game is None:
                return False, "not_found", []
            if game["status"] != "waiting":
                return False, "not_waiting", []

            existing = await conn.fetch(
                "SELECT * FROM game_players WHERE game_id = $1 ORDER BY join_order",
                game_id,
            )
            if any(p["user_id"] == user_id for p in existing):
                return False, "already_in", list(existing)
            if len(existing) >= 4:
                return False, "full", list(existing)

            await conn.execute(
                """
                INSERT INTO game_players
                    (game_id, user_id, username, first_name, join_order)
                VALUES ($1, $2, $3, $4, $5)
                """,
                game_id,
                user_id,
                username,
                first_name,
                len(existing) + 1,
            )
            players = await conn.fetch(
                "SELECT * FROM game_players WHERE game_id = $1 ORDER BY join_order",
                game_id,
            )
            return True, "ok", list(players)


async def list_players(game_id: int) -> list[asyncpg.Record]:
    return await pool().fetch(
        "SELECT * FROM game_players WHERE game_id = $1 ORDER BY join_order",
        game_id,
    )


async def save_deal(game_id: int, assignments: list[dict], winner_user_id: int) -> None:
    async with pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE games SET status = 'finished' WHERE id = $1",
                game_id,
            )
            for row in assignments:
                await conn.execute(
                    """
                    UPDATE game_players
                    SET card_key = $3, score = $4, is_winner = $5
                    WHERE game_id = $1 AND user_id = $2
                    """,
                    game_id,
                    row["user_id"],
                    row["card_key"],
                    row["score"],
                    row["user_id"] == winner_user_id,
                )


async def bump_stats(
    chat_id: int,
    players: list,
    winner_user_id: int,
    points: int,
    joker_user_id: int | None = None,
    joker_penalty: int = 5,
) -> None:
    async with pool().acquire() as conn:
        async with conn.transaction():
            for p in players:
                uid = p["user_id"]
                is_win = uid == winner_user_id
                is_joker = joker_user_id is not None and uid == joker_user_id
                delta = 0
                if is_win:
                    delta += points
                if is_joker:
                    delta -= joker_penalty
                await conn.execute(
                    """
                    INSERT INTO player_stats
                        (chat_id, user_id, username, first_name, score, played, wins)
                    VALUES ($1, $2, $3, $4, $5, 1, $6)
                    ON CONFLICT (chat_id, user_id) DO UPDATE SET
                        username = EXCLUDED.username,
                        first_name = EXCLUDED.first_name,
                        score = player_stats.score + EXCLUDED.score,
                        played = player_stats.played + 1,
                        wins = player_stats.wins + EXCLUDED.wins
                    """,
                    chat_id,
                    uid,
                    p["username"],
                    p["first_name"],
                    delta,
                    1 if is_win else 0,
                )


async def leaderboard(chat_id: int, limit: int = 15) -> list[asyncpg.Record]:
    return await pool().fetch(
        """
        SELECT * FROM player_stats
        WHERE chat_id = $1
        ORDER BY score DESC, wins DESC, played ASC
        LIMIT $2
        """,
        chat_id,
        limit,
    )


async def reset_group(chat_id: int) -> list[asyncpg.Record]:
    """Wipe this group's leaderboard and cancel any waiting/running lobby.
    Does not start a new game. Returns cancelled games so callers can delete msgs.
    """
    async with pool().acquire() as conn:
        async with conn.transaction():
            games = await conn.fetch(
                """
                SELECT * FROM games
                WHERE chat_id = $1 AND status IN ('waiting', 'running')
                """,
                chat_id,
            )
            await conn.execute(
                """
                UPDATE games SET status = 'expired'
                WHERE chat_id = $1 AND status IN ('waiting', 'running')
                """,
                chat_id,
            )
            await conn.execute(
                "DELETE FROM player_stats WHERE chat_id = $1",
                chat_id,
            )
            return list(games)
