# Rynex Security Discord Bot

Rynex Security is a `discord.py` community bot with leveling, attendance, support tickets backed by Gemini, scheduled news/CVE posts, configurable leaderboards, welcome cards, Hall of Fame announcements, and administrative configuration commands.

The bot preserves Discord responsiveness by keeping synchronous SQLAlchemy work in a bounded DB worker executor. Discord Gateway, Discord REST calls, and RSS HTTP requests remain asynchronous.

## Features

- Message and voice XP, ranks, image rank cards, level-up cards, and leaderboards.

- Daily attendance from messages and qualifying voice joins, reports, exports, and summaries.

- Configurable welcome messages with generated cards.

- Support ticket channels, persistent resolve/escalate buttons, ticket transcript/export, automatic closed-ticket cleanup, and Gemini-assisted replies.

- Scheduled CVE/news posting from The Hacker News and Latest CVEs.

- Per-guild feature enablement, bot-log channels, custom leaderboards, Hall of Fame roles/announcements, and scheduled notification posts.

- Whole-word forbidden-language filtering for ordinary messages, `/message`, and `/question`.

## Architecture

```
Discord Gateway / slash commands  
        │ async  
        ├── cogs: feature and task orchestration  
        ├── aiohttp: reusable RSS HTTP session (News)  
        ├── Google GenAI async client (Support)  
        └── DB executor (six bounded workers)  
                 └── synchronous SQLAlchemy session per operation  
                         └── SQLite or PostgreSQL
```

Each DB worker operation owns its `SessionLocal()` session and closes it before returning. Sessions are never shared across asyncio tasks or threads. Card rendering and RSS parsing are also kept out of the event-loop thread.

## Requirements

- Python 3.11+ (the checked-in environment uses Python 3.13).

- A Discord application/bot and a server where it can be invited.

- PostgreSQL is recommended for production; SQLite is used when `DATABASE\_URL` is empty.

- A Google Gemini API key is required only for AI ticket replies.

