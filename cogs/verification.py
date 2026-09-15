"""Verification commands backed by the existing feature configuration."""

import re
import secrets
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy.exc import IntegrityError

from database import SessionLocal, GuildConfig, VerificationRecord
from utils.db_executor import run_db


EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _get_config(guild_id):
    db = SessionLocal()
    try:
        config = db.query(GuildConfig).filter_by(guild_id=str(guild_id)).first()
        if not config or not config.verification_enabled or not config.verification_channel:
            return None
        return str(config.verification_channel)
    finally:
        db.close()


def _create_or_get_record(user_id: int, email: str):
    db = SessionLocal()
    try:
        existing = db.query(VerificationRecord).filter_by(discord_user_id=int(user_id)).first()
        if existing:
            return existing.verification_id, False

        for _ in range(10):
            verification_id = f"0x{secrets.token_hex(4).upper()}"
            record = VerificationRecord(
                discord_user_id=int(user_id),
                verification_id=verification_id,
                email=email,
            )
            db.add(record)
            try:
                db.commit()
                return verification_id, True
            except IntegrityError:
                db.rollback()
                # A concurrent request may have registered this user, or the
                # random ID may have collided. Check the user before retrying.
                existing = db.query(VerificationRecord).filter_by(discord_user_id=int(user_id)).first()
                if existing:
                    return existing.verification_id, False
        raise RuntimeError("Could not allocate a unique verification ID")
    finally:
        db.close()


def _get_record(user_id: int):
    db = SessionLocal()
    try:
        record = db.query(VerificationRecord).filter_by(discord_user_id=int(user_id)).first()
        if not record:
            return None
        return record.verification_id
    finally:
        db.close()


def _list_records():
    db = SessionLocal()
    try:
        records = db.query(VerificationRecord).order_by(VerificationRecord.created_at.asc()).all()
        return [(r.verification_id, r.discord_user_id, r.email) for r in records]
    finally:
        db.close()


class VerificationEmailModal(discord.ui.Modal, title="Verify — Email"):
    def __init__(self, cog):
        super().__init__()
        self.cog = cog
        self.email = discord.ui.TextInput(
            label="Email address",
            placeholder="student@example.com",
            required=True,
            max_length=254,
        )
        self.add_item(self.email)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.cog._register_email(interaction, self.email.value)


class VerificationDashboardView(discord.ui.View):
    """Persistent public dashboard used instead of requiring slash commands."""

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.primary, custom_id="verification_verify")
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VerificationEmailModal(self.cog))

    @discord.ui.button(label="Check UUID", style=discord.ButtonStyle.secondary, custom_id="verification_check_uuid")
    async def check_uuid_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.check_uuid_interaction(interaction)


class VerificationCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.dashboard_check.start()
        self.bot.add_view(VerificationDashboardView(self))

    def cog_unload(self):
        self.dashboard_check.cancel()

    @tasks.loop(minutes=1)
    async def dashboard_check(self):
        await self.bot.wait_until_ready()
        try:
            enabled_channels = await run_db(self._get_enabled_channel)
        except Exception:
            logging.exception("Could not load verification dashboard configuration")
            return
        for guild in self.bot.guilds:
            channel_id = enabled_channels.get(guild.id)
            if not channel_id:
                continue
            try:
                channel = guild.get_channel(int(channel_id))
            except (TypeError, ValueError):
                logging.warning("Invalid verification channel %r for guild %s", channel_id, guild.id)
                continue
            if not isinstance(channel, discord.TextChannel):
                continue
            try:
                found = False
                async for message in channel.history(limit=100):
                    if message.author == self.bot.user and message.embeds and message.embeds[0].title == "Verification Dashboard":
                        found = True
                        break
                if not found:
                    embed = discord.Embed(
                        title="Verification Dashboard",
                        description="Verify your account or view your existing Verification ID using the buttons below.",
                        color=discord.Color.blurple(),
                    )
                    await channel.send(embed=embed, view=VerificationDashboardView(self))
                    logging.info("Verification dashboard sent to channel %s in guild %s", channel.id, guild.id)
            except discord.HTTPException:
                logging.exception("Could not maintain verification dashboard in channel %s", channel.id)

    @dashboard_check.before_loop
    async def before_dashboard_check(self):
        await self.bot.wait_until_ready()

    @staticmethod
    def _get_enabled_channel():
        db = SessionLocal()
        try:
            rows = db.query(GuildConfig).filter(
                GuildConfig.verification_enabled.is_(True),
                GuildConfig.verification_channel.isnot(None),
            ).all()
            return {int(row.guild_id): str(row.verification_channel) for row in rows}
        finally:
            db.close()

    async def _configured_channel(self, interaction):
        if not interaction.guild_id or not interaction.channel:
            return None
        channel_id = await run_db(_get_config, interaction.guild_id)
        if not channel_id or str(interaction.channel.id) != channel_id:
            return None
        return channel_id

    async def _require_channel(self, interaction):
        if await self._configured_channel(interaction):
            return True
        await interaction.followup.send(
            "Verification is disabled or this is not the configured verification channel.",
            ephemeral=True,
        )
        return False

    async def _register_email(self, interaction: discord.Interaction, email: str):
        if not await self._require_channel(interaction):
            return
        email = email.strip().lower()
        if not EMAIL_RE.fullmatch(email):
            await interaction.followup.send("Please provide a valid email address.", ephemeral=True)
            return
        try:
            verification_id, created = await run_db(_create_or_get_record, interaction.user.id, email)
        except Exception:
            await interaction.followup.send("Verification could not be completed right now. Please try again.", ephemeral=True)
            return
        if not created:
            await interaction.followup.send(
                "You already have a permanent Verification ID. Use the **Check UUID** button to view it.",
                ephemeral=True,
            )
            return
        message = f"Verification successful.\n\nYour Verification ID:\n`{verification_id}`\n\nKeep this ID safe. It is permanently assigned to your account."
        await interaction.followup.send(message, ephemeral=True)

    async def check_uuid_interaction(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not await self._require_channel(interaction):
            return
        verification_id = await run_db(_get_record, interaction.user.id)
        if verification_id:
            await interaction.followup.send(f"Your Verification ID:\n```{verification_id}```", ephemeral=True)
        else:
            await interaction.followup.send("You are not registered yet. Use the **Verify** button and enter your email first.", ephemeral=True)

    @app_commands.command(name="verify-list", description="Admin: view verification records")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def verify_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not await self._require_channel(interaction):
            return
        records = await run_db(_list_records)
        if not records:
            await interaction.followup.send("No verification records found.", ephemeral=True)
            return
        lines = ["Verification ID | Discord User | Email"]
        for verification_id, user_id, email in records:
            member = interaction.guild.get_member(int(user_id)) if interaction.guild else None
            display = member.display_name if member else str(user_id)
            lines.append(f"{verification_id} | {display} | {email}")
        # Discord limits each message to 2,000 characters. Split at complete
        # records so every verification entry is shown, while keeping every
        # page ephemeral for the administrator who requested it.
        pages = []
        current = []
        current_length = len("```text\n\n```")
        for line in lines:
            if current and current_length + len(line) + 1 > 1900:
                pages.append("\n".join(current))
                current = []
                current_length = len("```text\n\n```")
            current.append(line)
            current_length += len(line) + 1
        if current:
            pages.append("\n".join(current))

        for page in pages:
            await interaction.followup.send(f"```text\n{page}\n```", ephemeral=True)


async def setup(bot):
    await bot.add_cog(VerificationCog(bot))
