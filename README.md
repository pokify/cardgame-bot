# Card Game Telegram bot

Group mini-game: `/card` opens a 4-player lobby. Highest unique card wins 5 points.

## Commands

- `/card` — caller joins first. Join button edits `n/4`. Starts at 4 players, or after 5 minutes with 2–3. Cancels if fewer than 2.
- `/leaderboard` — Score / Played / Wins. 🥇🥈🥉 on the top 3.
- Hourly auto-lobby in chats listed in `GAME_CHAT_IDS`.

## Card files

Put these in `assets/cards/`:

```
pip2.png pip3.png pip4.png pip5.png pip6.png pip7.png pip8.png pip9.png pip10.png
ace.png jack.png king.png queen.png joker.png
```

Missing files get a generated placeholder so the bot still runs.

Scores: 2–10 face value, Ace 11, Jack 12, King 13, Queen 14, Joker 15. One unique card each, so no ties.

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill BOT_TOKEN and DATABASE_URL
python -m bot.main
```

Create the bot with @BotFather, add `/card` and `/leaderboard`, then add the bot to the group.

Get the group id (forward a group message to @userinfobot, or read bot logs after `/card`). Put it in `GAME_CHAT_IDS` for hourly autos.

## Railway

1. Push this repo to GitHub.
2. New Railway project from that repo.
3. Add a PostgreSQL plugin. Share `DATABASE_URL` with the bot service.
4. Set service variables:

   - `BOT_TOKEN`
   - `DATABASE_URL` (from the plugin)
   - `GAME_CHAT_IDS`  e.g. `-1001234567890`
   - optional: `WIN_POINTS=5`, `LOBBY_SECONDS=300`, `AUTO_INTERVAL_SECONDS=3600`

5. Start command: `python -m bot.main`
6. Keep **one replica**. Long polling cannot share a token across instances.

Railway `postgres://` URLs are accepted; the bot rewrites them to `postgresql://`.

## Notes

- One live lobby or running game per group.
- Stats are per group.
- On deploy/restart, waiting lobbies are re-armed from Postgres.
