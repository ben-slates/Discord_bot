import discord
from discord.ext import commands
import sys
import os

class WelcomeCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        channel = member.guild.get_channel(1519249949630529638)
        if not channel:
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

async def setup(bot):
    await bot.add_cog(WelcomeCog(bot))
