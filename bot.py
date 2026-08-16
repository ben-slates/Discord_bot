import json
import os
from pathlib import Path
import re
import asyncio

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
from database import SessionLocal, GuildConfig

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
WORDS_FILE = Path(__file__).resolve().parent / "assets" / "words.json"


def load_forbidden_words() -> set[str]:
    if not WORDS_FILE.exists():
        return set()
    try:
        with WORDS_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, list):
            return {str(item).strip().lower() for item in data if str(item).strip()}
    except Exception as exc:
        print(f"Failed to load forbidden words: {exc}")
    return set()


FORBIDDEN_WORDS = load_forbidden_words()

# Compile a single regex to match forbidden words as whole words (not substrings).
# Uses negative/positive lookarounds to ensure the bad word is not part of a larger word.
try:
    if FORBIDDEN_WORDS:
        # sort by length desc to prefer longest matches in alternation
        words_sorted = sorted(FORBIDDEN_WORDS, key=lambda x: -len(x))
        escaped = [re.escape(w) for w in words_sorted]
        pattern = r"(?<!\w)(?:" + "|".join(escaped) + r")(?!\w)"
        FORBIDDEN_RE = re.compile(pattern, re.IGNORECASE)
    else:
        FORBIDDEN_RE = None
except Exception:
    FORBIDDEN_RE = None


class RynexBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.presences = True
        intents.message_content = True
        intents.guild_messages = True
        intents.dm_messages = False
        super().__init__(command_prefix="!", intents=intents)
        self.tree.on_error = self.on_app_command_error
        # expose the forbidden-word patterns to cogs via the bot instance
        self.FORBIDDEN_RE = FORBIDDEN_RE if 'FORBIDDEN_RE' in globals() else None
        self.FORBIDDEN_WORDS = FORBIDDEN_WORDS if 'FORBIDDEN_WORDS' in globals() else set()

    async def setup_hook(self):
        for filename in os.listdir('./cogs'):
            if filename.endswith('.py') and not filename.startswith('__'):
                try:
                    await self.load_extension(f'cogs.{filename[:-3]}')
                    print(f"Loaded extension: {filename}")
                except Exception as e:
                    print(f"Failed to load extension {filename}: {e}")
        server_id = os.getenv("SERVER_ID")
        if server_id:
            guild = discord.Object(id=int(server_id))
            self.tree.copy_global_to(guild=guild)
            try:
                await self.tree.sync(guild=guild)
                print(f"Synced commands to guild {server_id}")
            except Exception as e:
                print(f"Failed to sync to guild {server_id}: {e}")
                # await self.tree.sync()
        else:
            # await self.tree.sync()
            print("Synced globally (skipped due to dev rate limit)")

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        # Friendly handling for common app-command errors while still logging full tracebacks
        # Map common error class names to user-friendly messages
        friendly_by_name = {
            "TransformerError": "I couldn't resolve one of the command inputs (they may have left the server or the value is invalid).",
            "MissingPermissions": "You don't have the required permissions to run this command.",
            "BotMissingPermissions": "I don't have the required permissions to perform that action.",
            "CheckFailure": "You don't meet the requirements to run this command.",
            "CommandOnCooldown": "This command is on cooldown. Please try again later.",
            "MissingRequiredArgument": "A required command argument is missing. Please check the command usage.",
            "CommandInvokeError": "An error occurred while executing the command.",
        }

        sent_friendly = False
        err_name = type(error).__name__
        msg = friendly_by_name.get(err_name)

        # Special handling for cooldown to include retry info if available
        if err_name == "CommandOnCooldown":
            retry = getattr(error, "retry_after", None)
            if retry is not None:
                msg = f"This command is on cooldown. Try again in {int(retry)} seconds."

        if msg:
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
                sent_friendly = True
            except Exception:
                sent_friendly = False

        # If no friendly message sent above, send a generic fallback
        if not sent_friendly and not (msg and sent_friendly):
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(f" An unexpected error occurred: {error}", ephemeral=True)
                else:
                    await interaction.response.send_message(f" An unexpected error occurred: {error}", ephemeral=True)
            except:
                pass

        # Always post a short friendly entry and the full traceback to the configured log channel for diagnostics
        import traceback
        err_str = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        channel = await self._get_configured_log_channel(interaction.guild_id)
        if channel:
            try:
                # Short friendly embed for quick human scanning
                short_desc = msg or f"An unexpected error occurred: {err_name}"
                short_embed = discord.Embed(
                    title=f"Error: /{interaction.command.name if interaction.command else 'unknown'}",
                    color=discord.Color.orange()
                )
                short_embed.add_field(name="User", value=f"{interaction.user} ({interaction.user.id})", inline=True)
                short_embed.add_field(name="Guild", value=f"{interaction.guild.name if interaction.guild else interaction.guild_id}", inline=True)
                short_embed.add_field(name="Error", value=err_name, inline=True)
                short_embed.add_field(name="Message", value=(short_desc[:600] + "...") if len(short_desc) > 600 else short_desc, inline=False)
                await channel.send(embed=short_embed)

                # Full traceback embed for developers (trim to last 4000 chars)
                tb = err_str
                if len(tb) > 4000:
                    tb = tb[-4000:]
                tb_embed = discord.Embed(title=f"Traceback: /{interaction.command.name if interaction.command else 'unknown'}", description=f"```py\n{tb}\n```", color=discord.Color.red())
                await channel.send(embed=tb_embed)
            except Exception:
                pass

    async def on_error(self, event_method, *args, **kwargs):
        import traceback
        err_str = traceback.format_exc()
        channel = await self._get_configured_log_channel(None)
        if channel:
            try:
                if len(err_str) > 4000:
                    err_str = err_str[-4000:]
                embed = discord.Embed(title=f"Exception in {event_method}", description=f"```py\n{err_str}\n```", color=discord.Color.red())
                await channel.send(embed=embed)
            except:
                pass

    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        await self.process_commands(message)

        content = message.content.lower()
        if not content:
            return

        # Check message content against compiled forbidden-word regex (whole-word match)
        try:
            if FORBIDDEN_RE and FORBIDDEN_RE.search(content):
                try:
                    await message.delete()
                except discord.Forbidden:
                    pass
                except discord.HTTPException:
                    pass

                try:
                    await message.author.send(
                        "Your message was not sent because it contains blocked or harmful language."
                    )
                except Exception:
                    pass
                return
        except Exception:
            # Fall back to token-based matching if regex fails for any reason
            tokens = re.findall(r"\w+", content)
            for token in tokens:
                if token in FORBIDDEN_WORDS:
                    try:
                        await message.delete()
                    except discord.Forbidden:
                        pass
                    except discord.HTTPException:
                        pass

                    try:
                        await message.author.send(
                            "Your message was not sent because it contains blocked or harmful language."
                        )
                    except Exception:
                        pass
                    return

    async def _get_configured_log_channel(self, guild_id):
        db = SessionLocal()
        try:
            config = db.query(GuildConfig).filter_by(guild_id=str(guild_id)).first() if guild_id is not None else None
            if config and config.bot_logs_enabled and config.bot_logs_channel:
                return self.get_channel(int(config.bot_logs_channel))
            return None
        finally:
            db.close()

