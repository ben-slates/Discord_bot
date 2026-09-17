import os
import asyncio
import datetime
import tempfile
import logging
import discord
from dotenv import load_dotenv
from discord.ext import commands
from discord import app_commands
from database import SessionLocal, CustomLeaderboard, HallOfFameEntry, GuildConfig, UserData
from utils.halloffame import render as render_halloffame
from utils.leaderboard import get_leaderboard_channel_id
from utils.db_executor import run_db

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


class FeatureEnableModal(discord.ui.Modal, title="Enable Feature"):
    def __init__(self, cog):
        super().__init__()
        self.cog = cog
        self.feature = discord.ui.TextInput(
            label="Feature name",
            placeholder="verification, support, attendance, leaderboard...",
            required=True,
            max_length=40,
        )
        self.channel_id = discord.ui.TextInput(
            label="Channel/category ID",
            placeholder="Paste the Discord channel or category ID",
            required=True,
            max_length=25,
        )
        self.add_item(self.feature)
        self.add_item(self.channel_id)

    async def on_submit(self, interaction: discord.Interaction):
        feature = self.feature.value.strip().lower().replace(" ", "_")
        try:
            channel = interaction.guild.get_channel(int(self.channel_id.value.strip()))
        except (TypeError, ValueError):
            channel = None
        if not channel:
            await interaction.response.send_message("I could not find that channel or category ID.", ephemeral=True)
            return
        if feature == "support":
            from cogs.support import SupportRoleModal
            if not isinstance(channel, discord.CategoryChannel):
                await interaction.response.send_message("Support requires a category.", ephemeral=True)
                return
            await interaction.response.send_modal(SupportRoleModal(self.cog.bot, channel))
            return
        await interaction.response.defer(ephemeral=True)
        await self.cog.enable_feature_settings(interaction, feature, channel)


class FeatureDisableModal(discord.ui.Modal, title="Disable Feature"):
    def __init__(self, cog):
        super().__init__()
        self.cog = cog
        self.feature = discord.ui.TextInput(
            label="Feature name",
            placeholder="verification, support, attendance, certificate...",
            required=True,
            max_length=40,
        )
        self.add_item(self.feature)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        feature = self.feature.value.strip().lower().replace(" ", "_")
        await self.cog.disable_feature_settings(interaction, feature)


