"""Verification commands backed by the existing feature configuration."""

import re
import secrets

import discord
from discord import app_commands
from discord.ext import commands
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


class VerificationCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

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
        await interaction.response.send_message(
            "Verification is disabled or this is not the configured verification channel.",
            ephemeral=True,
        )
        return False

    @app_commands.command(name="verifi", description="Register your email and receive a permanent verification ID")
    @app_commands.describe(email="Your email address")
    async def verifi(self, interaction: discord.Interaction, email: str):
        if not await self._require_channel(interaction):
            return
        email = email.strip().lower()
        if not EMAIL_RE.fullmatch(email):
            await interaction.response.send_message("Please provide a valid email address.", ephemeral=True)
            return
        try:
            verification_id, created = await run_db(_create_or_get_record, interaction.user.id, email)
        except Exception:
            await interaction.response.send_message("Verification could not be completed right now. Please try again.", ephemeral=True)
            return
        if not created:
            await interaction.response.send_message(
                "You already have a permanent Verification ID. Use `/verify` to view it.",
                ephemeral=True,
            )
            return
        message = f"Verification successful.\n\nYour Verification ID:\n`{verification_id}`\n\nKeep this ID safe. It is permanently assigned to your account."
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(name="verify", description="Show your existing verification ID")
    async def verify(self, interaction: discord.Interaction):
        if not await self._require_channel(interaction):
            return
        verification_id = await run_db(_get_record, interaction.user.id)
        if verification_id:
            await interaction.response.send_message(f"Your Verification ID:\n`{verification_id}`", ephemeral=True)
        else:
            await interaction.response.send_message("You are not registered yet. Use `/verifi` with your email first.", ephemeral=True)

    @app_commands.command(name="verify-list", description="Admin: view verification records")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def verify_list(self, interaction: discord.Interaction):
        records = await run_db(_list_records)
        if not records:
            await interaction.response.send_message("No verification records found.", ephemeral=True)
            return
        lines = ["Verification ID | Discord User | Email"]
        for verification_id, user_id, email in records:
            member = interaction.guild.get_member(int(user_id)) if interaction.guild else None
            display = member.display_name if member else str(user_id)
            lines.append(f"{verification_id} | {display} | {email}")
        content = "\n".join(lines)
        if len(content) > 1900:
            content = content[:1890] + "\n... (list truncated)"
        await interaction.response.send_message(f"```text\n{content}\n```", ephemeral=True)


async def setup(bot):
    await bot.add_cog(VerificationCog(bot))
