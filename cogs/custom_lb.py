import discord
from discord.ext import commands
from discord import app_commands
from database import SessionLocal, CustomLeaderboard

class CustomLBCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="add_leaderboard", description="Add a custom daily leaderboard to a channel")
    @app_commands.default_permissions(administrator=True)
    async def add_leaderboard(self, interaction: discord.Interaction, channel: discord.TextChannel, name: str):
        db = SessionLocal()
        try:
            lb = db.query(CustomLeaderboard).filter_by(channel_id=str(channel.id)).first()
            if not lb:
                lb = CustomLeaderboard(channel_id=str(channel.id), guild_id=str(interaction.guild_id), name=name)
                db.add(lb)
            else:
                lb.name = name
            db.commit()
            await interaction.response.send_message(f"Custom leaderboard '{name}' added to {channel.mention}.", ephemeral=True)
        finally:
            db.close()

async def setup(bot):
    await bot.add_cog(CustomLBCog(bot))
