import os
import asyncio
import datetime
import discord
from dotenv import load_dotenv
from discord.ext import commands
from discord import app_commands
from database import SessionLocal, CustomLeaderboard, HallOfFameEntry, GuildConfig
from utils.leaderboard import get_leaderboard_channel_id

load_dotenv()

HALL_OF_FAME_ROLE_NAME = os.getenv("HALL_OF_FAME_ROLE_NAME", "Hall of Fame")
HALL_OF_FAME_ANNOUNCEMENT_CHANNEL_ID = int(os.getenv("HALL_OF_FAME_ANNOUNCEMENT_CHANNEL_ID", "1519244608532647996"))
HALL_OF_FAME_WARNING_CHANNEL_ID = int(os.getenv("HALL_OF_FAME_WARNING_CHANNEL_ID", "1519263271943667774"))
HALL_OF_FAME_DURATION_DAYS = int(os.getenv("HALL_OF_FAME_DURATION_DAYS", "7"))
HALL_OF_FAME_ADMIN_ROLE_NAME = os.getenv("HALL_OF_FAME_ADMIN_ROLE_NAME", "Administrator")
HALL_OF_FAME_TEMPLATES = {
    "red_team": {
        "label": "Red Team",
        "title": "Hall of Fame Updated",
        "description": (
            "The Hall of Fame has been updated to recognize Red Team members who have demonstrated outstanding commitment and consistent contributions to Rynex Security.\n\n"
            "Hall of Fame rankings are determined by each member's overall participation, including assigned tasks, meeting attendance, collaboration, community engagement, and accumulated XP."
        ),
        "field_name": "Current Red Team Hall of Fame",
        "footer": "Continue contributing across all areas to climb the rankings and earn your place among the community's top performers.",
    },
    "blue_team": {
        "label": "Blue Team",
        "title": "Hall of Fame Updated",
        "description": (
            "The Hall of Fame has been updated to recognize Blue Team members who have demonstrated outstanding commitment and consistent contributions to Rynex Security.\n\n"
            "Hall of Fame rankings are determined by each member's overall participation, including assigned tasks, meeting attendance, collaboration, community engagement, and accumulated XP."
        ),
        "field_name": "Current Blue Team Hall of Fame",
        "footer": "Continue contributing across all areas to climb the rankings and earn your place among the community's top performers.",
    },
    "overall": {
        "label": "Overall",
        "title": "Hall of Fame Updated",
        "description": (
            "The Hall of Fame has been updated to recognize department progress across Rynex Security.\n\n"
            "Select the department that showed the strongest progress and consistency, then review the current standings below."
        ),
        "field_name": "Department Progress",
        "footer": "Continue contributing across both Red Team and Blue Team to keep department progress moving forward.",
    },
}

HALL_OF_FAME_DEPARTMENTS = {
    "red_team": "Red Team",
    "blue_team": "Blue Team",
}


class CustomLBCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _require_leaderboard_channel(self, interaction: discord.Interaction, db):
        config = db.query(GuildConfig).filter_by(guild_id=str(interaction.guild_id)).first()
        if not config:
            config = GuildConfig(guild_id=str(interaction.guild_id))
            db.add(config)
            db.commit()

        channel_id = get_leaderboard_channel_id(config)
        if not channel_id:
            await interaction.response.send_message("Leaderboard is not enabled for this server.", ephemeral=True)
            return None

        if str(interaction.channel_id) != channel_id:
            await interaction.response.send_message(f"This command can only be used in <#{channel_id}>.", ephemeral=True)
            return None

        return config

    @app_commands.command(name="set_leaderboard", description="Admin:Set one role that qualifies for the main leaderboard")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(role="The single role that should qualify for the main leaderboard")
    async def set_leaderboard(self, interaction: discord.Interaction, role: discord.Role = None):
        db = SessionLocal()
        try:
            config = await self._require_leaderboard_channel(interaction, db)
            if not config:
                return

            if role is None:
                config.main_leaderboard_role_ids = None
                db.commit()
                await interaction.response.send_message("Main leaderboard role cleared.", ephemeral=True)
                return

            config.main_leaderboard_role_ids = str(role.id)
            db.commit()
            await interaction.response.send_message(
                f"Main leaderboard role updated to {role.name}.",
                ephemeral=True,
            )
        finally:
            db.close()

    @app_commands.command(name="enable", description="Admin:Enable a feature for this server")
    @app_commands.choices(
        option=[
            app_commands.Choice(name="Bot Logs", value="bot_logs"),
            app_commands.Choice(name="CVE and News", value="cve_and_news"),
            app_commands.Choice(name="Support", value="support"),
            app_commands.Choice(name="Leaderboard", value="leaderboard"),
            app_commands.Choice(name="Level Up Announcements", value="level_up_announcements"),
        ]
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(option="The feature to enable", channel="The channel/category to use for this feature")
    async def enable_feature(self, interaction: discord.Interaction, option: app_commands.Choice[str], channel: discord.abc.GuildChannel):
        db = SessionLocal()
        try:
            config = db.query(GuildConfig).filter_by(guild_id=str(interaction.guild_id)).first()
            if not config:
                config = GuildConfig(guild_id=str(interaction.guild_id))
                db.add(config)

            if option.value == "bot_logs":
                if not isinstance(channel, discord.TextChannel):
                    await interaction.response.send_message("Bot logs must use a text channel.", ephemeral=True)
                    return
                config.bot_logs_enabled = True
                config.bot_logs_channel = str(channel.id)
                db.commit()
                await interaction.response.send_message(f"Bot logs enabled for {channel.mention}.", ephemeral=True)
                return

            if option.value == "cve_and_news":
                if not isinstance(channel, discord.TextChannel):
                    await interaction.response.send_message("CVE and News must use a text channel.", ephemeral=True)
                    return
                config.cve_and_news_enabled = True
                config.cve_and_news_channel = str(channel.id)
                db.commit()
                await interaction.response.send_message(f"CVE and News enabled for {channel.mention}.", ephemeral=True)
                return

            if option.value == "support":
                if not isinstance(channel, discord.CategoryChannel):
                    await interaction.response.send_message("Support must be enabled with a category.", ephemeral=True)
                    return
                config.support_enabled = True
                config.support_category = str(channel.id)
                db.commit()
                await interaction.response.send_message(f"Support enabled for category {channel.mention}.", ephemeral=True)
                return

            if option.value == "leaderboard":
                if not isinstance(channel, discord.TextChannel):
                    await interaction.response.send_message("Leaderboard must use a text channel.", ephemeral=True)
                    return
                config.leaderboard_enabled = True
                config.leaderboard_channel = str(channel.id)
                db.commit()
                await interaction.response.send_message(f"Leaderboard enabled for {channel.mention}.", ephemeral=True)
                return

            if option.value == "level_up_announcements":
                if not isinstance(channel, discord.TextChannel):
                    await interaction.response.send_message("Level-up announcements must use a text channel.", ephemeral=True)
                    return
                config.level_up_announcements_enabled = True
                config.level_up_announcements_channel = str(channel.id)
                db.commit()
                await interaction.response.send_message(f"Level-up announcements enabled for {channel.mention}.", ephemeral=True)
                return

            await interaction.response.send_message("That option is not supported yet.", ephemeral=True)
        finally:
            db.close()

    @app_commands.command(name="disable", description="Admin:Disable a feature for this server")
    @app_commands.choices(
        option=[
            app_commands.Choice(name="Bot Logs", value="bot_logs"),
            app_commands.Choice(name="CVE and News", value="cve_and_news"),
            app_commands.Choice(name="Support", value="support"),
            app_commands.Choice(name="Leaderboard", value="leaderboard"),
            app_commands.Choice(name="Level Up Announcements", value="level_up_announcements"),
        ]
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(option="The feature to disable")
    async def disable_feature(self, interaction: discord.Interaction, option: app_commands.Choice[str]):
        db = SessionLocal()
        try:
            config = db.query(GuildConfig).filter_by(guild_id=str(interaction.guild_id)).first()
            if not config:
                config = GuildConfig(guild_id=str(interaction.guild_id))
                db.add(config)

            if option.value == "bot_logs":
                config.bot_logs_enabled = False
                config.bot_logs_channel = None
                db.commit()
                await interaction.response.send_message("Bot logs disabled.", ephemeral=True)
                return

            if option.value == "cve_and_news":
                config.cve_and_news_enabled = False
                config.cve_and_news_channel = None
                db.commit()
                await interaction.response.send_message("CVE and News disabled.", ephemeral=True)
                return

            if option.value == "support":
                config.support_enabled = False
                config.support_category = None
                db.commit()
                await interaction.response.send_message("Support disabled.", ephemeral=True)
                return

            if option.value == "leaderboard":
                config.leaderboard_enabled = False
                config.leaderboard_channel = None
                db.commit()
                await interaction.response.send_message("Leaderboard disabled.", ephemeral=True)
                return

            if option.value == "level_up_announcements":
                config.level_up_announcements_enabled = False
                config.level_up_announcements_channel = None
                db.commit()
                await interaction.response.send_message("Level-up announcements disabled.", ephemeral=True)
                return

            await interaction.response.send_message("That option is not supported yet.", ephemeral=True)
        finally:
            db.close()

    @app_commands.command(name="test", description="Admin:Send a test message to verify an enabled feature")
    @app_commands.choices(
        option=[
            app_commands.Choice(name="Bot Logs", value="bot_logs"),
            app_commands.Choice(name="CVE and News", value="cve_and_news"),
            app_commands.Choice(name="Leaderboard", value="leaderboard"),
            app_commands.Choice(name="Level Up Announcements", value="level_up_announcements"),
        ]
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(option="The feature to test")
    async def test_feature(self, interaction: discord.Interaction, option: app_commands.Choice[str]):
        db = SessionLocal()
        try:
            config = db.query(GuildConfig).filter_by(guild_id=str(interaction.guild_id)).first()
            if option.value == "bot_logs":
                if not config or not config.bot_logs_enabled or not config.bot_logs_channel:
                    await interaction.response.send_message("Bot logs are not enabled for this server yet.", ephemeral=True)
                    return

                channel = interaction.guild.get_channel(int(config.bot_logs_channel))
                if not channel:
                    await interaction.response.send_message("The configured bot logs channel could not be found.", ephemeral=True)
                    return

                try:
                    await channel.send(f"✅ Bot logs test message from {interaction.user.mention}")
                except discord.Forbidden:
                    await interaction.response.send_message("I do not have permission to send messages to that channel.", ephemeral=True)
                    return

                await interaction.response.send_message("Test message sent successfully.", ephemeral=True)
                return

            if option.value == "cve_and_news":
                if not config or not config.cve_and_news_enabled or not config.cve_and_news_channel:
                    await interaction.response.send_message("CVE and News are not enabled for this server yet.", ephemeral=True)
                    return

                channel = interaction.guild.get_channel(int(config.cve_and_news_channel))
                if not channel:
                    await interaction.response.send_message("The configured CVE and News channel could not be found.", ephemeral=True)
                    return

                try:
                    await channel.send(f"✅ CVE and News test message from {interaction.user.mention}")
                except discord.Forbidden:
                    await interaction.response.send_message("I do not have permission to send messages to that channel.", ephemeral=True)
                    return

                await interaction.response.send_message("Test message sent successfully.", ephemeral=True)
                return

            if option.value == "leaderboard":
                if not config or not config.leaderboard_enabled or not config.leaderboard_channel:
                    await interaction.response.send_message("Leaderboard is not enabled for this server yet.", ephemeral=True)
                    return

                channel = interaction.guild.get_channel(int(config.leaderboard_channel))
                if not channel:
                    await interaction.response.send_message("The configured leaderboard channel could not be found.", ephemeral=True)
                    return

                try:
                    await channel.send(f"✅ Leaderboard test message from {interaction.user.mention}")
                except discord.Forbidden:
                    await interaction.response.send_message("I do not have permission to send messages to that channel.", ephemeral=True)
                    return

                await interaction.response.send_message("Test message sent successfully.", ephemeral=True)
                return

            if option.value == "level_up_announcements":
                if not config or not config.level_up_announcements_enabled or not config.level_up_announcements_channel:
                    await interaction.response.send_message("Level-up announcements are not enabled for this server yet.", ephemeral=True)
                    return

                channel = interaction.guild.get_channel(int(config.level_up_announcements_channel))
                if not channel:
                    await interaction.response.send_message("The configured level-up announcements channel could not be found.", ephemeral=True)
                    return

                try:
                    import os
                    import sys
                    sys.path.append(os.path.join(os.path.dirname(__file__), "..", "utils"))
                    from rankcard import generate_levelup_card

                    card_file = await generate_levelup_card(
                        interaction.user,
                        100,
                        max_level=100,
                        previous_level=99,
                    )
                    await channel.send(f"✅ Level-up announcement test for {interaction.user.mention}", file=card_file)
                except discord.Forbidden:
                    await interaction.response.send_message("I do not have permission to send messages to that channel.", ephemeral=True)
                    return
                except Exception as e:
                    await interaction.response.send_message(f"Could not send the level-up test card: {e}", ephemeral=True)
                    return

                await interaction.response.send_message("Level-up test card sent successfully.", ephemeral=True)
                return

            await interaction.response.send_message("That option is not supported yet.", ephemeral=True)
        finally:
            db.close()

    @app_commands.command(name="add_custom_leaderboard", description="Admin:Add a custom daily leaderboard to a channel")
    @app_commands.default_permissions(administrator=True)
    async def add_custom_leaderboard(self, interaction: discord.Interaction, channel: discord.TextChannel, name: str):
        db = SessionLocal()
        try:
            config = db.query(GuildConfig).filter_by(guild_id=str(interaction.guild_id)).first()
            if not config:
                config = GuildConfig(guild_id=str(interaction.guild_id))
                db.add(config)
                db.commit()

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

    @app_commands.command(name="remove_custom_leaderboard", description="Admin:Remove a custom leaderboard from a channel")
    @app_commands.default_permissions(administrator=True)
    async def remove_custom_leaderboard(self, interaction: discord.Interaction, channel: discord.TextChannel):
        db = SessionLocal()
        try:
            config = db.query(GuildConfig).filter_by(guild_id=str(interaction.guild_id)).first()
            if not config:
                config = GuildConfig(guild_id=str(interaction.guild_id))
                db.add(config)
                db.commit()

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

    @app_commands.command(name="hall_of_fame", description="Admin:Add users to the Hall of Fame role")
    @app_commands.choices(
        template=[
            app_commands.Choice(name="Red Team", value="red_team"),
            app_commands.Choice(name="Blue Team", value="blue_team"),
        ]
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        template="Choose the Hall of Fame template to use",
        user1="First user to add (max 5 total)",
        user2="Second user to add (max 5 total)",
        user3="Third user to add (max 5 total)",
        user4="Fourth user to add (max 5 total)",
        user5="Fifth user to add (max 5 total)",
    )
    async def add_to_hall_of_fame(
        self,
        interaction: discord.Interaction,
        template: app_commands.Choice[str],
        user1: discord.Member = None,
        user2: discord.Member = None,
        user3: discord.Member = None,
        user4: discord.Member = None,
        user5: discord.Member = None,
    ):
        await interaction.response.defer(ephemeral=True)

        template_key = template.value
        template_config = HALL_OF_FAME_TEMPLATES.get(template_key)
        if not template_config:
            await interaction.followup.send("Invalid Hall of Fame template selected.", ephemeral=True)
            return

        users = [u for u in (user1, user2, user3, user4, user5) if u is not None]
        if not users:
            await interaction.followup.send("Please provide at least one user.", ephemeral=True)
            return

        if len(users) > 5:
            await interaction.followup.send(f"You can add up to 5 user(s) for the {template_config['label']} template.", ephemeral=True)
            return

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
            embed = discord.Embed(
                title=template_config["title"],
                description=template_config["description"],
                color=discord.Color.gold(),
                timestamp=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5))),
            )
            if processed_users:
                ranking_lines = [f"#{index} • {user.mention}" for index, user in enumerate(processed_users[:5], start=1)]
                embed.add_field(name=template_config["field_name"], value="\n".join(ranking_lines), inline=False)
            else:
                embed.description = "The Hall of Fame update could not assign the role to any provided user."

            embed.set_footer(text=template_config["footer"])
            await announcement_channel.send(content="@everyone", embed=embed)

            try:
                await interaction.followup.send(
                    f"Processed {len(users)} user(s). New assignments: {len(assigned_users)}. Already had the role: {len(already_had_role_users)}.",
                    ephemeral=True,
                )
            except discord.NotFound:
                pass

            asyncio.create_task(
                self._remove_hall_of_fame_role_after_delay(guild, role, warning_channel, admin_role)
            )
        finally:
            db.close()

    @app_commands.command(name="hall_of_fame_overall", description="Admin:Announce department progress for the Hall of Fame")
    @app_commands.choices(
        department=[
            app_commands.Choice(name="Red Team", value="red_team"),
            app_commands.Choice(name="Blue Team", value="blue_team"),
        ]
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        department="Choose the department to announce",
    )
    async def hall_of_fame_overall(
        self,
        interaction: discord.Interaction,
        department: app_commands.Choice[str],
    ):
        await interaction.response.defer(ephemeral=True)

        department_label = HALL_OF_FAME_DEPARTMENTS.get(department.value)
        if not department_label:
            await interaction.followup.send("Please choose Red Team or Blue Team.", ephemeral=True)
            return

        guild = interaction.guild
        if not guild:
            await interaction.followup.send("This command can only be used in a server.", ephemeral=True)
            return

        announcement_channel = guild.get_channel(HALL_OF_FAME_ANNOUNCEMENT_CHANNEL_ID)
        if not announcement_channel:
            await interaction.followup.send("The configured announcement channel could not be found.", ephemeral=True)
            return

        template_config = HALL_OF_FAME_TEMPLATES["overall"]
        department_word = department_label.split()[0].lower()
        matching_roles = [role.mention for role in guild.roles if department_word in role.name.lower()]
        embed = discord.Embed(
            title=template_config["title"],
            description=(
                f"The Hall of Fame has been updated to recognize {department_label} department progress across Rynex Security.\n\n"
                f"{department_label} is showing strong consistency, collaboration, and contribution across assigned tasks, meeting attendance, community engagement, and accumulated XP."
            ),
            color=discord.Color.gold(),
            timestamp=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5))),
        )
        embed.add_field(
            name=f"{department_label} Department Progress",
            value=f"{department_label} progress is good. Keep pushing the department forward and maintain the momentum.",
            inline=False,
        )
        embed.add_field(
            name="Related Roles",
            value="\n".join(matching_roles) if matching_roles else f"No roles found containing '{department_word}'.",
            inline=False,
        )
        embed.set_footer(text=template_config["footer"])
        await announcement_channel.send(content="@everyone", embed=embed)

        try:
            await interaction.followup.send(
                f"Posted Overall department progress for {department_label}.",
                ephemeral=True,
            )
        except discord.NotFound:
            pass

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
