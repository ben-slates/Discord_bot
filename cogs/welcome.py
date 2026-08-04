import discord
from discord.ext import commands
import sys
import os

from database import SessionLocal, GuildConfig


class WelcomeCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        db = SessionLocal()
        try:
            config = db.query(GuildConfig).filter_by(guild_id=str(member.guild.id)).first()
            if not config or not config.welcome_enabled or not config.welcome_channel:
                return

            channel = member.guild.get_channel(int(config.welcome_channel))
            if not channel or not isinstance(channel, discord.TextChannel):
                return

            sys.path.append(os.path.join(os.path.dirname(__file__), "..", "utils"))
            from welcomecard import generate_welcome_card

            try:
                file = await generate_welcome_card(member)
                msg_text = (
                    f"Welcome to **Rynex Security**, **{member.mention}**!\n\n"
                    "Read <#1519028752145842339>, introduce yourself in <#1519251723850612766>, and join the discussion.\n"
                    "Welcome to the community."
                )
                await channel.send(msg_text, file=file)
            except Exception as e:
                print(f"Failed to send welcome card: {e}")
        finally:
            db.close()

async def setup(bot):
    await bot.add_cog(WelcomeCog(bot))
