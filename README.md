# Rynex Security Discord Bot

Rynex Security is a Discord community bot for leveling, attendance, welcome messages, support tickets, custom leaderboards, news, and profile cards. It is designed to be easy to set up even if you are new to Python or Discord bot hosting.

## What this bot can do

- XP and leveling for members
- Rank and rank-card commands
- Welcome cards for new members
- Attendance tracking and attendance reports
- Support ticket creation and AI-assisted replies
- Custom leaderboards and daily summaries
- News and CVE posting
- Level-up announcement cards

## Quick start for beginners

### 1. Requirements

You need:

- Python 3.11 or newer
- A Discord bot token
- A Discord server ID
- Optional: a PostgreSQL database, though SQLite works by default
- Optional: a Gemini API key if you want the support AI features

### 2. Create a local Python environment

```bash
python -m venv venv
source venv/bin/activate
```

On Windows, use:

```bash
venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Create a .env file

Create a file named .env in the project root with this structure:

```env
BOT_TOKEN=your_discord_bot_token
SERVER_ID=your_discord_server_id
DATABASE_URL=sqlite:///./rynex.db
GEMINI_API_KEY=your_google_gemini_api_key
```

Important notes:

- The bot will use SQLite automatically if DATABASE_URL is not set.
- PostgreSQL URLs are supported, but old postgres:// URLs are converted automatically.
- Keep your .env file private. Do not commit it to Git.

### 5. Invite the bot to your Discord server

In Discord Developer Portal:

1. Create or open your bot application.
2. Go to OAuth2 → URL Generator.
3. Select bot and application.commands.
4. Give the bot the permissions you want, such as:
   - Read Messages/View Channels
   - Send Messages
   - Embed Links
   - Attach Files
   - Manage Roles (if you use Hall of Fame or role-based features)
5. Use the generated invite link to add the bot to your server.

### 6. Start the bot

```bash
python bot.py
```

On startup, the bot:

- loads all cogs from the cogs folder
- creates or updates the database tables if needed
- syncs slash commands to your server

## How the database and caching system works

This bot uses a buffered-write system so it does not hit the database constantly for every small update.

### Normal behavior

- Commands such as /rank, /rankcard, /leaderboard, /stats, and /export read from the live database directly.
- This means those commands always use the latest stored data.

### Buffered writes

- Most write operations are not sent to the database immediately.
- Instead, they are stored in a local cache file under the cache folder.
- Every 6 hours, the bot flushes those buffered writes into the real database.

### Why this exists

This reduces heavy live database traffic and helps keep the bot stable when many activity events happen in a short time.

### Files involved

- [database.py](database.py) contains the database setup, models, and buffered-write logic
- [bot.py](bot.py) starts the background flush loop
- [cogs/news.py](cogs/news.py) uses the cache flow for news entries
- [utils/batch_cache.py](utils/batch_cache.py) provides the shared cache helpers

If you are debugging locally, you may see files appear in the cache folder after bot activity. Those files are only local cache data and are not the main database.

## Features and how to use them

### Leveling and XP

Members gain XP from activity and can view their rank with:

- /rank
- /rankcard
- /leaderboard

You can also configure leveling channels and level-up announcements from the bot’s admin commands.

### Attendance

Attendance is tracked automatically based on activity and can be viewed with:

- /stats
- /today
- /month
- /export

### Welcome messages

New members can receive welcome cards and messages when configured.

### Support tickets

Admins can enable support features and use the support commands to open and manage tickets.

### News and CVE updates

The news cog posts updates from configured feeds. It uses the buffered cache system so it does not write every entry to the database instantly.

### Hall of Fame and custom leaderboards

Admins can configure custom leaderboards and Hall of Fame announcements through the slash commands.

## Admin setup tips

Some channels and settings are stored per server in the database.

Common setup tasks:

- Enable features with the admin slash commands
- Choose the correct channel for leveling, attendance, logs, or news
- Make sure the bot has permission to send messages in those channels
- Check the bot role permissions if you use role-based features

## Card system

The bot can generate image cards for rank, welcome, and level-up events.

The card rendering logic lives in [utils/card_design.py](utils/card_design.py) and the image assets are stored in the assets folder.

Card types:

- Rank card
- Welcome card
- Level-up card

## Running as a background service

On Linux, you can keep the bot alive with systemd.

1. Edit [rynexbot.service](rynexbot.service) and update the user, working directory, and Python path.
2. Copy it to systemd:

```bash
sudo cp rynexbot.service /etc/systemd/system/rynexbot.service
sudo systemctl daemon-reload
sudo systemctl enable --now rynexbot
```

Useful commands:

```bash
sudo systemctl status rynexbot
sudo journalctl -u rynexbot -f
sudo systemctl restart rynexbot
```

## Project layout

```text
assets/         Card background images and fonts
cogs/           Bot features and slash commands
scripts/        Small helper scripts
utils/          Card rendering and helper modules
database.py     Database models and buffered-write logic
bot.py          Bot startup and extension loading
```

## Troubleshooting

If the bot does not start:

- Check that your .env file exists and has the correct values
- Make sure your bot token is valid
- Check that the bot has permission in your Discord server
- Make sure Python dependencies are installed
- Look at the terminal output for the exact error

If commands do not work:

- Confirm the bot is online and connected to the server
- Confirm the relevant feature is enabled
- Make sure the bot can send messages in the target channel

## Development check

You can compile the project to catch syntax issues:

```bash
python -m py_compile bot.py database.py cogs/*.py utils/*.py scripts/*.py
```