bot = RynexBot()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

    # One-time startup scan: check recent history (limit 25 messages) per text channel
    # for messages containing forbidden words and delete them if bot has permission.
    if not hasattr(bot, "_history_scan_done") or not bot._history_scan_done:
        bot._history_scan_done = True
        async def _scan():
            for guild in bot.guilds:
                for channel in getattr(guild, "text_channels", []):
                    try:
                        perms = channel.permissions_for(guild.me)
                        if not (perms.view_channel and perms.read_message_history):
                            continue
                        limit = 25
                        async for msg in channel.history(limit=limit):
                            if msg.author.bot or not msg.content:
                                continue
                            cont = msg.content.lower()
                            try:
                                hit = bool(FORBIDDEN_RE.search(cont)) if FORBIDDEN_RE else any(token in FORBIDDEN_WORDS for token in re.findall(r"\w+", cont))
                            except Exception:
                                hit = any(token in FORBIDDEN_WORDS for token in re.findall(r"\w+", cont))
                            if hit:
                                if perms.manage_messages:
                                    try:
                                        await msg.delete()
                                        await asyncio.sleep(0.1)
                                    except discord.Forbidden:
                                        pass
                                    except discord.HTTPException:
                                        pass
                                else:
                                    # Can't delete; skip silently
                                    pass
                    except Exception as e:
                        print(f"History scan failed in {guild.name}/{getattr(channel,'name',channel.id)}: {e}")

        # Schedule scan without blocking on_ready
        asyncio.create_task(_scan())

if __name__ == "__main__":
    if not BOT_TOKEN:
        print("Missing BOT_TOKEN in .env")
    else:
        bot.run(BOT_TOKEN)
