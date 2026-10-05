"""Send all card types for one random real member, then exit.

Run from the project root with: ./venv/bin/python scripts/send_test_cards.py
It uses BOT_TOKEN from .env and an explicit ``--guild-id`` target. Channels
come from that guild's own configuration.
"""
from __future__ import annotations

import asyncio
import argparse
import os
import random
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "utils"))

from database import GuildConfig, SessionLocal, UserData  # noqa: E402
from rankcard import generate_levelup_card, generate_rank_card  # noqa: E402
from welcomecard import generate_welcome_card  # noqa: E402


def get_card_data(guild_id: int, user_id: int) -> tuple[int, int, int, int, int, str | None]:
    """Read existing XP/config data without creating or modifying user records."""
    db = SessionLocal()
    try:
        config = db.query(GuildConfig).filter_by(guild_id=str(guild_id)).first()
        user = db.query(UserData).filter_by(guild_id=str(guild_id), user_id=user_id).first()
        xp = user.xp if user else 0
        level = user.level if user else 1
        daily_xp = user.daily_xp_earned if user else 0
        rank = (
            db.query(UserData).filter(UserData.guild_id == str(guild_id), UserData.xp > xp).count()
            + db.query(UserData).filter(
                UserData.guild_id == str(guild_id), UserData.xp == xp, UserData.user_id < user_id
            ).count() + 1
        )
        return level, xp, rank, daily_xp, config.daily_xp_limit if config else 100, config.leveling_channel if config else None
    finally:
        db.close()


class CardSender(discord.Client):
    def __init__(self, guild_id: int, **kwargs):
        super().__init__(**kwargs)
        self.target_guild_id = guild_id

    async def on_ready(self) -> None:
        try:
            guild_id = self.target_guild_id
            guild = self.get_guild(guild_id)
            if not guild:
                raise RuntimeError(f"Guild {guild_id} is not available to this bot.")
            members = [member async for member in guild.fetch_members(limit=None) if not member.bot]
            if not members:
                raise RuntimeError("The guild has no non-bot members.")

            config = SessionLocal()
            try:
                guild_config = config.query(GuildConfig).filter_by(guild_id=str(guild.id)).first()
                welcome_channel_id = guild_config.welcome_channel if guild_config else None
            finally:
                config.close()
            welcome_channel = guild.get_channel(int(welcome_channel_id)) if welcome_channel_id else None
            if not isinstance(welcome_channel, discord.abc.Messageable):
                raise RuntimeError("This guild has no configured welcome channel.")

            for member in random.sample(members, k=1):
                level, xp, rank, daily_xp, daily_limit, configured_level_channel = await asyncio.to_thread(get_card_data, guild.id, member.id)
                level_channel_id = int(configured_level_channel) if configured_level_channel and configured_level_channel.isdigit() else None
                level_channel = guild.get_channel(level_channel_id) if level_channel_id else None
                if not isinstance(level_channel, discord.abc.Messageable):
                    print(f"{member.display_name}: level/rank channel {level_channel_id} was not found; skipping those cards.")
                    level_channel = None

                cards = (
                    ("Welcome Card", welcome_channel, lambda: generate_welcome_card(member)),
                    ("Level Up Card", level_channel, lambda: generate_levelup_card(member, level, previous_level=max(1, level - 1))),
                    ("Rank Card", level_channel, lambda: generate_rank_card(member, level, xp, rank, daily_xp, daily_limit, None, guild.name)),
                )
                for card_type, channel, generate in cards:
                    if channel is None:
                        continue
                    try:
                        await channel.send(content=f"{member.display_name} — {card_type}", file=await generate())
                    except Exception as error:
                        print(f"{member.display_name} {card_type} failed: {error}")
        except Exception as error:
            print(f"Test-card sender failed: {error}")
        finally:
            await self.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Send test cards using one guild's configured channels.")
    parser.add_argument("--guild-id", type=int, required=True, help="Guild whose configured channels to use")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN must be set in .env.")
    intents = discord.Intents.none()
    intents.guilds = True
    intents.members = True
    CardSender(args.guild_id, intents=intents).run(token)


if __name__ == "__main__":
    main()
