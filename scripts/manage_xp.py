#!/usr/bin/env python3
"""Admin script to adjust user levels/xp and enforce daily XP cap.

Usage examples:
  # Set user by Discord ID to level 30 (xp auto-calculated)
  python scripts/manage_xp.py --user-id 123456789012345678 --set-level 30

  # Set user by username (requires BOT_TOKEN + SERVER_ID in .env)
  python scripts/manage_xp.py --username "aghga asfand" --set-level 30

  # Enforce daily XP cap across all users (caps daily_xp_earned to 100)
  python scripts/manage_xp.py --enforce-cap

"""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv
import argparse
from sqlalchemy import text

env_path = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(dotenv_path=env_path)

from database import SessionLocal, UserData, GuildConfig


def calculate_required_xp(level: int) -> int:
    if level < 1:
        return 0
    return (20 * (level ** 2)) + (100 * level) + 250


def xp_for_level(level: int) -> int:
    # Return the minimum total XP that corresponds to `level`.
    if level <= 1:
        return 0
    return calculate_required_xp(level - 1)


def set_user_level_by_id(user_id: int, target_level: int, xp_value: int | None = None):
    db = SessionLocal()
    try:
        user = db.query(UserData).filter_by(user_id=int(user_id)).first()
        if not user:
            print(f"User {user_id} not found in database.")
            return False

        new_xp = xp_value if xp_value is not None else xp_for_level(target_level)
        user.level = int(target_level)
        user.xp = int(new_xp)
        db.commit()
        print(f"Updated user {user_id}: level={user.level}, xp={user.xp}")
        return True
    finally:
        db.close()


def find_user_id_by_name(bot_token: str, guild_id: str, name_query: str) -> int | None:
    import requests

    headers = {"Authorization": f"Bot {bot_token}"}
    url = f"https://discord.com/api/v10/guilds/{guild_id}/members?limit=1000"
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"Failed to fetch guild members: {e}")
        return None

    members = resp.json()
    q = name_query.lower()
    for m in members:
        username = m.get("user", {}).get("username", "").lower()
        nick = (m.get("nick") or "").lower()
        display = nick or username
        if q in display:
            return int(m["user"]["id"])
    return None


def enforce_daily_cap(max_cap: int = 100):
    db = SessionLocal()
    try:
        updated = 0
        for user in db.query(UserData).filter(UserData.daily_xp_earned != None).all():
            try:
                if getattr(user, "daily_xp_earned", 0) is None:
                    continue
                if user.daily_xp_earned > max_cap:
                    print(f"Capping user {user.user_id} daily_xp_earned {user.daily_xp_earned} -> {max_cap}")
                    user.daily_xp_earned = max_cap
                    db.add(user)
                    updated += 1
            except Exception:
                continue
        db.commit()
        print(f"Enforced daily cap. Rows updated: {updated}")
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description="Manage user XP and levels")
    parser.add_argument("--user-id", type=int, help="Discord user id to target")
    parser.add_argument("--username", type=str, help="Partial username/display name to search (requires BOT_TOKEN + SERVER_ID in .env)")
    parser.add_argument("--set-level", type=int, help="Set target level for the user")
    parser.add_argument("--xp", type=int, help="Optional explicit XP value to set")
    parser.add_argument("--enforce-cap", action="store_true", help="Enforce daily XP cap (default 100) across all users")
    parser.add_argument("--cap-value", type=int, default=100, help="Cap value to enforce for daily_xp_earned (default: 100)")

    args = parser.parse_args()

    if args.enforce_cap:
        enforce_daily_cap(args.cap_value)

    if args.set_level:
        target_level = args.set_level
        xp_value = args.xp
        uid = args.user_id
        if not uid and args.username:
            BOT_TOKEN = os.getenv("BOT_TOKEN")
            GUILD_ID = os.getenv("SERVER_ID")
            if not BOT_TOKEN or not GUILD_ID:
                print("To resolve username to id you must set BOT_TOKEN and SERVER_ID in .env")
                sys.exit(1)
            uid = find_user_id_by_name(BOT_TOKEN, GUILD_ID, args.username)
            if not uid:
                print("No member matched the provided username query.")
                sys.exit(1)

        if not uid:
            print("No user specified. Use --user-id or --username plus --set-level")
            sys.exit(1)

        set_user_level_by_id(uid, target_level, xp_value)


if __name__ == "__main__":
    main()
