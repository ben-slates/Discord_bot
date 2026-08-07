"""Set one Discord member's level and XP using the configured bot and database."""

import asyncio
import os
import sys

import argparse
import asyncio
import os
import sys
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

DEFAULT_USERNAME = "asfandyar.deb"
DEFAULT_LEVEL = 30


def normalized(value: str | None) -> str:
    return (value or "").strip().casefold()


def set_level_in_db(user_id: int, target_level: int):
    from database import SessionLocal, UserData
    from cogs.leveling import calculate_required_xp

    db = SessionLocal()
    try:
        user = db.query(UserData).filter_by(user_id=int(user_id)).first()
        if user is None:
            user = UserData(user_id=int(user_id))
            db.add(user)

        user.level = int(target_level)
        user.xp = calculate_required_xp(target_level - 1)
        db.commit()
        print(f"Updated {user_id} to level {user.level} with {user.xp} XP.")
        return True
    finally:
        db.close()


async def run_discord_lookup(username: str, target_level: int):
    import discord

    token = os.getenv("BOT_TOKEN")
    server_id = os.getenv("SERVER_ID")
    if not token or not server_id:
        raise RuntimeError("BOT_TOKEN and SERVER_ID must be set in .env to resolve username")

    intents = discord.Intents.none()
    intents.guilds = True
    intents.members = True
    client = discord.Client(intents=intents)
    print(f"Connecting to Discord to find @{username}...", flush=True)

    @client.event
    async def on_ready():
        try:
            print("Connected. Looking up member and updating the database...", flush=True)
            guild = client.get_guild(int(server_id))
            if guild is None:
                raise RuntimeError(f"Bot cannot access configured server {server_id}")

            target = normalized(username)
            matches = []
            candidates = []
            for member in guild.members:
                n_name = normalized(member.name)
                n_display = normalized(member.display_name)
                n_tag = normalized(f"{member.name}#{member.discriminator}")
                if target == n_name or target == n_display or target == n_tag:
                    matches.append(member)
                if target in n_name or target in n_display or target in n_tag:
                    candidates.append(member)

            if len(matches) == 0 and candidates:
                matches = candidates

            if len(matches) != 1:
                names = ", ".join(f"{m.display_name} (id={m.id})" for m in matches) or "none"
                raise RuntimeError(
                    f"Expected one member matching {username!r}; found {len(matches)}. Candidates: {names}"
                )

            member = matches[0]
            set_level_in_db(member.id, target_level)
        finally:
            await client.close()

    await client.start(os.getenv("BOT_TOKEN"))


def main():
    parser = argparse.ArgumentParser(description="Set one Discord member's level and XP using the configured bot and database.")
    parser.add_argument("--user-id", type=int, help="Discord user id to update directly")
    parser.add_argument("--username", type=str, help="Username (or partial) to resolve via the bot")
    parser.add_argument("--level", type=int, default=DEFAULT_LEVEL, help="Target level to set (default 30)")
    args = parser.parse_args()

    if args.user_id:
        try:
            set_level_in_db(args.user_id, args.level)
            return
        except Exception as e:
            print(f"Failed to set level by id: {e}", file=sys.stderr)
            raise SystemExit(1)

    username = args.username or DEFAULT_USERNAME
    try:
        asyncio.run(run_discord_lookup(username, args.level))
    except Exception as error:
        print(f"Level update failed: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
