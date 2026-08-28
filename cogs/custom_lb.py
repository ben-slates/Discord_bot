import os
import asyncio
import datetime
import tempfile
import discord
from dotenv import load_dotenv
from discord.ext import commands
from discord import app_commands
from database import SessionLocal, CustomLeaderboard, HallOfFameEntry, GuildConfig, UserData
from utils.halloffame import render as render_halloffame
from utils.leaderboard import get_leaderboard_channel_id

load_dotenv()

HALL_OF_FAME_DURATION_DAYS = 7
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
    "custom": {
        "label": "Custom",
        "title": "Hall of Fame Updated",
        "description": (
            "The Hall of Fame has been updated to recognize {name} members who have demonstrated outstanding commitment and consistent contributions to Rynex Security.\n\n"
            "Hall of Fame rankings are determined by each member's overall participation, including assigned tasks, meeting attendance, collaboration, community engagement, and accumulated XP."
        ),
        "field_name": "Current {name} Hall of Fame",
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


def build_hall_of_fame_template(template_key, custom_name=None):
    if template_key != "custom":
        return HALL_OF_FAME_TEMPLATES[template_key]

    name = (custom_name or "").strip()
    if not name:
        raise ValueError("Please provide a custom name for the custom Hall of Fame template.")

    return {
        "label": name,
        "title": HALL_OF_FAME_TEMPLATES["custom"]["title"],
        "description": HALL_OF_FAME_TEMPLATES["custom"]["description"].format(name=name),
        "field_name": HALL_OF_FAME_TEMPLATES["custom"]["field_name"].format(name=name),
        "footer": HALL_OF_FAME_TEMPLATES["custom"]["footer"],
    }


def build_hall_of_fame_overall_content(department_key, department_name=None):
    if department_name and department_name.strip():
        label = department_name.strip()
        description = (
            f"The Hall of Fame has been updated to recognize {label} department progress across Rynex Security.\n\n"
            f"{label} is showing strong consistency, collaboration, and contribution across assigned tasks, meeting attendance, community engagement, and accumulated XP."
        )
        return label, description

    label = HALL_OF_FAME_DEPARTMENTS.get(department_key, department_key)
    description = (
        f"The Hall of Fame has been updated to recognize {label} department progress across Rynex Security.\n\n"
        f"{label} is showing strong consistency, collaboration, and contribution across assigned tasks, meeting attendance, community engagement, and accumulated XP."
    )
    return label, description


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

    @app_commands.command(name="enable-ceritification", description="Admin:enable certificate generation for a role and channel")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(role="Only members with this role may generate certificates", channel="Channel where certificates may be generated")
    async def enable_ceritification(self, interaction: discord.Interaction, role: discord.Role, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        db = SessionLocal()
        try:
            config = db.query(GuildConfig).filter_by(guild_id=str(interaction.guild_id)).first()
            if not config:
                config = GuildConfig(guild_id=str(interaction.guild_id))
                db.add(config)
            config.certificate_enabled = True
            config.certificate_role = str(role.id)
            config.certificate_channel = str(channel.id)
            db.commit()
            await interaction.followup.send(
                f"Certificate generation enabled for members with {role.mention} in {channel.mention}.",
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
            app_commands.Choice(name="Attendance", value="attendance"),
            app_commands.Choice(name="Welcome Messages", value="welcome"),
            app_commands.Choice(name="Leaderboard", value="leaderboard"),
            app_commands.Choice(name="Level Up Announcements", value="level_up_announcements"),
            app_commands.Choice(name="Verification", value="verification"),
        ]
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        option="The feature to enable",
        channel="The target channel or category for this feature",
    )
    async def enable_feature(
        self,
        interaction: discord.Interaction,
        option: app_commands.Choice[str],
        channel: discord.abc.GuildChannel,
    ):
        await interaction.response.defer(ephemeral=True)
        db = SessionLocal()
        try:
            config = db.query(GuildConfig).filter_by(guild_id=str(interaction.guild_id)).first()
            if not config:
                config = GuildConfig(guild_id=str(interaction.guild_id))
                db.add(config)

            if option.value == "bot_logs":
                if not isinstance(channel, discord.TextChannel):
                    await interaction.followup.send("Bot logs must use a text channel.", ephemeral=True)
                    return
                config.bot_logs_enabled = True
                config.bot_logs_channel = str(channel.id)
                db.commit()
                await interaction.followup.send(f"Bot logs enabled for {channel.mention}.", ephemeral=True)
                return

            if option.value == "cve_and_news":
                if not isinstance(channel, discord.TextChannel):
                    await interaction.followup.send("CVE and News must use a text channel.", ephemeral=True)
                    return
                config.cve_and_news_enabled = True
                config.cve_and_news_channel = str(channel.id)
                db.commit()
                await interaction.followup.send(f"CVE and News enabled for {channel.mention}.", ephemeral=True)
                return

            if option.value == "support":
                if not isinstance(channel, discord.CategoryChannel):
                    await interaction.followup.send("Support must be enabled with a category.", ephemeral=True)
                    return
                config.support_enabled = True
                config.support_category = str(channel.id)
                db.commit()
                await interaction.followup.send(f"Support enabled for category {channel.mention}.", ephemeral=True)
                return

            if option.value == "attendance":
                if not isinstance(channel, discord.TextChannel):
                    await interaction.followup.send("Attendance must use a text channel.", ephemeral=True)
                    return
                config.attendance_enabled = True
                config.attendance_channel = str(channel.id)
                db.commit()
                await interaction.followup.send(f"Attendance enabled for {channel.mention}.", ephemeral=True)
                return

            if option.value == "welcome":
                if not isinstance(channel, discord.TextChannel):
                    await interaction.followup.send("Welcome messages must use a text channel.", ephemeral=True)
                    return
                config.welcome_enabled = True
                config.welcome_channel = str(channel.id)
                db.commit()
                await interaction.followup.send(f"Welcome messages enabled for {channel.mention}.", ephemeral=True)
                return

            if option.value == "hall_of_fame":
                await interaction.followup.send("Use /enable_hall_of_fame for Hall of Fame setup.", ephemeral=True)
                return

            if option.value == "leaderboard":
                if not isinstance(channel, discord.TextChannel):
                    await interaction.followup.send("Leaderboard must use a text channel.", ephemeral=True)
                    return
                config.leaderboard_enabled = True
                config.leaderboard_channel = str(channel.id)
                db.commit()
                await interaction.followup.send(f"Leaderboard enabled for {channel.mention}.", ephemeral=True)
                return

            if option.value == "level_up_announcements":
                if not isinstance(channel, discord.TextChannel):
                    await interaction.followup.send("Level-up announcements must use a text channel.", ephemeral=True)
                    return
                config.level_up_announcements_enabled = True
                config.level_up_announcements_channel = str(channel.id)
                db.commit()
                await interaction.followup.send(f"Level-up announcements enabled for {channel.mention}.", ephemeral=True)
                return

            if option.value == "verification":
                if not isinstance(channel, discord.TextChannel):
                    await interaction.followup.send("Verification must use a text channel.", ephemeral=True)
                    return
                config.verification_enabled = True
                config.verification_channel = str(channel.id)
                db.commit()
                await interaction.followup.send(f"Verification enabled for {channel.mention}.", ephemeral=True)
                return

            await interaction.followup.send("That option is not supported yet.", ephemeral=True)
        finally:
            db.close()

    @app_commands.command(name="enable_hall_of_fame", description="Admin:Enable Hall of Fame with a role and two channels")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        role_name="The role to use for Hall of Fame",
        announcement_channel="The channel for Hall of Fame announcements",
        warning_channel="The channel for Hall of Fame warning messages",
    )
    async def enable_hall_of_fame(
        self,
        interaction: discord.Interaction,
        role_name: discord.Role,
        announcement_channel: discord.TextChannel,
        warning_channel: discord.TextChannel,
    ):
        db = SessionLocal()
        try:
            config = db.query(GuildConfig).filter_by(guild_id=str(interaction.guild_id)).first()
            if not config:
                config = GuildConfig(guild_id=str(interaction.guild_id))
                db.add(config)

            if not isinstance(announcement_channel, discord.TextChannel):
                await interaction.response.send_message("Announcement channel must be a text channel.", ephemeral=True)
                return
            if not isinstance(warning_channel, discord.TextChannel):
                await interaction.response.send_message("Warning channel must be a text channel.", ephemeral=True)
                return

            config.hall_of_fame_enabled = True
            config.hall_of_fame_channel = str(announcement_channel.id)
            config.hall_of_fame_role_name = role_name.name
            config.hall_of_fame_announcement_channel = str(announcement_channel.id)
            config.hall_of_fame_warning_channel = str(warning_channel.id)
            db.commit()
            await interaction.response.send_message(
                f"Hall of Fame enabled. Role: {config.hall_of_fame_role_name}",
                ephemeral=True,
            )
        finally:
            db.close()

    @app_commands.command(name="disable", description="Admin:Disable a feature for this server")
    @app_commands.choices(
        option=[
            app_commands.Choice(name="Bot Logs", value="bot_logs"),
            app_commands.Choice(name="CVE and News", value="cve_and_news"),
            app_commands.Choice(name="Support", value="support"),
            app_commands.Choice(name="Attendance", value="attendance"),
            app_commands.Choice(name="Welcome Messages", value="welcome"),
            app_commands.Choice(name="Hall of Fame", value="hall_of_fame"),
            app_commands.Choice(name="Leaderboard", value="leaderboard"),
            app_commands.Choice(name="Level Up Announcements", value="level_up_announcements"),
            app_commands.Choice(name="Verification", value="verification"),
            app_commands.Choice(name="Certificate", value="certificate"),
        ]
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(option="The feature to disable")
    async def disable_feature(self, interaction: discord.Interaction, option: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
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
                await interaction.followup.send("Bot logs disabled.", ephemeral=True)
                return

            if option.value == "cve_and_news":
                config.cve_and_news_enabled = False
                config.cve_and_news_channel = None
                db.commit()
                await interaction.followup.send("CVE and News disabled.", ephemeral=True)
                return

            if option.value == "support":
                config.support_enabled = False
                config.support_category = None
                db.commit()
                await interaction.followup.send("Support disabled.", ephemeral=True)
                return

            if option.value == "attendance":
                config.attendance_enabled = False
                config.attendance_channel = None
                db.commit()
                await interaction.followup.send("Attendance disabled.", ephemeral=True)
                return

            if option.value == "welcome":
                config.welcome_enabled = False
                config.welcome_channel = None
                db.commit()
                await interaction.followup.send("Welcome messages disabled.", ephemeral=True)
                return

            if option.value == "hall_of_fame":
                config.hall_of_fame_enabled = False
                config.hall_of_fame_channel = None
                config.hall_of_fame_role_name = None
                config.hall_of_fame_announcement_channel = None
                config.hall_of_fame_warning_channel = None
                db.commit()
                await interaction.followup.send("Hall of Fame disabled.", ephemeral=True)
                return

            if option.value == "leaderboard":
                config.leaderboard_enabled = False
                config.leaderboard_channel = None
                db.commit()
                await interaction.followup.send("Leaderboard disabled.", ephemeral=True)
                return

            if option.value == "level_up_announcements":
                config.level_up_announcements_enabled = False
                config.level_up_announcements_channel = None
                db.commit()
                await interaction.followup.send("Level-up announcements disabled.", ephemeral=True)
                return

            if option.value == "verification":
                config.verification_enabled = False
                config.verification_channel = None
                db.commit()
                await interaction.followup.send("Verification disabled. Existing verification records were preserved.", ephemeral=True)
                return

            if option.value == "certificate":
                config.certificate_enabled = False
                config.certificate_role = None
                config.certificate_channel = None
                db.commit()
                await interaction.followup.send("Certificate generation disabled. Existing verification records were preserved.", ephemeral=True)
                return

            await interaction.followup.send("That option is not supported yet.", ephemeral=True)
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
                    from rankcard import generate_levelup_card # type: ignore

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
    @app_commands.describe(
        channel="The channel where the leaderboard should appear",
        name="The display name for the custom leaderboard",
        role="Optional role that members must have to qualify for this leaderboard",
    )
    async def add_custom_leaderboard(self, interaction: discord.Interaction, channel: discord.TextChannel, name: str, role: discord.Role = None):
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
            lb.required_role_id = str(role.id) if role else None
            db.commit()
            if role:
                await interaction.response.send_message(
                    f"Custom leaderboard '{name}' added to {channel.mention} for members with the {role.name} role.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    f"Custom leaderboard '{name}' added to {channel.mention} for all members.",
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

    @app_commands.command(name="hall_of_fame", description="Admin:Add users to the Hall of Fame and generate an image")
    @app_commands.choices(
        template=[
            app_commands.Choice(name="Red Team", value="red_team"),
            app_commands.Choice(name="Blue Team", value="blue_team"),
            app_commands.Choice(name="Custom", value="custom"),
        ]
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        template="Choose the Hall of Fame template to use",
        user1="First user to add",
        user2="Second user to add",
        user3="Third user to add",
        user4="Fourth user to add",
        user5="Fifth user to add",
        custom_name="Optional custom name to use for the custom template",
    )
    async def add_to_hall_of_fame(
        self,
        interaction: discord.Interaction,
        template: app_commands.Choice[str],
        user1: discord.Member,
        user2: discord.Member,
        user3: discord.Member,
        user4: discord.Member,
        user5: discord.Member,
        custom_name: str = None,
    ):
        db = SessionLocal()
        try:
            config = db.query(GuildConfig).filter_by(guild_id=str(interaction.guild_id)).first()
            if not config or not config.hall_of_fame_enabled or not config.hall_of_fame_channel:
                await interaction.response.send_message("Hall of Fame is not enabled for this server yet.", ephemeral=True)
                return
        finally:
            db.close()

        await interaction.response.defer(ephemeral=True)

        template_key = template.value
        try:
            template_config = build_hall_of_fame_template(template_key, custom_name=custom_name)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except KeyError:
            await interaction.followup.send("Invalid Hall of Fame template selected.", ephemeral=True)
            return

        users = [user1, user2, user3, user4, user5]
        if len({u.id for u in users}) != len(users):
            await interaction.followup.send("Please provide five unique users for the Hall of Fame.", ephemeral=True)
            return

        guild = interaction.guild
        if not guild:
            await interaction.followup.send("This command can only be used in a server.", ephemeral=True)
            return

        role_name = (config.hall_of_fame_role_name or "").strip()
        if not role_name:
            await interaction.followup.send("Hall of Fame role name is not configured for this server yet.", ephemeral=True)
            return
        role = discord.utils.get(guild.roles, name=role_name)
        if not role:
            try:
                role = await guild.create_role(name=role_name, reason="Hall of Fame role")
            except discord.Forbidden:
                await interaction.followup.send("I do not have permission to create or manage the Hall of Fame role.", ephemeral=True)
                return

        announcement_channel_id = config.hall_of_fame_announcement_channel or config.hall_of_fame_channel
        warning_channel_id = config.hall_of_fame_warning_channel or config.hall_of_fame_channel
        announcement_channel = guild.get_channel(int(announcement_channel_id)) if announcement_channel_id else None
        warning_channel = guild.get_channel(int(warning_channel_id)) if warning_channel_id else None
        if not announcement_channel or not warning_channel:
            await interaction.followup.send("The configured Hall of Fame channel could not be found.", ephemeral=True)
            return

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

            user_stats = []
            avatars = []
            ui_db = SessionLocal()
            try:
                for user in users:
                    user_record = ui_db.query(UserData).filter_by(user_id=int(user.id)).first()
                    if user_record:
                        user_stats.append((user.display_name, user_record.xp, user_record.level))
                    else:
                        user_stats.append((user.display_name, 0, 1))
                    avatar_bytes = await user.display_avatar.replace(size=256).read()
                    avatars.append(avatar_bytes)
            finally:
                ui_db.close()

            if processed_users:
                ranking_lines = [f"#{index} • {user.mention}" for index, user in enumerate(processed_users[:5], start=1)]
                embed.add_field(name=template_config["field_name"], value="\n".join(ranking_lines), inline=False)
                filename = f"halloffame_{guild.id}_{interaction.id}.png"
                file_obj = await asyncio.to_thread(render_halloffame, user_stats, None, avatars, None)
                attachment = discord.File(file_obj, filename=filename)
                embed.set_image(url=f"attachment://{filename}")
                await announcement_channel.send(content="@everyone", embed=embed, file=attachment)
            else:
                embed.description = "The Hall of Fame update could not assign the role to any provided user."
                embed.set_footer(text=template_config["footer"])
                await announcement_channel.send(content="@everyone", embed=embed)

            embed.set_footer(text=template_config["footer"])

            try:
                await interaction.followup.send(
                    f"Processed {len(users)} user(s). New assignments: {len(assigned_users)}. Already had the role: {len(already_had_role_users)}.",
                    ephemeral=True,
                )
            except discord.NotFound:
                pass

            asyncio.create_task(
                self._remove_hall_of_fame_role_after_delay(guild, role, warning_channel)
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
        department_name="Optional custom department name to announce",
    )
    async def hall_of_fame_overall(
        self,
        interaction: discord.Interaction,
        department: app_commands.Choice[str] = None,
        department_name: str = None,
    ):
        db = SessionLocal()
        try:
            config = db.query(GuildConfig).filter_by(guild_id=str(interaction.guild_id)).first()
            if not config or not config.hall_of_fame_enabled or not config.hall_of_fame_channel:
                await interaction.response.send_message("Hall of Fame is not enabled for this server yet.", ephemeral=True)
                return
        finally:
            db.close()

        await interaction.response.defer(ephemeral=True)

        department_label, description = build_hall_of_fame_overall_content(
            department.value if department else None,
            department_name=department_name,
        )
        if not department_label:
            await interaction.followup.send("Please choose Red Team or Blue Team, or provide a custom department name.", ephemeral=True)
            return

        guild = interaction.guild
        if not guild:
            await interaction.followup.send("This command can only be used in a server.", ephemeral=True)
            return

        announcement_channel_id = config.hall_of_fame_announcement_channel or config.hall_of_fame_channel
        announcement_channel = guild.get_channel(int(announcement_channel_id)) if announcement_channel_id else None
        if not announcement_channel:
            await interaction.followup.send("The configured announcement channel could not be found.", ephemeral=True)
            return

        template_config = HALL_OF_FAME_TEMPLATES["overall"]
        department_word = department_label.split()[0].lower()
        matching_roles = [role.mention for role in guild.roles if department_word in role.name.lower()]
        embed = discord.Embed(
            title=template_config["title"],
            description=description,
            color=discord.Color.gold(),
            timestamp=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5))),
        )
        embed.add_field(
            name="Top Performing Department",
            value=department_label,
            inline=False,
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

    def _get_admin_role_mention(self, guild):
        for role in sorted(guild.roles, key=lambda r: r.position, reverse=True):
            if role.permissions.administrator:
                return role.mention
        return "@administrator"

    async def _remove_hall_of_fame_role_after_delay(self, guild, role, warning_channel):
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
            admin_mention = self._get_admin_role_mention(guild)
            try:
                await warning_channel.send(
                    f"The Hall of Fame period has ended. {admin_mention}"
                )
            except discord.Forbidden:
                pass


async def setup(bot):
    await bot.add_cog(CustomLBCog(bot))
