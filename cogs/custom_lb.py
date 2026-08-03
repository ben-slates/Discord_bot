import os
import asyncio
import datetime
import discord
from dotenv import load_dotenv
from discord.ext import commands
from discord import app_commands
from database import SessionLocal, CustomLeaderboard, HallOfFameEntry

load_dotenv()

HALL_OF_FAME_ROLE_NAME = os.getenv("HALL_OF_FAME_ROLE_NAME", "Hall of Fame")
HALL_OF_FAME_ANNOUNCEMENT_CHANNEL_ID = int(os.getenv("HALL_OF_FAME_ANNOUNCEMENT_CHANNEL_ID", "1519244608532647996"))
HALL_OF_FAME_WARNING_CHANNEL_ID = int(os.getenv("HALL_OF_FAME_WARNING_CHANNEL_ID", "1519263271943667774"))
HALL_OF_FAME_DURATION_DAYS = int(os.getenv("HALL_OF_FAME_DURATION_DAYS", "7"))
HALL_OF_FAME_ADMIN_ROLE_NAME = os.getenv("HALL_OF_FAME_ADMIN_ROLE_NAME", "Administrator")


class CustomLBCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="add_leaderboard", description="Admin:Add a custom daily leaderboard to a channel")
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
            await interaction.response.send_message(
                f"Custom leaderboard '{name}' added to {channel.mention}.",
                ephemeral=True,
            )
        finally:
            db.close()

    @app_commands.command(name="remove_leaderboard", description="Admin:Remove a custom leaderboard from a channel")
    @app_commands.default_permissions(administrator=True)
    async def remove_leaderboard(self, interaction: discord.Interaction, channel: discord.TextChannel):
        db = SessionLocal()
        try:
            lb = db.query(CustomLeaderboard).filter_by(channel_id=str(channel.id)).first()
            if not lb:
                await interaction.response.send_message(
                    f"No custom leaderboard is set for {channel.mention}.",
                    ephemeral=True,
                )
                return

            db.delete(lb)
            db.commit()
            await interaction.response.send_message(
                f"Custom leaderboard removed from {channel.mention}.",
                ephemeral=True,
            )
        finally:
            db.close()

    @app_commands.command(name="add_to_hall_of_fame", description="Admin:Add users to the Hall of Fame role")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        user1="First user to add (max 5 total)",
        user2="Second user to add (max 5 total)",
        user3="Third user to add (max 5 total)",
        user4="Fourth user to add (max 5 total)",
        user5="Fifth user to add (max 5 total)",
    )
    async def add_to_hall_of_fame(
        self,
        interaction: discord.Interaction,
        user1: discord.Member = None,
        user2: discord.Member = None,
        user3: discord.Member = None,
        user4: discord.Member = None,
        user5: discord.Member = None,
    ):
        users = [u for u in (user1, user2, user3, user4, user5) if u is not None]
        if not users:
            await interaction.response.send_message("Please provide at least one user.", ephemeral=True)
            return

        if len(users) > 5:
            await interaction.response.send_message("You can add up to 5 users at a time.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if not guild:
            await interaction.followup.send("This command can only be used in a server.", ephemeral=True)
            return

        role = discord.utils.get(guild.roles, name=HALL_OF_FAME_ROLE_NAME)
        if not role:
            try:
                role = await guild.create_role(name=HALL_OF_FAME_ROLE_NAME, reason="Hall of Fame role")
            except discord.Forbidden:
                await interaction.followup.send("I do not have permission to create or manage the Hall of Fame role.", ephemeral=True)
                return

        announcement_channel = guild.get_channel(HALL_OF_FAME_ANNOUNCEMENT_CHANNEL_ID)
        warning_channel = guild.get_channel(HALL_OF_FAME_WARNING_CHANNEL_ID)
        if not announcement_channel or not warning_channel:
            await interaction.followup.send("The configured announcement or warning channel could not be found.", ephemeral=True)
            return

        admin_role = discord.utils.get(guild.roles, name=HALL_OF_FAME_ADMIN_ROLE_NAME)

        db = SessionLocal()
        assigned_users = []
        already_had_role_users = []
        try:
            for user in users:
                existing_entry = db.query(HallOfFameEntry).filter_by(guild_id=str(guild.id), user_id=user.id).first()
                if role in user.roles:
                    already_had_role_users.append(user)
                    if not existing_entry:
                        expires_at = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5))) + datetime.timedelta(days=HALL_OF_FAME_DURATION_DAYS)
                        db.add(HallOfFameEntry(
                            guild_id=str(guild.id),
                            user_id=user.id,
                            role_name=role.name,
                            expires_at=expires_at,
                        ))
                    else:
                        existing_entry.expires_at = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5))) + datetime.timedelta(days=HALL_OF_FAME_DURATION_DAYS)
                    continue

                try:
                    await user.add_roles(role, reason="Added to Hall of Fame")
                    assigned_users.append(user)
                except discord.Forbidden:
                    continue

                expires_at = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5))) + datetime.timedelta(days=HALL_OF_FAME_DURATION_DAYS)
                if existing_entry:
                    existing_entry.expires_at = expires_at
                    existing_entry.role_name = role.name
                else:
                    db.add(HallOfFameEntry(
                        guild_id=str(guild.id),
                        user_id=user.id,
                        role_name=role.name,
                        expires_at=expires_at,
                    ))

            db.commit()

            processed_users = assigned_users + already_had_role_users
            mentions = " ".join(user.mention for user in processed_users)
            if processed_users:
                await announcement_channel.send(
                    f"🎉 Hall of Fame updated! {mentions}"
                )
            else:
                await announcement_channel.send(
                    f"🎉 Hall of Fame update attempted, but the bot could not assign the role."
                )

            await interaction.followup.send(
                f"Processed {len(users)} user(s). New assignments: {len(assigned_users)}. Already had the role: {len(already_had_role_users)}.",
                ephemeral=True,
            )

            asyncio.create_task(
                self._remove_hall_of_fame_role_after_delay(guild, role, warning_channel, admin_role)
            )
        finally:
            db.close()

    async def _remove_hall_of_fame_role_after_delay(self, guild, role, warning_channel, admin_role):
        db = SessionLocal()
        expired_entries = []
        try:
            entries = db.query(HallOfFameEntry).filter_by(guild_id=str(guild.id)).all()
            now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5)))
            for entry in entries:
                if entry.expires_at > now:
                    continue

                expired_entries.append(entry)
                member = guild.get_member(int(entry.user_id))
                if member:
                    try:
                        if role in member.roles:
                            await member.remove_roles(role, reason="Hall of Fame period ended")
                    except discord.Forbidden:
                        pass

                db.delete(entry)
            db.commit()
        finally:
            db.close()

        if expired_entries and warning_channel:
            admin_mention = admin_role.mention if admin_role else "@administrator"
            try:
                await warning_channel.send(
                    f"⚠️ The Hall of Fame period has ended. {admin_mention}"
                )
            except discord.Forbidden:
                pass


async def setup(bot):
    await bot.add_cog(CustomLBCog(bot))
