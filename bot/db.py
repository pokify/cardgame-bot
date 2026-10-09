from __future__ import annotations

import asyncpg
from datetime import datetime, timedelta, timezone

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
                prev_rank       INTEGER,
                prev_score      INTEGER,
                PRIMARY KEY (chat_id, user_id)
            );
            """
        )
        await conn.execute(
            "ALTER TABLE games ADD COLUMN IF NOT EXISTS flavor TEXT"
        )
        await conn.execute(
            "ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS prev_rank INTEGER"
        )
        await conn.execute(
            "ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS prev_score INTEGER"
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS house_challenges (
                chat_id         BIGINT NOT NULL,
                user_id         BIGINT NOT NULL,
                challenge_date  DATE NOT NULL,
                attempts        INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, user_id, challenge_date)
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS house_settings (
                chat_id          BIGINT PRIMARY KEY,
                limit_enabled    BOOLEAN NOT NULL DEFAULT FALSE,
                house_enabled    BOOLEAN NOT NULL DEFAULT TRUE,
                max_plays_per_day INTEGER,
                pvp_before_unlock INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        await conn.execute(
            "ALTER TABLE house_settings ADD COLUMN IF NOT EXISTS house_enabled BOOLEAN NOT NULL DEFAULT TRUE"
        )
        await conn.execute(
            "ALTER TABLE house_settings ADD COLUMN IF NOT EXISTS max_plays_per_day INTEGER"
        )
        await conn.execute(
            "ALTER TABLE house_settings ADD COLUMN IF NOT EXISTS pvp_before_unlock INTEGER NOT NULL DEFAULT 1"
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS game_modes (
                chat_id BIGINT PRIMARY KEY,
                mode TEXT NOT NULL,
                target INTEGER NOT NULL DEFAULT 0,
                days INTEGER NOT NULL DEFAULT 0,
                active BOOLEAN NOT NULL DEFAULT FALSE,
                pending BOOLEAN NOT NULL DEFAULT TRUE,
                started_at TIMESTAMPTZ,
                ends_at TIMESTAMPTZ,
                winner_user_id BIGINT,
                winner_username TEXT,
                winner_score INTEGER
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS game_mode_history (
                chat_id BIGINT NOT NULL,
                mode TEXT NOT NULL,
                user_id BIGINT NOT NULL,
                username TEXT,
                wins INTEGER NOT NULL DEFAULT 0,
                plays INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, mode, user_id)
            )
            """
        )
        # Track participation once per Game Mode tournament, not once per /cards hand.
        await conn.execute(
            "ALTER TABLE game_mode_history ADD COLUMN IF NOT EXISTS last_event_started_at TIMESTAMPTZ"
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deal_memory (
                id          BIGSERIAL PRIMARY KEY,
                chat_id     BIGINT NOT NULL,
                mode        TEXT NOT NULL,
                cards       TEXT[] NOT NULL,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS deal_memory_chat_mode_idx ON deal_memory (chat_id, mode, created_at DESC)"
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pvp_settings (
                chat_id      BIGINT PRIMARY KEY,
                base_2       INTEGER NOT NULL DEFAULT 5,
                bonus_2      INTEGER NOT NULL DEFAULT 1,
                base_3       INTEGER NOT NULL DEFAULT 5,
                bonus_3      INTEGER NOT NULL DEFAULT 2,
                base_4       INTEGER NOT NULL DEFAULT 5,
                bonus_4      INTEGER NOT NULL DEFAULT 3,
                house_risk   INTEGER NOT NULL DEFAULT 0,
                house_reward INTEGER NOT NULL DEFAULT 0,
                joker_points INTEGER NOT NULL DEFAULT -5
            )
            """
        )
        await conn.execute(
            "ALTER TABLE pvp_settings ADD COLUMN IF NOT EXISTS joker_points INTEGER NOT NULL DEFAULT -5"
        )
        await conn.execute(
            "ALTER TABLE player_stats ADD COLUMN IF NOT EXISTS house_points INTEGER NOT NULL DEFAULT 0"
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
        ORDER BY score DESC, wins DESC, played ASC,
                 COALESCE(prev_rank, 2147483647) ASC, user_id ASC
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
            # Snapshot ranks + scores before this game's changes so the next
            # leaderboard can show meaningful ⬆️ / ⬇️ (not tier reshuffles).
            pre_rows = await conn.fetch(
                """
                SELECT user_id, score,
                       ROW_NUMBER() OVER (
                           ORDER BY score DESC, wins DESC, played ASC, user_id ASC
                       ) AS rank
                FROM player_stats
                WHERE chat_id = $1
                """,
                chat_id,
            )
            old_ranks = {r["user_id"]: int(r["rank"]) for r in pre_rows}
            old_scores = {r["user_id"]: int(r["score"]) for r in pre_rows}

            for p in players:
                uid = p["user_id"]
                is_win = uid == winner_user_id
                is_joker = joker_user_id is not None and uid == joker_user_id
                delta = 0
                if is_win:
                    delta += points
                if is_joker:
                    delta += joker_penalty
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

            # Store pre-game rank + score for every player on the board.
            # New players have no history → NULL → shown as "—" until next game.
            all_stats = await conn.fetch(
                "SELECT user_id FROM player_stats WHERE chat_id = $1",
                chat_id,
            )
            for r in all_stats:
                uid = r["user_id"]
                await conn.execute(
                    """
                    UPDATE player_stats
                    SET prev_rank = $3, prev_score = $4
                    WHERE chat_id = $1 AND user_id = $2
                    """,
                    chat_id,
                    uid,
                    old_ranks.get(uid),
                    old_scores.get(uid),
                )


async def leaderboard(chat_id: int, limit: int = 15) -> list[asyncpg.Record]:
    return await pool().fetch(
        """
        SELECT ps.*
        FROM player_stats ps
        LEFT JOIN game_modes gm
          ON gm.chat_id=ps.chat_id AND gm.active=TRUE
        WHERE ps.chat_id=$1
        ORDER BY
          CASE WHEN gm.winner_user_id IS NOT NULL
                    AND ps.user_id=gm.winner_user_id THEN 0 ELSE 1 END,
          ps.score DESC, ps.wins DESC, ps.played ASC,
          COALESCE(ps.prev_rank,2147483647) ASC, ps.user_id ASC
        LIMIT $2
        """,chat_id,limit)


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
            # /clearlb also resets House daily challenge usage.
            await conn.execute(
                "DELETE FROM house_challenges WHERE chat_id = $1",
                chat_id,
            )
            return list(games)


async def house_challenges_used(chat_id: int, user_id: int) -> int:
    row = await pool().fetchrow(
        """
        SELECT attempts
        FROM house_challenges
        WHERE chat_id = $1
          AND user_id = $2
          AND challenge_date = CURRENT_DATE
        """,
        chat_id,
        user_id,
    )
    return int(row["attempts"]) if row else 0


async def consume_house_challenge(chat_id: int, user_id: int, max_plays: int) -> tuple[bool, int]:
    """Atomically consume one of today's House challenges. max_plays must be >= 1."""
    row = await pool().fetchrow(
        """
        INSERT INTO house_challenges
            (chat_id, user_id, challenge_date, attempts)
        VALUES ($1, $2, CURRENT_DATE, 1)
        ON CONFLICT (chat_id, user_id, challenge_date)
        DO UPDATE SET attempts = house_challenges.attempts + 1
        WHERE house_challenges.attempts < $3
        RETURNING attempts
        """,
        chat_id,
        user_id,
        max_plays,
    )
    if row is None:
        return False, 0
    attempts = int(row["attempts"])
    return True, max(0, max_plays - attempts)


async def get_house_settings(chat_id: int) -> dict:
    row = await pool().fetchrow(
        "SELECT house_enabled, max_plays_per_day, pvp_before_unlock "
        "FROM house_settings WHERE chat_id = $1",
        chat_id,
    )
    if row is None:
        return {
            "house_enabled": True,
            "max_plays_per_day": None,
            "pvp_before_unlock": 1,
        }
    return {
        "house_enabled": bool(row["house_enabled"]),
        "max_plays_per_day": row["max_plays_per_day"],
        "pvp_before_unlock": max(0, int(row["pvp_before_unlock"] or 0)),
    }


async def save_house_settings(
    chat_id: int,
    house_enabled: bool,
    max_plays_per_day: int | None,
    pvp_before_unlock: int = 1,
) -> None:
    pvp_before_unlock = max(0, min(20, int(pvp_before_unlock)))
    await pool().execute(
        """
        INSERT INTO house_settings
            (chat_id, house_enabled, max_plays_per_day, limit_enabled, pvp_before_unlock)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (chat_id) DO UPDATE SET
            house_enabled = EXCLUDED.house_enabled,
            max_plays_per_day = EXCLUDED.max_plays_per_day,
            limit_enabled = EXCLUDED.limit_enabled,
            pvp_before_unlock = EXCLUDED.pvp_before_unlock
        """,
        chat_id,
        house_enabled,
        max_plays_per_day,
        max_plays_per_day is not None,
        pvp_before_unlock,
    )


async def pvp_games_played(chat_id: int, user_id: int) -> int:
    row = await pool().fetchrow(
        "SELECT played FROM player_stats WHERE chat_id = $1 AND user_id = $2",
        chat_id,
        user_id,
    )
    return int(row["played"]) if row else 0


async def house_enabled(chat_id: int) -> bool:
    settings = await get_house_settings(chat_id)
    return bool(settings["house_enabled"])



async def get_game_mode(chat_id: int) -> dict | None:
    row = await pool().fetchrow("SELECT * FROM game_modes WHERE chat_id=$1", chat_id)
    return dict(row) if row else None


async def save_game_mode_pending(chat_id: int, mode: str, target: int = 0, days: int = 0) -> None:
    mode = "first" if mode == "first" else "highest"
    await pool().execute(
        """
        INSERT INTO game_modes
            (chat_id,mode,target,days,active,pending)
        VALUES ($1,$2,$3,$4,FALSE,TRUE)
        ON CONFLICT (chat_id) DO UPDATE SET
            mode=EXCLUDED.mode,target=EXCLUDED.target,days=EXCLUDED.days,
            active=FALSE,pending=TRUE,started_at=NULL,ends_at=NULL,
            winner_user_id=NULL,winner_username=NULL,winner_score=NULL
        """,
        chat_id, mode, max(0,int(target)), max(0,int(days)),
    )


async def activate_game_mode(chat_id: int) -> dict | None:
    row = await pool().fetchrow("SELECT * FROM game_modes WHERE chat_id=$1", chat_id)
    if row is None:
        return None
    now = datetime.now(timezone.utc)
    ends_at = now + timedelta(days=max(0,int(row["days"] or 0))) if row["mode"]=="highest" else None
    row = await pool().fetchrow(
        """
        UPDATE game_modes
        SET active=TRUE,pending=FALSE,started_at=$2,ends_at=$3,
            winner_user_id=NULL,winner_username=NULL,winner_score=NULL
        WHERE chat_id=$1 RETURNING *
        """, chat_id, now, ends_at
    )
    return dict(row) if row else None


async def reset_game_mode_for_restart(chat_id: int) -> dict | None:
    row=await pool().fetchrow(
        """
        UPDATE game_modes SET active=FALSE,pending=TRUE,started_at=NULL,ends_at=NULL,
        winner_user_id=NULL,winner_username=NULL,winner_score=NULL
        WHERE chat_id=$1 RETURNING *
        """,chat_id)
    return dict(row) if row else None


async def end_game_mode(chat_id: int) -> dict | None:
    row=await pool().fetchrow(
        """
        UPDATE game_modes SET active=FALSE,pending=FALSE,started_at=NULL,ends_at=NULL,
        winner_user_id=NULL,winner_username=NULL,winner_score=NULL
        WHERE chat_id=$1 RETURNING *
        """,chat_id)
    return dict(row) if row else None


async def clear_game_mode_history(chat_id: int, mode: str) -> None:
    await pool().execute("DELETE FROM game_mode_history WHERE chat_id=$1 AND mode=$2",chat_id,mode)


async def game_mode_history_exists(chat_id: int, mode: str) -> bool:
    return await pool().fetchrow(
        "SELECT 1 FROM game_mode_history WHERE chat_id=$1 AND mode=$2 AND wins>0 LIMIT 1",
        chat_id,mode
    ) is not None


async def game_mode_history(chat_id: int, mode: str) -> list[asyncpg.Record]:
    return await pool().fetch(
        """
        SELECT * FROM game_mode_history
        WHERE chat_id=$1 AND mode=$2 AND wins>0
        ORDER BY wins DESC,plays DESC,COALESCE(username,''),user_id
        """,chat_id,mode
    )


async def game_mode_participation(chat_id: int, mode: str, players: list[dict]) -> None:
    """Count each player once per Game Mode event, regardless of hands played."""
    if not players:
        return
    async with pool().acquire() as conn:
        event = await conn.fetchval(
            "SELECT started_at FROM game_modes WHERE chat_id=$1 AND mode=$2 AND active=TRUE",
            chat_id, mode,
        )
        if event is None:
            return
        async with conn.transaction():
            for player in players:
                await conn.execute(
                    """
                    INSERT INTO game_mode_history
                        (chat_id,mode,user_id,username,wins,plays,last_event_started_at)
                    VALUES($1,$2,$3,$4,0,1,$5)
                    ON CONFLICT(chat_id,mode,user_id) DO UPDATE SET
                        username=EXCLUDED.username,
                        plays=game_mode_history.plays +
                            CASE WHEN game_mode_history.last_event_started_at
                                      IS DISTINCT FROM EXCLUDED.last_event_started_at
                                 THEN 1 ELSE 0 END,
                        last_event_started_at=EXCLUDED.last_event_started_at
                    """,
                    chat_id, mode, int(player["user_id"]), player.get("username"), event,
                )


async def lock_game_mode_winner(chat_id: int,user_id: int,username: str|None,score: int) -> dict|None:
    row=await pool().fetchrow(
        """
        UPDATE game_modes SET winner_user_id=$2,winner_username=$3,winner_score=$4
        WHERE chat_id=$1 AND active=TRUE AND winner_user_id IS NULL RETURNING *
        """,chat_id,user_id,username,score)
    if row is None:
        return None
    await pool().execute(
        """
        INSERT INTO game_mode_history(chat_id,mode,user_id,username,wins,plays)
        VALUES($1,$2,$3,$4,1,0)
        ON CONFLICT(chat_id,mode,user_id) DO UPDATE SET
            username=EXCLUDED.username,wins=game_mode_history.wins+1
        """,chat_id,row["mode"],user_id,username)
    return dict(row)


async def check_first_to_winner(chat_id:int) -> dict|None:
    mode=await get_game_mode(chat_id)
    if not mode or not mode["active"] or mode["mode"]!="first" or mode["winner_user_id"] is not None:
        return None
    target=int(mode["target"] or 0)
    if target<=0:
        return None
    row=await pool().fetchrow(
        """
        SELECT user_id,username,score FROM player_stats
        WHERE chat_id=$1 AND score >= $2
        ORDER BY score DESC,wins DESC,played ASC,user_id ASC LIMIT 1
        """,chat_id,target)
    if row is None:
        return None
    return await lock_game_mode_winner(chat_id,int(row["user_id"]),row["username"],int(row["score"]))


async def highest_score_winner(chat_id:int) -> dict|None:
    mode=await get_game_mode(chat_id)
    if not mode or not mode["active"] or mode["mode"]!="highest" or mode["winner_user_id"] is not None:
        return mode if mode and mode["winner_user_id"] else None
    row=await pool().fetchrow(
        """
        SELECT user_id,username,score FROM player_stats
        WHERE chat_id=$1
        ORDER BY score DESC,wins DESC,played ASC,user_id ASC LIMIT 1
        """,chat_id)
    if row is None:
        return mode
    return await lock_game_mode_winner(chat_id,int(row["user_id"]),row["username"],int(row["score"]))

async def record_deal_memory(chat_id: int, mode: str, cards: list[str]) -> None:
    async with pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO deal_memory (chat_id, mode, cards)
                VALUES ($1, $2, $3)
                """,
                chat_id,
                mode,
                cards,
            )
            old = await conn.fetch(
                """
                SELECT id FROM deal_memory
                WHERE chat_id = $1 AND mode = $2
                ORDER BY created_at DESC, id DESC
                OFFSET 2
                """,
                chat_id,
                mode,
            )
            if old:
                await conn.execute(
                    "DELETE FROM deal_memory WHERE id = ANY($1::bigint[])",
                    [r["id"] for r in old],
                )


async def banned_cards(chat_id: int, mode: str) -> list[str]:
    rows = await pool().fetch(
        """
        SELECT cards FROM deal_memory
        WHERE chat_id = $1 AND mode = $2
        ORDER BY created_at DESC, id DESC
        LIMIT 2
        """,
        chat_id,
        mode,
    )
    out: list[str] = []
    for row in rows:
        out.extend(list(row["cards"] or []))
    return out


def _default_pvp_settings() -> dict:
    return {
        "base_2": 5,
        "bonus_2": 1,
        "base_3": 5,
        "bonus_3": 2,
        "base_4": 5,
        "bonus_4": 3,
        "house_risk": 0,
        "house_reward": 0,
        "joker_points": -5,
    }


def pvp_points_for(settings: dict, n_players: int) -> int:
    if n_players == 2:
        base, bonus = settings["base_2"], settings["bonus_2"]
    elif n_players == 3:
        base, bonus = settings["base_3"], settings["bonus_3"]
    else:
        base, bonus = settings["base_4"], settings["bonus_4"]
    if base <= 0:
        return 0
    return base + max(0, bonus)


def house_rr_active(settings: dict) -> bool:
    return int(settings.get("house_risk") or 0) != 0 or int(settings.get("house_reward") or 0) != 0


async def get_pvp_settings(chat_id: int) -> dict:
    row = await pool().fetchrow(
        "SELECT * FROM pvp_settings WHERE chat_id = $1",
        chat_id,
    )
    if row is None:
        return _default_pvp_settings()
    data = _default_pvp_settings()
    for key in data:
        if row[key] is not None:
            data[key] = int(row[key])
    if data["base_2"] <= 0:
        data["bonus_2"] = 0
    if data["base_3"] <= 0:
        data["bonus_3"] = 0
    if data["base_4"] <= 0:
        data["bonus_4"] = 0
    return data


async def save_pvp_settings(chat_id: int, settings: dict) -> None:
    data = {**_default_pvp_settings(), **settings}
    if data["base_2"] <= 0:
        data["base_2"] = 0
        data["bonus_2"] = 0
    if data["base_3"] <= 0:
        data["base_3"] = 0
        data["bonus_3"] = 0
    if data["base_4"] <= 0:
        data["base_4"] = 0
        data["bonus_4"] = 0
    await pool().execute(
        """
        INSERT INTO pvp_settings (
            chat_id, base_2, bonus_2, base_3, bonus_3, base_4, bonus_4,
            house_risk, house_reward, joker_points
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        ON CONFLICT (chat_id) DO UPDATE SET
            base_2 = EXCLUDED.base_2,
            bonus_2 = EXCLUDED.bonus_2,
            base_3 = EXCLUDED.base_3,
            bonus_3 = EXCLUDED.bonus_3,
            base_4 = EXCLUDED.base_4,
            bonus_4 = EXCLUDED.bonus_4,
            house_risk = EXCLUDED.house_risk,
            house_reward = EXCLUDED.house_reward,
            joker_points = EXCLUDED.joker_points
        """,
        chat_id,
        data["base_2"],
        data["bonus_2"],
        data["base_3"],
        data["bonus_3"],
        data["base_4"],
        data["bonus_4"],
        data["house_risk"],
        data["house_reward"],
        data["joker_points"],
    )


async def apply_house_result(
    chat_id: int,
    player: dict,
    delta: int,
    won: bool,
) -> None:
    """Record one House game in the combined leaderboard.

    House games increment Played and Wins (on a win), and the configured
    House result changes both overall Score and the H (House) total.
    """
    async with pool().acquire() as conn:
        async with conn.transaction():
            pre_rows = await conn.fetch(
                """
                SELECT user_id, score,
                       ROW_NUMBER() OVER (
                           ORDER BY score DESC, wins DESC, played ASC, user_id ASC
                       ) AS rank
                FROM player_stats
                WHERE chat_id = $1
                """,
                chat_id,
            )
            old_ranks = {r["user_id"]: int(r["rank"]) for r in pre_rows}
            old_scores = {r["user_id"]: int(r["score"]) for r in pre_rows}

            await conn.execute(
                """
                INSERT INTO player_stats
                    (chat_id, user_id, username, first_name,
                     score, played, wins, house_points)
                VALUES ($1, $2, $3, $4, $5, 1, $6, $5)
                ON CONFLICT (chat_id, user_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    score = player_stats.score + EXCLUDED.score,
                    played = player_stats.played + 1,
                    wins = player_stats.wins + EXCLUDED.wins,
                    house_points = player_stats.house_points + EXCLUDED.house_points
                """,
                chat_id,
                player["user_id"],
                player["username"],
                player["first_name"],
                delta,
                1 if won else 0,
            )

            all_stats = await conn.fetch(
                "SELECT user_id FROM player_stats WHERE chat_id = $1",
                chat_id,
            )
            for row in all_stats:
                uid = row["user_id"]
                await conn.execute(
                    """
                    UPDATE player_stats
                    SET prev_rank = $3, prev_score = $4
                    WHERE chat_id = $1 AND user_id = $2
                    """,
                    chat_id,
                    uid,
                    old_ranks.get(uid),
                    old_scores.get(uid),
                )


async def apply_pvp_score_delta(chat_id: int, player: dict, delta: int) -> None:
    """Change PvP Score only. Does not change Played/Wins."""
    if delta == 0:
        return
    async with pool().acquire() as conn:
        async with conn.transaction():
            pre_rows = await conn.fetch(
                """
                SELECT user_id, score,
                       ROW_NUMBER() OVER (
                           ORDER BY score DESC, wins DESC, played ASC, user_id ASC
                       ) AS rank
                FROM player_stats
                WHERE chat_id = $1
                """,
                chat_id,
            )
            old_ranks = {r["user_id"]: int(r["rank"]) for r in pre_rows}
            old_scores = {r["user_id"]: int(r["score"]) for r in pre_rows}
            await conn.execute(
                """
                INSERT INTO player_stats
                    (chat_id, user_id, username, first_name, score, played, wins, house_points)
                VALUES ($1, $2, $3, $4, $5, 0, 0, $5)
                ON CONFLICT (chat_id, user_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    score = player_stats.score + EXCLUDED.score,
                    house_points = player_stats.house_points + EXCLUDED.house_points
                """,
                chat_id,
                player["user_id"],
                player["username"],
                player["first_name"],
                delta,
            )
            all_stats = await conn.fetch(
                "SELECT user_id FROM player_stats WHERE chat_id = $1",
                chat_id,
            )
            for r in all_stats:
                uid = r["user_id"]
                await conn.execute(
                    """
                    UPDATE player_stats
                    SET prev_rank = $3, prev_score = $4
                    WHERE chat_id = $1 AND user_id = $2
                    """,
                    chat_id,
                    uid,
                    old_ranks.get(uid),
                    old_scores.get(uid),
                )
