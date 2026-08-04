import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
from dotenv import load_dotenv
from database import SessionLocal, GuildConfig, flush_pending_changes_to_db

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

class RynexBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.all()
        super().__init__(command_prefix="!", intents=intents)
        self.tree.on_error = self.on_app_command_error

    def cog_unload(self):
        self.buffer_flush_loop.cancel()

    @tasks.loop(hours=6)
    async def buffer_flush_loop(self):
        try:
            flush_pending_changes_to_db()
        except Exception as exc:
            print(f"Buffered DB flush error: {exc}")

    @buffer_flush_loop.before_loop
    async def before_buffer_flush_loop(self):
        await self.wait_until_ready()

    async def setup_hook(self):
        self.buffer_flush_loop.start()

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
        # Notify the user running the command
        try:
            if interaction.response.is_done():
                await interaction.followup.send(f" An unexpected error occurred: {error}", ephemeral=True)
            else:
                await interaction.response.send_message(f" An unexpected error occurred: {error}", ephemeral=True)
        except:
            pass
            
        import traceback
        err_str = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        channel = await self._get_configured_log_channel(interaction.guild_id)
        if channel:
            try:
                if len(err_str) > 4000:
                    err_str = err_str[-4000:]
                embed = discord.Embed(title=f"Command Error in /{interaction.command.name if interaction.command else 'unknown'}", description=f"```py\n{err_str}\n```", color=discord.Color.red())
                await channel.send(embed=embed)
            except:
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

if __name__ == "__main__":
    if not BOT_TOKEN:
        print("Missing BOT_TOKEN in .env")
    else:
        bot.run(BOT_TOKEN)