- Dependencies listed in [requirements.txt](file:///home/ben/rdp/rynexsecurity/requirements.txt), including `discord`, `SQLAlchemy`, `psycopg2-binary`, `aiohttp`, `feedparser`, `Pillow`, and `google-genai`.

## Installation

```
git clone https://github.com/ben-slates/Discord\_bot.git   
cd Discord\_bot  
python3 -m venv venv  
source venv/bin/activate  
python -m pip install --upgrade pip  
python -m pip install -r requirements.txt  
cp .env.example .env
```

Set the required values in `.env`, then start the bot:

```
python bot.py
```

The application creates existing mapped tables and applies its compatibility column checks at startup. Back up a production database before deploying schema changes.

## Environment variables

| Variable | Required | Format and purpose |
| - | - | - |
| `BOT\_TOKEN` | Yes to run the bot | Discord bot token, e.g. `BOT\_TOKEN=replace\_with\_discord\_token`. |
| `SERVER\_ID` | Recommended | Discord guild ID used for development command sync, e.g. `123456789012345678`. Omit to skip guild sync. |
| `DATABASE\_URL` | No | SQLAlchemy URL. Empty uses `sqlite:///./rynex.db`; PostgreSQL example: `postgresql://user:password@host:5432/database`. |
| `GEMINI\_API\_KEY` | Required for AI replies | Google Gemini API key used by the support cog, e.g. `replace\_with\_google\_key`. Without it, ticket channels still exist but AI replies are unavailable. |
| `SECRET\_KEY` | Only for `auth.py` consumers | JWT signing secret for the FastAPI helper module. |
| `ALGORITHM` | Only for `auth.py` consumers | JWT algorithm, e.g. `HS256`. |
| `ACCESS\_TOKEN\_EXPIRE\_MINUTES` | Only for `auth.py` consumers | Integer JWT lifetime in minutes, e.g. `60`. |


Never commit `.env`, tokens, URLs containing database passwords, or API keys. Do not paste them into Discord, logs, issues, or the README.

## Discord setup and permissions

Invite the application with `bot` and `applications.commands` scopes. Enable the privileged **Server Members**, **Presence**, and **Message Content** intents in the Developer Portal: the bot requests them for member lists, qualifying voice/presence state, and message XP/moderation.

Grant only the permissions needed for enabled features:

- View Channels, Read Message History, Send Messages, Embed Links, Attach Files, and Use Application Commands.

- Manage Messages for deleting filtered content.

- Manage Channels for ticket creation/renaming/deletion.

- Manage Roles for Hall of Fame roles.

- Read/Send permissions in each configured feature channel.

Slash-command decorators set default administrator/manage-messages permissions where noted below. Server owners should still validate channel overwrites and bot-role hierarchy.

## Configuration and commands

Use `/enable` and `/disable` as an administrator to configure per-guild features. The feature command validates whether the selected target is a text channel or category and writes the configuration to `guild\_config`.

| Command | Parameters | Permission / behavior |
| - | - | - |
| `/message` | `channel`, optional `content` | Administrator. Opens a multiline modal; preserves entered whitespace/markdown and sends to the target channel. |
| `/set\_leaderboard` | optional `role` | Administrator. Sets or clears the role eligible for the main leaderboard. |
| `/enable` | `option`, `channel` | Administrator. Enables bot logs, CVE/news, support, attendance, welcome, leaderboard, or level-up announcements. |
| `/disable` | `option` | Administrator. Disables a configured feature. |
| `/enable\_hall\_of\_fame` | `role\_name`, `announcement\_channel`, `warning\_channel` | Administrator. Configures Hall of Fame. |
| `/test` | `option` | Administrator. Sends a safe test message/card to a configured feature channel. |
| `/add\_custom\_leaderboard` | `channel`, `name`, optional `role` | Administrator. Creates/updates a role-filtered leaderboard. |
| `/remove\_custom\_leaderboard` | `channel` | Administrator. Removes that leaderboard. |
| `/hall\_of\_fame` | template, five members, optional custom name | Administrator. Assigns/refreshes Hall of Fame entries and posts an image. |
| `/hall\_of\_fame\_overall` | optional department, optional name | Administrator. Posts department progress. |
| `/rank` | optional `member` | Requires enabled leaderboard and its configured channel. Shows XP, level, rank, and daily XP. |
| `/leaderboard` | none | Requires enabled leaderboard/channel. Shows main or channel-specific leaderboard. |
| `/rankcard` | optional `member` | Requires enabled leaderboard/channel. Generates a rank card. |
| `/stats` | none | Requires enabled attendance and its configured channel. |
| `/export` | optional `month`, `day`, `user` | Administrator; sends attendance CSV. |
| `/today` | none | Shows current-day attendance in the attendance channel. |
| `/user` | optional `member` | Shows attendance history and seven recent days. |
| `/month` | optional `month\_str` (`YYYY-MM`) | Shows monthly attendance summary. |
| `/activity` | none | Administrator; shows in-memory message activity and current voice members. |
| `/ticket` | optional `reason` | Opens one ticket per user in the configured support category; cooldown applies. |
| `/question` | `text` | Posts a moderated question embed; cooldown applies. |
| `/adduser`, `/removeuser` | `member` | Administrator; changes ticket-channel access. |
| `/close` | none | Marks the current ticket closed and limits its owner’s sends. |
| `/reopen` | none | Manage Messages; reopens a closed ticket. |
| `/transcript` | none | Manage Messages; exports up to 500 ticket messages. |
| `/closeall` | none | Administrator; closes all open tickets in the configured category. |


Ticket AI responses include **Mark as Resolved** and **Talk to Human / Unsatisfied** persistent buttons. The latter stops AI replies for that ticket and alerts an administrator-named role when available.

## Background work

- News checks feeds every 30 minutes after the bot is ready.

- Notifications check scheduled summaries each minute: the morning leaderboard/custom boards run at `08:00` and attendance summary at `23:50` (UTC+05:00).

- Voice XP runs every minute; it requires at least two non-bot, non-deafened members in a voice channel.

- Attendance cleanup runs daily at noon (UTC+05:00).

- Closed tickets are checked every 30 minutes and deleted after 12 hours.

- Hall of Fame expiry is checked after Hall of Fame processing.

## News system

News uses The Hacker News and Latest CVEs feeds. When **CVE and News** is enabled, it fetches both feeds concurrently with a reusable `aiohttp.ClientSession`, a 5-second connect timeout, 15-second read timeout, and 20-second total timeout. RSS parsing runs off the event loop.

Recent links are cached locally under `cache/news\_cache.json`, deduplicated against `news\_logs`, and persisted/cleaned on the configured flush interval (six hours). Feed, HTTP, Discord, and database failures are logged; one feed failure does not stop the task’s future iterations.

## Database, poolers, and latency

The synchronous SQLAlchemy engine uses `pool\_pre\_ping`, a 30-minute recycle, and PostgreSQL settings of `pool\_size=10`, `max\_overflow=20`, `pool\_timeout=5`, and a 10-second driver connect timeout. The DB executor has six workers, so the application does not create unlimited concurrent work or share sessions.

Production measurements showed zero executor queue time but high worker checkout/SQL time. DNS verification of the configured `aws-0-ap-southeast-2.pooler.supabase.com` endpoint resolved AWS IPv4 addresses only; its `::ffff:` forms are IPv4-mapped, not native IPv6. This verifies an IPv4 pooler endpoint. It does **not** establish that a direct IPv6 PostgreSQL endpoint is available or would be faster.

High checkout latency can be caused by the provider pooler, distance/routing, TLS/connection setup, or free-tier wake-up/idle behavior. High SQL latency can also reflect remote database load. The bot deliberately awaits these operations from worker threads, so they do not block Discord’s event loop. Do not disable pooling or move synchronous SQLAlchemy back into handlers as a workaround.

For provider investigation, compare warm and idle timings from the same server, inspect provider metrics, and run `EXPLAIN (ANALYZE, BUFFERS)` only on a controlled production session. Keep credentials out of shell history and logs.

## Monitoring and troubleshooting

Useful logs:

- `Logged in as ...`: Gateway login succeeded.

- `Event loop blocked ...`: serious only when recurring; the active stall sampler records the executing frame.

- `Synchronous SQLAlchemy execute on MainThread`: serious; it identifies a direct ORM call from async code that must move to `run\_db`.

- `DB timing ...`: worker total, queue, checkout, SQL statement, flush, and commit timing. A large worker time with zero queue is infrastructure/DB latency, not executor contention.

- `News timing ...`: separates configuration DB, feeds, dedupe DB, sends, and remaining local work. Long feed time is often awaited network time, not loop blocking.

- Discord `Can't keep up`: serious gateway responsiveness issue; inspect the stall sample and direct main-thread SQL log.

Commands:

```
\# Application checks  
source venv/bin/activate  
python -m compileall -q bot.py cogs utils database.py  
python -m unittest discover -s tests -v  
  
\# Service operations  
sudo systemctl status rynexbot  
sudo systemctl restart rynexbot  
sudo journalctl -u rynexbot -f  
  
\# Dependency and DNS diagnostics (no credentials printed)  
python -m pip show discord sqlalchemy google-genai aiohttp  
getent ahosts aws-0-ap-southeast-2.pooler.supabase.com
```

## systemd deployment

The repository includes [rynexbot.service](file:///home/ben/rdp/rynexsecurity/rynexbot.service). It is a template and currently uses `/root/bot`; edit `User`, `WorkingDirectory`, and `ExecStart` to match the actual deployment before installing it.

```
sudo cp rynexbot.service /etc/systemd/system/rynexbot.service  
sudo systemctl daemon-reload  
sudo systemctl enable --now rynexbot
```

Use `stop`, `restart`, `status`, and `journalctl` commands above for operations.

## Project structure

```
bot.py                 Discord client, cog loading, moderation, watchdog  
database.py            SQLAlchemy engine, models, compatibility schema checks  
cogs/                  Admin, attendance, leaderboard, leveling, news, notifications, support, welcome  
utils/db\_executor.py   Bounded executor for synchronous DB work  
utils/diag.py          Event-loop and DB timing diagnostics  
utils/\*card\*.py        Pillow card generation and design helpers  
assets/                Card backgrounds, fonts, logos, forbidden-word JSON  
cache/                 Local news cache (runtime data)  
scripts/               XP management and card testing utilities  
tests/                 Leaderboard helper tests  
rynexbot.service       systemd service template
```

## Development and security rules

- Never call `time.sleep()`, blocking HTTP clients, or synchronous SQLAlchemy from an async Discord handler.

- Use `run\_db`/`run\_db\_profiled` for synchronous DB work; create/close the session inside that worker function.

- Keep Discord and `aiohttp` operations async; do not wrap an entire cog or loop in a thread.

- Use timeouts for external APIs and never log API keys, tokens, passwords, or full database URLs.

- Preserve the bot’s feature configuration and permissions when changing implementation details.

## Known limitations

- Discord API rate limits, RSS providers, Gemini availability, DNS, and PostgreSQL provider/network health are external dependencies.

- Free-tier PostgreSQL or poolers can exhibit cold-start and connection checkout latency.

- The News cache is local to the host; multiple bot instances need shared coordination before being used for high availability.

- Ticket transcripts are intentionally limited to 500 messages, and Discord messages/embeds have platform size limits.

