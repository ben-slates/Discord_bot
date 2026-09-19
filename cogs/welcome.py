"""Public welcome cards plus default one-time direct-message onboarding."""

import asyncio
import logging
import os
import sys

import discord
from discord.ext import commands, tasks

from database import GuildConfig, SessionLocal, WelcomeDMDelivery
from utils.db_executor import run_db


RULES_CHANNEL_ID = 1519028752145842339
INTRODUCTIONS_CHANNEL_ID = 1519251723850612766

def _welcome_channel_config(guild_id: int):
    db = SessionLocal()
    try:
        config = db.query(GuildConfig).filter_by(guild_id=str(guild_id)).first()
        if not config or not config.welcome_enabled or not config.welcome_channel:
            return None
        return str(config.welcome_channel)
    finally:
        db.close()


def _members_without_onboarding(guild_id: int, member_ids: list[int]):
    if not member_ids:
        return []
    db = SessionLocal()
    try:
        delivered = {
            row[0]
            for row in db.query(WelcomeDMDelivery.user_id).filter(
                WelcomeDMDelivery.guild_id == str(guild_id),
                WelcomeDMDelivery.user_id.in_([str(member_id) for member_id in member_ids]),
            ).all()
        }
        return [member_id for member_id in member_ids if str(member_id) not in delivered]
    finally:
        db.close()


def _mark_onboarding_delivered(guild_id: int, member_ids: list[int]):
    db = SessionLocal()
    try:
        for member_id in member_ids:
            if not db.query(WelcomeDMDelivery.id).filter_by(guild_id=str(guild_id), user_id=str(member_id)).first():
                db.add(WelcomeDMDelivery(guild_id=str(guild_id), user_id=str(member_id)))
        db.commit()
    finally:
        db.close()


def _clear_onboarding_delivery(guild_id: int, member_id: int):
    """Allow a member who leaves and later rejoins to receive onboarding again."""
    db = SessionLocal()
    try:
        db.query(WelcomeDMDelivery).filter_by(
            guild_id=str(guild_id), user_id=str(member_id)
        ).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


class WelcomeCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.startup_onboarding.start()

    def cog_unload(self):
        self.startup_onboarding.cancel()

    @tasks.loop(count=1)
    async def startup_onboarding(self):
        """Onboard existing members once after the bot starts or is added."""
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            await self._onboard_guild_members(guild)

    @startup_onboarding.before_loop
    async def before_startup_onboarding(self):
        await self.bot.wait_until_ready()

    def _guide_channels(self, guild: discord.Guild):
        # Prefer each server's own channel names so onboarding works without
        # manual IDs when the bot is added elsewhere. The existing Rynex IDs
        # remain only as a fallback for legacy channel names.
        rules = discord.utils.find(
            lambda channel: any(term in channel.name.lower() for term in ("rule", "guideline", "start-here")),
            guild.text_channels,
        ) or guild.get_channel(RULES_CHANNEL_ID)
        introductions = discord.utils.find(
            lambda channel: any(term in channel.name.lower() for term in ("intro", "introduction", "introductions", "start-here")),
            guild.text_channels,
        ) or guild.get_channel(INTRODUCTIONS_CHANNEL_ID)
        return rules, introductions

    def _onboarding_message(self, guild: discord.Guild, member: discord.Member) -> str:
        rules, introductions = self._guide_channels(guild)
        rules_text = rules.mention if rules else "the server rules channel"
        intro_text = introductions.mention if introductions else "the introductions channel"
        return (
            f"Welcome to **{guild.name}**, {member.mention}!\n\n"
            f"Please start by reading {rules_text}, then introduce yourself in {intro_text}.\n"
            "Be respectful, protect members’ privacy, and do not share harmful or unauthorized content. "
            "Explore the community channels, join your team discussions, and use the Support Dashboard whenever you need help.\n\n"
            "We’re glad to have you here."
        )

    async def _onboard_guild_members(self, guild: discord.Guild, members=None):
        members = members or [member for member in guild.members if not member.bot]
        member_map = {member.id: member for member in members if not member.bot}
        pending_ids = await run_db(_members_without_onboarding, guild.id, list(member_map))
        if not pending_ids:
            return

        semaphore = asyncio.Semaphore(3)

        async def send_one(member):
            async with semaphore:
                try:
                    await member.send(self._onboarding_message(guild, member))
                except discord.Forbidden:
                    logging.info("Cannot DM onboarding message to %s in guild %s", member.id, guild.id)
                except discord.HTTPException:
                    logging.exception("Failed to DM onboarding message to %s in guild %s", member.id, guild.id)
                # Record both successful and blocked DMs: a restart must not
                # repeatedly retry members who have DMs disabled.
                return member.id

        attempted = await asyncio.gather(*(send_one(member_map[member_id]) for member_id in pending_ids))
        await run_db(_mark_onboarding_delivered, guild.id, attempted)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self._onboard_guild_members(guild)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        await self._onboard_guild_members(member.guild, [member])

        # Preserve the optional existing public welcome-card feature.
        channel_id = await run_db(_welcome_channel_config, member.guild.id)
        if not channel_id:
            return
        channel = member.guild.get_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            sys.path.append(os.path.join(os.path.dirname(__file__), "..", "utils"))
            from welcomecard import generate_welcome_card  # type: ignore
            file = await generate_welcome_card(member)
            rules, introductions = self._guide_channels(member.guild)
            rules_text = rules.mention if rules else "the server rules channel"
            intro_text = introductions.mention if introductions else "the introductions channel"
            await channel.send(
                f"Welcome to **{member.guild.name}**, **{member.mention}**!\n\n"
                f"Read {rules_text}, introduce yourself in {intro_text}, and join the discussion.\n"
                "Welcome to the community.",
                file=file,
            )
        except Exception:
            logging.exception("Failed to send public welcome card for member %s", member.id)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if not member.bot:
            await run_db(_clear_onboarding_delivery, member.guild.id, member.id)


async def setup(bot):
    await bot.add_cog(WelcomeCog(bot))