class CertificationSetupModal(discord.ui.Modal, title="Enable Certification"):
    def __init__(self, cog):
        super().__init__()
        self.cog = cog
        self.role_id = discord.ui.TextInput(label="Required role ID", required=True, max_length=25)
        self.channel_id = discord.ui.TextInput(label="Certification channel ID", required=True, max_length=25)
        self.add_item(self.role_id)
        self.add_item(self.channel_id)

    async def on_submit(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(int(self.role_id.value.strip())) if self.role_id.value.strip().isdigit() else None
        try:
            channel = interaction.guild.get_channel(int(self.channel_id.value.strip()))
        except (TypeError, ValueError):
            channel = None
        if not role or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Enter a valid role ID and text-channel ID.", ephemeral=True)
            return
        await self.cog.enable_certification(interaction, role, channel)


class HallOfFameSetupModal(discord.ui.Modal, title="Enable Hall of Fame"):
    def __init__(self, cog):
        super().__init__()
        self.cog = cog
        self.role_id = discord.ui.TextInput(label="Hall of Fame role ID", required=True, max_length=25)
        self.announcement_id = discord.ui.TextInput(label="Announcement channel ID", required=True, max_length=25)
        self.warning_id = discord.ui.TextInput(label="Warning channel ID", required=True, max_length=25)
        self.add_item(self.role_id)
        self.add_item(self.announcement_id)
        self.add_item(self.warning_id)

    async def on_submit(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(int(self.role_id.value.strip())) if self.role_id.value.strip().isdigit() else None
        try:
            announcement = interaction.guild.get_channel(int(self.announcement_id.value.strip()))
            warning = interaction.guild.get_channel(int(self.warning_id.value.strip()))
        except (TypeError, ValueError):
            announcement = warning = None
        if not role or not isinstance(announcement, discord.TextChannel) or not isinstance(warning, discord.TextChannel):
            await interaction.response.send_message("Enter a valid role ID and two text-channel IDs.", ephemeral=True)
            return
        await self.cog.enable_hall_of_fame(interaction, role, announcement, warning)


def _select_options(items, empty_label="No channels available"):
    options = [discord.SelectOption(label=item.name[:100], value=str(item.id)) for item in items[:25]]
    return options or [discord.SelectOption(label=empty_label, value="0")]


def _resolve_selected_channel(guild: discord.Guild, selected):
    """Convert a ChannelSelect interaction value to the cached guild channel.

    discord.py may return an AppCommandChannel wrapper from a native channel
    select. It has an ID but is not a TextChannel/CategoryChannel instance.
    """
    return guild.get_channel(int(selected.id)) if selected else None


class SettingsErrorView(discord.ui.View):
    async def on_error(self, interaction, error, item):
        logging.error(
            "Settings panel interaction failed",
            exc_info=(type(error), error, error.__traceback__),
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send("The settings action failed. Please try again.", ephemeral=True)
            else:
                await interaction.response.send_message("The settings action failed. Please try again.", ephemeral=True)
        except discord.HTTPException:
            pass


class FeatureEnableSelectView(SettingsErrorView):
    def __init__(self, cog, guild):
        super().__init__(timeout=300)
        self.cog = cog
        self.feature = discord.ui.Select(
            placeholder="Select a feature to enable",
            options=[
                discord.SelectOption(label="Bot Logs", value="bot_logs"),
                discord.SelectOption(label="CVE and News", value="cve_and_news"),
                discord.SelectOption(label="Quote of the Day", value="quote_of_day"),
                discord.SelectOption(label="Support", value="support"),
                discord.SelectOption(label="Attendance", value="attendance"),
                discord.SelectOption(label="Welcome Messages", value="welcome"),
                discord.SelectOption(label="Leaderboard", value="leaderboard"),
                discord.SelectOption(label="Level-Up Announcements", value="level_up_announcements"),
                discord.SelectOption(label="Verification", value="verification"),
            ],
        )
        self.channel = discord.ui.ChannelSelect(
            placeholder="Select the target channel/category",
            channel_types=[discord.ChannelType.text, discord.ChannelType.category],
        )
        self.add_item(self.feature)
        self.add_item(self.channel)
        self.feature.callback = self._selection_changed
        self.channel.callback = self._selection_changed

    async def _selection_changed(self, interaction):
        # A select-menu choice is its own Discord interaction. A defer is the
        # fastest acknowledgement and remains reliable when Discord/API calls
        # are slow; the selected value stays on this view for Apply.
        await interaction.response.defer(ephemeral=True)

    @discord.ui.button(label="Apply", style=discord.ButtonStyle.success)
    async def apply(self, interaction, button):
        feature = self.feature.values[0] if self.feature.values else None
        channel = _resolve_selected_channel(interaction.guild, self.channel.values[0] if self.channel.values else None)
        if not feature or not channel:
            await interaction.response.send_message("Select both a feature and a channel first.", ephemeral=True)
            return
        if feature == "support":
            from cogs.support import SupportRoleModal
            if not isinstance(channel, discord.CategoryChannel):
                await interaction.response.send_message("Support requires a category.", ephemeral=True)
                return
            await interaction.response.send_modal(SupportRoleModal(self.cog.bot, channel))
            return
        await interaction.response.defer(ephemeral=True)
        await self.cog.enable_feature_settings(interaction, feature, channel)


class FeatureDisableSelectView(SettingsErrorView):
    def __init__(self, cog):
        super().__init__(timeout=300)
        self.cog = cog
        self.feature = discord.ui.Select(
            placeholder="Select a feature to disable",
            options=[discord.SelectOption(label=label, value=value) for label, value in [
                ("Bot Logs", "bot_logs"), ("CVE and News", "cve_and_news"), ("Quote of the Day", "quote_of_day"), ("Support", "support"),
                ("Attendance", "attendance"), ("Welcome Messages", "welcome"), ("Hall of Fame", "hall_of_fame"),
                ("Leaderboard", "leaderboard"), ("Level-Up Announcements", "level_up_announcements"),
                ("Verification", "verification"), ("Certificate", "certificate"),
            ]],
        )
        self.add_item(self.feature)
        self.feature.callback = self._selection_changed

    async def _selection_changed(self, interaction):
        await interaction.response.defer(ephemeral=True)

    @discord.ui.button(label="Disable Selected Feature", style=discord.ButtonStyle.danger)
    async def apply(self, interaction, button):
        if not self.feature.values:
            await interaction.response.send_message("Select a feature first.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self.cog.disable_feature_settings(interaction, self.feature.values[0])


class CertificationSelectView(SettingsErrorView):
    def __init__(self, cog, guild):
        super().__init__(timeout=300)
        self.cog = cog
        self.role = discord.ui.RoleSelect(placeholder="Select the required role")
        self.channel = discord.ui.ChannelSelect(placeholder="Select the certification channel", channel_types=[discord.ChannelType.text])
        self.add_item(self.role)
        self.add_item(self.channel)
        self.role.callback = self._selection_changed
        self.channel.callback = self._selection_changed

    async def _selection_changed(self, interaction):
        await interaction.response.defer(ephemeral=True)

    @discord.ui.button(label="Enable Certification", style=discord.ButtonStyle.success)
    async def apply(self, interaction, button):
        role = self.role.values[0] if self.role.values else None
        channel = _resolve_selected_channel(interaction.guild, self.channel.values[0] if self.channel.values else None)
        if not role or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Select a role and text channel first.", ephemeral=True)
            return
        await self.cog.enable_certification(interaction, role, channel)


class HallOfFameSelectView(SettingsErrorView):
    def __init__(self, cog, guild):
        super().__init__(timeout=300)
        self.cog = cog
        self.role = discord.ui.RoleSelect(placeholder="Select the Hall of Fame role")
        self.announcement = discord.ui.ChannelSelect(placeholder="Select announcement channel", channel_types=[discord.ChannelType.text])
        self.warning = discord.ui.ChannelSelect(placeholder="Select warning channel", channel_types=[discord.ChannelType.text])
        self.add_item(self.role)
        self.add_item(self.announcement)
        self.add_item(self.warning)
        self.role.callback = self._selection_changed
        self.announcement.callback = self._selection_changed
        self.warning.callback = self._selection_changed

    async def _selection_changed(self, interaction):
        await interaction.response.defer(ephemeral=True)

    @discord.ui.button(label="Enable Hall of Fame", style=discord.ButtonStyle.success)
    async def apply(self, interaction, button):
        role = self.role.values[0] if self.role.values else None
        announcement = _resolve_selected_channel(interaction.guild, self.announcement.values[0] if self.announcement.values else None)
        warning = _resolve_selected_channel(interaction.guild, self.warning.values[0] if self.warning.values else None)
        if not role or not isinstance(announcement, discord.TextChannel) or not isinstance(warning, discord.TextChannel):
            await interaction.response.send_message("Select a role and both text channels first.", ephemeral=True)
            return
        await self.cog.enable_hall_of_fame(interaction, role, announcement, warning)


class MainLeaderboardRoleView(SettingsErrorView):
    def __init__(self, cog):
        super().__init__(timeout=300)
        self.cog = cog
        self.role = discord.ui.RoleSelect(placeholder="Select the main leaderboard role")
        self.add_item(self.role)
        self.role.callback = self._selection_changed

    async def _selection_changed(self, interaction):
        await interaction.response.defer(ephemeral=True)

    @discord.ui.button(label="Save Role", style=discord.ButtonStyle.success)
    async def save(self, interaction, button):
        if not self.role.values:
            await interaction.response.send_message("Select a role first.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        saved = await run_db(_set_main_leaderboard_role_worker, interaction.guild_id, self.role.values[0].id)
        if not saved:
            await interaction.followup.send("Enable the main leaderboard before setting its qualifying role.", ephemeral=True)
            return
        await interaction.followup.send(f"Main leaderboard role updated to {self.role.values[0].mention}.", ephemeral=True)

    @discord.ui.button(label="Clear Role", style=discord.ButtonStyle.secondary)
    async def clear(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        saved = await run_db(_set_main_leaderboard_role_worker, interaction.guild_id, None)
        await interaction.followup.send(
            "Main leaderboard role cleared." if saved else "Enable the main leaderboard before changing its qualifying role.",
            ephemeral=True,
        )


class CustomLeaderboardNameModal(discord.ui.Modal, title="Custom Leaderboard Name"):
    def __init__(self, cog, channel, role):
        super().__init__()
        self.cog = cog
        self.channel = channel
        self.role = role
        self.name = discord.ui.TextInput(label="Leaderboard name", placeholder="e.g. Blue Team Daily", required=True, max_length=100)
        self.add_item(self.name)

    async def on_submit(self, interaction):
        name = self.name.value.strip()
        if not name:
            await interaction.response.send_message("Enter a leaderboard name.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await run_db(
            _upsert_custom_leaderboard_worker,
            interaction.guild_id,
            self.channel.id,
            name,
            self.role.id if self.role else None,
        )
        scope = f"members with {self.role.mention}" if self.role else "all members"
        await interaction.followup.send(f"Custom leaderboard **{name}** saved for {self.channel.mention} ({scope}).", ephemeral=True)


class CustomLeaderboardSetupView(SettingsErrorView):
    def __init__(self, cog):
        super().__init__(timeout=300)
        self.cog = cog
        self.channel = discord.ui.ChannelSelect(placeholder="Select the leaderboard channel", channel_types=[discord.ChannelType.text])
        self.role = discord.ui.RoleSelect(placeholder="Optional: select a qualifying role")
        self.add_item(self.channel)
        self.add_item(self.role)
        self.channel.callback = self._selection_changed
        self.role.callback = self._selection_changed

    async def _selection_changed(self, interaction):
        await interaction.response.defer(ephemeral=True)

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.success)
    async def continue_setup(self, interaction, button):
        if not self.channel.values:
            await interaction.response.send_message("Select a text channel first.", ephemeral=True)
            return
        await interaction.response.send_modal(
            CustomLeaderboardNameModal(
                self.cog,
                _resolve_selected_channel(interaction.guild, self.channel.values[0]),
                self.role.values[0] if self.role.values else None,
            )
        )


class CustomLeaderboardRemoveView(SettingsErrorView):
    def __init__(self, cog):
        super().__init__(timeout=300)
        self.cog = cog
        self.channel = discord.ui.ChannelSelect(placeholder="Select the custom leaderboard channel", channel_types=[discord.ChannelType.text])
        self.add_item(self.channel)
        self.channel.callback = self._selection_changed

    async def _selection_changed(self, interaction):
        await interaction.response.defer(ephemeral=True)

    @discord.ui.button(label="Remove Custom Leaderboard", style=discord.ButtonStyle.danger)
    async def remove(self, interaction, button):
        if not self.channel.values:
            await interaction.response.send_message("Select a text channel first.", ephemeral=True)
            return
        channel = _resolve_selected_channel(interaction.guild, self.channel.values[0])
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Select a valid text channel first.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        removed = await run_db(_remove_custom_leaderboard_worker, interaction.guild_id, channel.id)
        await interaction.followup.send(
            f"Custom leaderboard removed from {channel.mention}." if removed else f"No custom leaderboard is configured for {channel.mention}.",
            ephemeral=True,
        )


class LeaderboardSettingsView(SettingsErrorView):
    def __init__(self, cog):
        super().__init__(timeout=300)
        self.cog = cog

    @discord.ui.button(label="Set Main Leaderboard Role", style=discord.ButtonStyle.primary)
    async def set_main_role(self, interaction, button):
        await interaction.response.send_message("Select the one role that qualifies for the main leaderboard:", view=MainLeaderboardRoleView(self.cog), ephemeral=True)

    @discord.ui.button(label="Add Custom Leaderboard", style=discord.ButtonStyle.success)
    async def add_custom(self, interaction, button):
        await interaction.response.send_message("Select a channel and, optionally, a qualifying role:", view=CustomLeaderboardSetupView(self.cog), ephemeral=True)

    @discord.ui.button(label="Remove Custom Leaderboard", style=discord.ButtonStyle.danger)
    async def remove_custom(self, interaction, button):
        await interaction.response.send_message("Select the custom leaderboard channel to remove:", view=CustomLeaderboardRemoveView(self.cog), ephemeral=True)


class FeatureTestView(SettingsErrorView):
    def __init__(self, cog):
        super().__init__(timeout=300)
        self.cog = cog
        self.feature = discord.ui.Select(
            placeholder="Select an enabled feature to test",
            options=[
                discord.SelectOption(label="Bot Logs", value="bot_logs"),
                discord.SelectOption(label="CVE and News", value="cve_and_news"),
                discord.SelectOption(label="Leaderboard", value="leaderboard"),
                discord.SelectOption(label="Level-Up Announcements", value="level_up_announcements"),
                discord.SelectOption(label="Quote of the Day", value="quote_of_day"),
            ],
        )
        self.add_item(self.feature)
        self.feature.callback = self._selected

    async def _selected(self, interaction):
        await interaction.response.defer(ephemeral=True)

    @discord.ui.button(label="Run Test", style=discord.ButtonStyle.success)
    async def run_test(self, interaction, button):
        if not self.feature.values:
            await interaction.response.send_message("Select a feature first.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self.cog.test_feature_settings(interaction, self.feature.values[0])


class SettingsPanelView(SettingsErrorView):
    def __init__(self, cog):
        # The panel is an ephemeral admin tool; keep its buttons active for
        # the lifetime of the bot instead of silently expiring after 5 minutes.
        super().__init__(timeout=None)
        self.cog = cog

    async def _admin(self, interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only administrators can use the bot settings panel.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Enable Feature", style=discord.ButtonStyle.success)
    async def enable(self, interaction, button):
        if await self._admin(interaction):
            await interaction.response.defer(ephemeral=True)
            await interaction.followup.send("Select the feature and target channel/category:", view=FeatureEnableSelectView(self.cog, interaction.guild), ephemeral=True)

    @discord.ui.button(label="Disable Feature", style=discord.ButtonStyle.danger)
    async def disable(self, interaction, button):
        if await self._admin(interaction):
            await interaction.response.defer(ephemeral=True)
            await interaction.followup.send("Select the feature to disable:", view=FeatureDisableSelectView(self.cog), ephemeral=True)

    @discord.ui.button(label="Enable Certification", style=discord.ButtonStyle.primary)
    async def certification(self, interaction, button):
        if await self._admin(interaction):
            await interaction.response.defer(ephemeral=True)
            await interaction.followup.send("Select the required role and certification channel:", view=CertificationSelectView(self.cog, interaction.guild), ephemeral=True)

    @discord.ui.button(label="Enable Hall of Fame", style=discord.ButtonStyle.primary)
    async def hall_of_fame(self, interaction, button):
        if await self._admin(interaction):
            await interaction.response.defer(ephemeral=True)
            await interaction.followup.send("Select the role, announcement channel, and warning channel:", view=HallOfFameSelectView(self.cog, interaction.guild), ephemeral=True)

    @discord.ui.button(label="Leaderboard Settings", style=discord.ButtonStyle.secondary)
    async def leaderboard_settings(self, interaction, button):
        if await self._admin(interaction):
            await interaction.response.send_message(
                "Configure the main qualifying role or add/remove custom leaderboards:",
                view=LeaderboardSettingsView(self.cog),
                ephemeral=True,
            )

    @discord.ui.button(label="Test Feature", style=discord.ButtonStyle.secondary)
    async def test_feature(self, interaction, button):
        if await self._admin(interaction):
            await interaction.response.send_message(
                "Select an enabled feature to send its test message:",
                view=FeatureTestView(self.cog),
                ephemeral=True,
            )


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


def _enable_feature_worker(guild_id, option, channel_id):
    db = SessionLocal()
    try:
        config = db.query(GuildConfig).filter_by(guild_id=str(guild_id)).first()
        if not config:
            config = GuildConfig(guild_id=str(guild_id))
            db.add(config)
        fields = {
            "bot_logs": ("bot_logs_enabled", "bot_logs_channel", "Bot logs"),
            "cve_and_news": ("cve_and_news_enabled", "cve_and_news_channel", "CVE and News"),
            "quote_of_day": ("quote_of_day_enabled", "quote_of_day_channel", "Quote of the Day"),
            "attendance": ("attendance_enabled", "attendance_channel", "Attendance"),
            "welcome": ("welcome_enabled", "welcome_channel", "Welcome messages"),
            "leaderboard": ("leaderboard_enabled", "leaderboard_channel", "Leaderboard"),
            "level_up_announcements": ("level_up_announcements_enabled", "level_up_announcements_channel", "Level-up announcements"),
            "verification": ("verification_enabled", "verification_channel", "Verification"),
        }
        entry = fields.get(option)
        if not entry:
            return None, "Unknown feature. Use one of the feature names shown in the panel."
        enabled_field, channel_field, label = entry
        setattr(config, enabled_field, True)
        setattr(config, channel_field, str(channel_id))
        db.commit()
        return label, None
    finally:
        db.close()


def _disable_feature_worker(guild_id, option):
    db = SessionLocal()
    try:
        config = db.query(GuildConfig).filter_by(guild_id=str(guild_id)).first()
        if not config:
            return None, "No feature configuration exists for this server."
        fields = {
            "bot_logs": ("bot_logs_enabled", "bot_logs_channel", "Bot logs"),
            "cve_and_news": ("cve_and_news_enabled", "cve_and_news_channel", "CVE and News"),
            "quote_of_day": ("quote_of_day_enabled", "quote_of_day_channel", "Quote of the Day"),
            "support": ("support_enabled", "support_category", "Support"),
            "attendance": ("attendance_enabled", "attendance_channel", "Attendance"),
            "welcome": ("welcome_enabled", "welcome_channel", "Welcome messages"),
            "hall_of_fame": ("hall_of_fame_enabled", None, "Hall of Fame"),
            "leaderboard": ("leaderboard_enabled", "leaderboard_channel", "Leaderboard"),
            "level_up_announcements": ("level_up_announcements_enabled", "level_up_announcements_channel", "Level-up announcements"),
            "verification": ("verification_enabled", "verification_channel", "Verification"),
            "certificate": ("certificate_enabled", "certificate_channel", "Certificate generation"),
        }
        entry = fields.get(option)
        if not entry:
            return None, "Unknown feature. Use one of the feature names shown in the panel."
        enabled_field, channel_field, label = entry
        setattr(config, enabled_field, False)
        if channel_field:
            setattr(config, channel_field, None)
        if option == "hall_of_fame":
            config.hall_of_fame_channel = None
            config.hall_of_fame_role_name = None
            config.hall_of_fame_announcement_channel = None
            config.hall_of_fame_warning_channel = None
        db.commit()
        return label, None
    finally:
        db.close()


def _set_main_leaderboard_role_worker(guild_id, role_id):
    db = SessionLocal()
    try:
        config = db.query(GuildConfig).filter_by(guild_id=str(guild_id)).first()
        if not config or not get_leaderboard_channel_id(config):
            return False
        config.main_leaderboard_role_ids = str(role_id) if role_id else None
        db.commit()
        return True
    finally:
        db.close()


def _upsert_custom_leaderboard_worker(guild_id, channel_id, name, role_id):
    db = SessionLocal()
    try:
        lb = db.query(CustomLeaderboard).filter_by(channel_id=str(channel_id)).first()
        if not lb:
            lb = CustomLeaderboard(channel_id=str(channel_id), guild_id=str(guild_id), name=name)
            db.add(lb)
        else:
            lb.name = name
        lb.required_role_id = str(role_id) if role_id else None
        db.commit()
    finally:
        db.close()


def _remove_custom_leaderboard_worker(guild_id, channel_id):
    db = SessionLocal()
    try:
        lb = db.query(CustomLeaderboard).filter_by(
            guild_id=str(guild_id), channel_id=str(channel_id)
        ).first()
        if not lb:
            return False
        db.delete(lb)
        db.commit()
        return True
    finally:
        db.close()


def _feature_test_config_worker(guild_id, option):
    db = SessionLocal()
    try:
        config = db.query(GuildConfig).filter_by(guild_id=str(guild_id)).first()
        fields = {
            "bot_logs": ("bot_logs_enabled", "bot_logs_channel", "Bot logs"),
            "cve_and_news": ("cve_and_news_enabled", "cve_and_news_channel", "CVE and News"),
            "leaderboard": ("leaderboard_enabled", "leaderboard_channel", "Leaderboard"),
            "level_up_announcements": ("level_up_announcements_enabled", "level_up_announcements_channel", "Level-up announcements"),
            "quote_of_day": ("quote_of_day_enabled", "quote_of_day_channel", "Quote of the Day"),
        }
        entry = fields.get(option)
        if not entry:
            return None
        enabled_field, channel_field, label = entry
        if not config or not getattr(config, enabled_field) or not getattr(config, channel_field):
            return label, None
        return label, str(getattr(config, channel_field))
    finally:
        db.close()


class CustomLBCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="setting", description="Admin: open the bot feature settings panel")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def setting(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="Bot Settings Panel",
            description=(
                "Use the buttons below to configure features.\n\n"
                "**Enable Feature** — enable Bot Logs, CVE/News, Quote of the Day, Support, Attendance, Welcome, Leaderboard, "
                "Level-Up Announcements, or Verification by selecting the feature and channel/category from lists.\n"
                "**Disable Feature** — disable an enabled feature without changing unrelated settings.\n"
                "**Enable Certification** — choose the required role and certificate channel.\n"
                "**Enable Hall of Fame** — choose the role, announcement channel, and warning channel.\n"
                "**Leaderboard Settings** — set the main qualifying role or add/remove custom leaderboards."
            ),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, view=SettingsPanelView(self), ephemeral=True)

    async def enable_feature_settings(self, interaction, option, channel):
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send("This feature requires a text channel.", ephemeral=True)
            return
        label, error = await run_db(_enable_feature_worker, interaction.guild_id, option, channel.id)
        if error:
            await interaction.followup.send(error, ephemeral=True)
            return
        if option == "attendance":
            attendance_cog = self.bot.get_cog("AttendanceCog")
            if attendance_cog:
                try:
                    await attendance_cog.ensure_dashboard(interaction.guild, channel.id)
                except Exception:
                    logging.exception("Attendance enabled but dashboard creation failed for guild %s", interaction.guild_id)
        await interaction.followup.send(f"{label} enabled for {channel.mention}.", ephemeral=True)

    async def disable_feature_settings(self, interaction, option):
        label, error = await run_db(_disable_feature_worker, interaction.guild_id, option)
        await interaction.followup.send(error or f"{label} disabled.", ephemeral=True)

    async def test_feature_settings(self, interaction, option):
        result = await run_db(_feature_test_config_worker, interaction.guild_id, option)
        if not result:
            await interaction.followup.send("That feature cannot be tested from this panel.", ephemeral=True)
            return
        label, channel_id = result
        if not channel_id:
            await interaction.followup.send(f"{label} is not enabled for this server yet.", ephemeral=True)
            return
        channel = interaction.guild.get_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(f"The configured {label} channel could not be found.", ephemeral=True)
            return
        try:
            if option == "level_up_announcements":
                import sys
                sys.path.append(os.path.join(os.path.dirname(__file__), "..", "utils"))
                from rankcard import generate_levelup_card  # type: ignore
                card_file = await generate_levelup_card(interaction.user, 100, max_level=None, previous_level=99)
                await channel.send(f"✅ Level-up announcement test for {interaction.user.mention}", file=card_file)
            elif option == "quote_of_day":
                embed = discord.Embed(
                    title="Quote of the Day",
                    description="“The best way to predict the future is to invent it.”",
                    color=discord.Color.teal(),
                )
                embed.set_footer(text="Alan Kay")
                await channel.send(embed=embed)
            else:
                await channel.send(f"✅ {label} test message from {interaction.user.mention}")
        except discord.Forbidden:
            await interaction.followup.send(f"I do not have permission to send messages in {channel.mention}.", ephemeral=True)
            return
        except discord.HTTPException:
            logging.exception("Failed to send %s test to channel %s", label, channel.id)
            await interaction.followup.send(f"Could not send the {label} test message.", ephemeral=True)
            return
        await interaction.followup.send(f"{label} test sent to {channel.mention}.", ephemeral=True)

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

    async def enable_certification(self, interaction: discord.Interaction, role: discord.Role, channel: discord.TextChannel):
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

    async def enable_feature(
        self,
        interaction: discord.Interaction,
        option: app_commands.Choice[str],
        channel: discord.abc.GuildChannel,
    ):
        if option.value == "support":
            if not isinstance(channel, discord.CategoryChannel):
                await interaction.response.send_message("Support must be enabled with a category.", ephemeral=True)
                return
            from cogs.support import SupportRoleModal
            await interaction.response.send_modal(SupportRoleModal(self.bot, channel))
            return
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
                await interaction.followup.send("Use /setting and choose Enable Hall of Fame.", ephemeral=True)
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

    async def enable_hall_of_fame(
        self,
        interaction: discord.Interaction,
        role_name: discord.Role,
        announcement_channel: discord.TextChannel,
        warning_channel: discord.TextChannel,
    ):
        # Acknowledge the modal submission before database work so Discord
        # does not expire the interaction while configuration is being saved.
        await interaction.response.defer(ephemeral=True)
        db = SessionLocal()
        try:
            config = db.query(GuildConfig).filter_by(guild_id=str(interaction.guild_id)).first()
            if not config:
                config = GuildConfig(guild_id=str(interaction.guild_id))
                db.add(config)

            if not isinstance(announcement_channel, discord.TextChannel):
                await interaction.followup.send("Announcement channel must be a text channel.", ephemeral=True)
                return
            if not isinstance(warning_channel, discord.TextChannel):
                await interaction.followup.send("Warning channel must be a text channel.", ephemeral=True)
                return

            config.hall_of_fame_enabled = True
            config.hall_of_fame_channel = str(announcement_channel.id)
            config.hall_of_fame_role_name = role_name.name
            config.hall_of_fame_announcement_channel = str(announcement_channel.id)
            config.hall_of_fame_warning_channel = str(warning_channel.id)
            db.commit()
            await interaction.followup.send(
                f"Hall of Fame enabled. Role: {config.hall_of_fame_role_name}",
                ephemeral=True,
            )
        finally:
            db.close()

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
                config.support_admin_role = None
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
                        max_level=None,
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
