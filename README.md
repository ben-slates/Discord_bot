# Rynex Security Discord Bot

A Discord community bot with XP/leveling, welcome messages, support tickets, attendance, notifications, custom leaderboards, and premium rendered profile cards.

## Requirements

- Python 3.11 or newer
- A Discord application and bot token
- Discord server ID
- SQLite (default) or a PostgreSQL database

Install the project and rendering/runtime dependencies:

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt discord.py Pillow python-dotenv
```

On Windows, activate the environment with `venv\Scripts\activate`.

## Configuration

Create a local `.env` file in the project root:

```env
BOT_TOKEN=your_discord_bot_token
SERVER_ID=your_discord_server_id
DATABASE_URL=sqlite:///./rynex.db
GEMINI_API_KEY=your_google_gemini_api_key
```

`DATABASE_URL` is optional. When it is omitted, the bot uses `sqlite:///./rynex.db`. PostgreSQL URLs are supported; legacy `postgres://` URLs are normalized automatically.

Never commit `.env` or bot tokens.

Copy `.env.example` when setting up a new server, then fill in the values locally. `GEMINI_API_KEY` is required for the AI support cog and `test_models.py`.

## Discord channel and role setup

### Copy a channel ID

In Discord, enable **User Settings → Advanced → Developer Mode**. Right-click the required channel and select **Copy Channel ID**. Replace only the numeric ID in the relevant source file, then restart the bot/service.

The following IDs are currently hard-coded and should be changed when deploying this bot to another server:

| Feature | File | What to change |
| --- | --- | --- |
| Welcome-card destination | `cogs/welcome.py` | `member.guild.get_channel(1519249949630529638)` |
| Rules link in welcome message | `cogs/welcome.py` | `<#1519028752145842339>` |
| Introductions link in welcome message | `cogs/welcome.py` | `<#1519251723850612766>` |
| Rank, Rank Card, and default Level Up channel | `cogs/leveling.py` | `DEFAULT_LEVELING_CHANNEL_ID` and the command channel checks |
| Attendance command channel | `cogs/attendance.py` | `1529889568172544170` in the command checks |
| Automated news destination | `cogs/news.py` | `NEWS_CHANNEL_ID` |

Keep the Rank command checks and `DEFAULT_LEVELING_CHANNEL_ID` set to the same channel if you want `/rank`, `/rankcard`, `/leaderboard`, and level-up notifications to use one leveling channel.

### Private internee leaderboard

`/leaderboard` has two modes:

1. In the normal leveling channel, it shows the top ten members by XP.
2. In a channel registered with `/add_leaderboard`, it shows a custom top three using **XP + attendance**.

For a private internee-only leaderboard:

1. Create a text channel, for example `#intern-leaderboard`.
2. In **Edit Channel → Permissions**, deny **View Channel** for `@everyone`.
3. Allow **View Channel** for the `Internee` role, staff roles, and the bot role. The bot also needs **Send Messages**, **Read Message History**, and **Use Application Commands**.
4. Ensure qualifying users have a role whose name contains `internee` (case-insensitive), for example `Internee`, `Cybersecurity Internee`, or `INTERNEE`.
5. As a Discord administrator, run:

   ```text
   /add_leaderboard channel:#intern-leaderboard name:Intern Leaderboard
   ```

6. Run `/leaderboard` inside `#intern-leaderboard`.

The custom leaderboard reads members from that selected channel and includes only non-bot members whose role name contains `internee`. To use a different qualifying role, update the role check in `cogs/leveling.py` (and the matching daily-summary check in `cogs/notifications.py`) from `"internee"` to your role-name keyword, then restart the bot.

### Database-backed channel settings

Some destinations are stored per guild in `GuildConfig` instead of being fixed in a source file: `leveling_channel`, `attendance_channel`, and `notification_channel`. The Level Up card uses `leveling_channel` when it is configured, otherwise it falls back to `DEFAULT_LEVELING_CHANNEL_ID` in `cogs/leveling.py`.

## Run the bot

```bash
source venv/bin/activate
python bot.py
```

On startup, the bot loads every Python cog in `cogs/` and syncs application commands to `SERVER_ID` when configured.

## Run as a background service (systemd)

Linux servers using systemd can keep the bot running in the background with the included [rynexbot.service](rynexbot.service) unit. The unit restarts the bot automatically after a crash or reboot.

Before installing it, edit `rynexbot.service` and set these values to match your server:

- `User=` — the Linux account that owns and runs the bot. Avoid running as `root` when a dedicated service account is available.
- `WorkingDirectory=` — the absolute path to this repository.
- `ExecStart=` — the absolute path to the virtual environment Python executable followed by `bot.py`.

For example, if the project is installed at `/home/discord/rynexsecurity` and is owned by the `discord` user:

```ini
[Service]
User=discord
WorkingDirectory=/home/discord/rynexsecurity
ExecStart=/home/discord/rynexsecurity/venv/bin/python bot.py
Restart=always
RestartSec=5
```

Install and start the service:

```bash
sudo cp rynexbot.service /etc/systemd/system/rynexbot.service
sudo systemctl daemon-reload
sudo systemctl enable --now rynexbot
```

Useful service commands:

```bash
# Check whether the bot is running
sudo systemctl status rynexbot

# Follow live bot logs
sudo journalctl -u rynexbot -f

# Restart after code or configuration changes
sudo systemctl restart rynexbot

# Stop it and prevent startup after reboot
sudo systemctl disable --now rynexbot
```

After changing `rynexbot.service`, run `sudo systemctl daemon-reload` before restarting it. Keep the `.env` file in the configured `WorkingDirectory` so `bot.py` can load it.

## Cards

The three card types share the rendering engine in `utils/card_design.py`:

- Rank Card — rank, level, total XP, XP progress, and daily XP
- Welcome Card — guild welcome information and member count
- Level Up Card — achievement-style previous/new level notification

Backgrounds are chosen randomly each time a card is generated. Add images directly to, or inside subfolders of, the corresponding asset folder; all supported images are included automatically and the same image is not selected twice consecutively when alternatives exist.

| Card | Asset folder | Render size | Aspect ratio | Recommended background size |
| --- | --- | ---: | ---: | ---: |
| Rank | `assets/rankcard_bg/` | 1180 × 500 px | 2.36:1 | 2360 × 1000 px or larger |
| Welcome | `assets/welcome_bg/` | 1000 × 400 px | 2.50:1 | 2000 × 800 px or larger |
| Level Up | `assets/Levelupcard_bg/` | 1000 × 320 px | 3.125:1 | 2000 × 640 px or larger |

Supported formats are WebP, PNG, JPG/JPEG, BMP, and GIF. Images are center-cropped to the target ratio, so backgrounds that match the listed ratio preserve the intended composition best.

## Send sample cards

The standalone sender renders all three current card designs for one random non-bot member, sends them to their configured feature channels, and exits:

```bash
source venv/bin/activate
python scripts/send_test_cards.py
```

It uses `BOT_TOKEN` and `SERVER_ID` from `.env`. Welcome cards go to the welcome channel defined in the script; Rank and Level Up cards use the guild’s configured leveling channel, with the project fallback when no channel is configured.

## Project layout

```text
assets/     Card wallpapers and fonts
cogs/       Discord features and listeners
scripts/    One-off developer utilities
utils/      Card rendering and XP helpers
database.py SQLAlchemy configuration and models
bot.py      Bot startup and extension loading
```

## Development checks

Compile the project after changes:

```bash
python -m py_compile bot.py database.py cogs/*.py utils/*.py scripts/*.py
```

`test_models.py` can also be run when working on the configured AI integration:

```bash
python test_models.py
```
