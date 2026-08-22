import discord
from discord.ext import commands, tasks
from discord import app_commands
import datetime
import asyncio
import logging
from zoneinfo import ZoneInfo
from database import SessionLocal, GuildConfig, UserData, AttendanceLog, CustomLeaderboard
from utils.db_executor import run_db
from utils.leaderboard import (
    get_channel_members,
    get_leaderboard_channel_id,
    get_main_leaderboard_role_ids,
    should_include_member_for_custom_leaderboard,
    should_include_member_for_main_leaderboard,
)

PKT = ZoneInfo("Asia/Karachi")
DAILY_SUMMARY_TIMES = [
    datetime.time(hour=8, minute=0, tzinfo=PKT),
    datetime.time(hour=23, minute=50, tzinfo=PKT),
]


def _next_leaderboard_run(now_utc: datetime.datetime | None = None) -> datetime.datetime:
    """Return the next 08:00 Asia/Karachi run as a timezone-aware UTC time."""
    now_utc = now_utc or datetime.datetime.now(datetime.timezone.utc)
    now_pkt = now_utc.astimezone(PKT)
    next_run = now_pkt.replace(hour=8, minute=0, second=0, microsecond=0)
    if next_run <= now_pkt:
        next_run += datetime.timedelta(days=1)
    return next_run.astimezone(datetime.timezone.utc)


class NotificationsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        if not self.daily_summary.is_running():
            self.daily_summary.start()

    def cog_unload(self):
        self.daily_summary.cancel()

    @tasks.loop(time=DAILY_SUMMARY_TIMES)
    async def daily_summary(self):
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        now_pkt = now_utc.astimezone(PKT)
        logging.info(
            "Leaderboard task triggered: utc=%s pakistan=%s",
            now_utc.isoformat(), now_pkt.isoformat(),
        )
        try:
            await self._run_daily_summary(now_pkt)
        except Exception:
            # A failed run must not terminate the fixed-time scheduler.
            logging.exception("Daily notification task failed; scheduler remains active")

    async def _run_daily_summary(self, now: datetime.datetime):
        current_time_str = now.strftime("%H:%M")
        # Load configs in a thread to avoid blocking the event loop
        configs = await run_db(_fetch_all_configs)
        for config in configs:
                guild = self.bot.get_guild(int(config.guild_id))
                if not guild:
                    continue
                
                # Morning Leaderboard at 08:00
                if current_time_str == "08:00" and config.leaderboard_enabled:
                    leaderboard_channel_id = get_leaderboard_channel_id(config)
                    if leaderboard_channel_id:
                        channel = guild.get_channel(int(leaderboard_channel_id))
                    else:
                        channel = None
                    if channel:
                        permissions = channel.permissions_for(guild.me)
                        if not permissions.send_messages or not permissions.embed_links:
                            logging.warning(
                                "Leaderboard channel %s is missing send_messages or embed_links for the bot",
                                channel.id,
                            )
                        # Delete the previous leaderboard message to keep the channel clean
                        try:
                            async for msg in channel.history(limit=50):
                                if msg.author == self.bot.user and msg.embeds:
                                    if msg.embeds[0].title == "Morning Leaderboard Update":
                                        await msg.delete()
                                        break
                        except Exception as delete_err:
                            print(f"Failed to delete old leaderboard: {delete_err}")

                        logging.info("Generating leaderboard for guild %s", guild.id)
                        embed = discord.Embed(title="Morning Leaderboard Update", description="Top 10 users by total XP.", color=discord.Color.gold())
                        allowed_role_ids = await run_db(_get_main_lb_role_ids, config.guild_id)
                        top_users = []
                        # Fetch top users from DB in thread
                        user_rows = await run_db(_fetch_top_users, 1000)
                        for user in user_rows:
                            member = guild.get_member(int(user['user_id']))
                            if not member or member.bot:
                                continue
                            if not should_include_member_for_main_leaderboard(member, allowed_role_ids):
                                continue
                            top_users.append(user)
                            if len(top_users) >= 10:
                                break
                        
                        if not top_users:
                            embed.description = "No XP data yet!"
                        else:
                            for index, u in enumerate(top_users, start=1):
                                member = guild.get_member(int(u['user_id']))
                                name = member.display_name if member else f"Unknown ({u['user_id']})"
                                prefix = f"#{index}"
                                embed.add_field(
                                    name=f"{prefix} {name}",
                                    value=f"Level {u['level']}\n{u['xp']} XP",
                                    inline=False
                                )
                        
                        logging.info("Sending leaderboard to channel %s in guild %s", channel.id, guild.id)
                        try:
                            await channel.send(embed=embed)
                        except discord.HTTPException:
                            logging.exception("Failed to send leaderboard to channel %s", channel.id)
                        else:
                            logging.info("Leaderboard sent successfully to channel %s", channel.id)
                    else:
                        logging.warning(
                            "Configured leaderboard channel %s was not found in guild %s",
                            leaderboard_channel_id, guild.id,
                        )
                
                # Nightly Attendance at 23:50
                if current_time_str == "23:50" and config.attendance_enabled and config.attendance_channel:
                    channel = guild.get_channel(int(config.attendance_channel))
                    if channel:
                        today = now.strftime('%Y-%m-%d')
                        embed = discord.Embed(title=f"🌙 Nightly Attendance Log - {today}", color=discord.Color.purple())
                        
                        total_members = sum(1 for m in guild.members if not m.bot)
                        total_today = await run_db(_count_attendance, config.guild_id, today)
                        absent = max(0, total_members - total_today)
                        rate = round((total_today / total_members * 100), 1) if total_members > 0 else 0.0
                        
                        att_text = (
                            f"**Total Members (Non-Bot):** {total_members}\n"
                            f"**Present Today:** {total_today}\n"
                            f"**Absent Today:** {absent}\n"
                            f"**Attendance Rate:** {rate}%"
                        )
                        embed.description = att_text
                        try:
                            await channel.send(embed=embed)
                        except discord.Forbidden:
                            pass
                
                # Custom Leaderboards at 08:00
                if current_time_str == "08:00":
                    custom_lbs = await run_db(_fetch_custom_lbs, config.guild_id)
                    for lb in custom_lbs:
                        channel = guild.get_channel(int(lb.channel_id))
                        if not channel: continue

                        # Delete previous custom leaderboard message to keep channel clean
                        try:
                            async for msg in channel.history(limit=50):
                                if msg.author == self.bot.user and msg.embeds:
                                    if msg.embeds[0].title == f"Top 3 {lb.name}":
                                        await msg.delete()
                                        break
                        except Exception as delete_err:
                            print(f"Failed to delete old custom leaderboard in {lb.channel_id}: {delete_err}")
                        
                        scores = []
                        all_users = await run_db(_fetch_all_users, config.guild_id)
                        user_map = {int(u['user_id']): u for u in all_users}

                        att_logs = await run_db(_fetch_att_logs, config.guild_id)
                        att_map = {}
                        for log in att_logs:
                            uid = int(log['user_id'])
                            att_map[uid] = att_map.get(uid, 0) + 1

                        members = await get_channel_members(channel)
                        for member in members:
                            if member.bot:
                                continue
                            
                            if not should_include_member_for_custom_leaderboard(member, required_role_ids=[lb.required_role_id] if lb.required_role_id else []):
                                continue
                            
                            user = user_map.get(member.id)
                            if not user: continue
                            att_count = att_map.get(member.id, 0)
                            score = user['xp'] + (att_count * 50)
                            scores.append((member, score, user['level'], user['xp'], att_count))
                        
                        scores.sort(key=lambda x: x[1], reverse=True)
                        top_3 = scores[:3]
                        
                        embed = discord.Embed(title=f"Top 3 {lb.name}", description="Ranked by XP and Attendance", color=discord.Color.blue())
                        if not top_3:
                            embed.description = "No data yet!"
                        else:
                            for idx, (member, score, level, xp, att_count) in enumerate(top_3, 1):
                                embed.add_field(
                                    name=f"#{idx} {member.display_name}",
                                    value=f"Level {level} | {xp} XP | {att_count} Days\nCombined Score: {score}",
                                    inline=False
                                )
                        try:
                            await channel.send(embed=embed)
                        except discord.Forbidden:
                            pass
        # end of configs loop

    @daily_summary.before_loop
    async def before_daily_summary(self):
        await self.bot.wait_until_ready()
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        now_pkt = now_utc.astimezone(PKT)
        next_run_utc = _next_leaderboard_run(now_utc)
        logging.info(
            "Leaderboard scheduler started: current_utc=%s current_pakistan=%s "
            "next_leaderboard_run=%s (%s)",
            now_utc.isoformat(), now_pkt.isoformat(),
            next_run_utc.isoformat(), next_run_utc.astimezone(PKT).isoformat(),
        )

async def setup(bot):
    await bot.add_cog(NotificationsCog(bot))


# Thread helpers
def _fetch_all_configs():
    db = SessionLocal()
    try:
        return db.query(GuildConfig).all()
    finally:
        db.close()

def _fetch_top_users(limit=1000):
    db = SessionLocal()
    try:
        rows = db.query(UserData).order_by(UserData.xp.desc()).limit(limit).all()
        return [{'user_id': r.user_id, 'xp': r.xp, 'level': r.level} for r in rows]
    finally:
        db.close()

def _count_attendance(guild_id, date_str):
    db = SessionLocal()
    try:
        return db.query(AttendanceLog).filter_by(guild_id=str(guild_id), date=date_str).count()
    finally:
        db.close()

def _fetch_custom_lbs(guild_id):
    db = SessionLocal()
    try:
        return db.query(CustomLeaderboard).filter_by(guild_id=str(guild_id)).all()
    finally:
        db.close()

def _fetch_all_users(guild_id):
    db = SessionLocal()
    try:
        rows = db.query(UserData).all()
        return [{'user_id': r.user_id, 'xp': r.xp, 'level': r.level} for r in rows]
    finally:
        db.close()

def _fetch_att_logs(guild_id):
    db = SessionLocal()
    try:
        rows = db.query(AttendanceLog).filter_by(guild_id=str(guild_id)).all()
        return [{'user_id': r.user_id, 'date': r.date} for r in rows]
    finally:
        db.close()

def _get_main_lb_role_ids(guild_id):
    db = SessionLocal()
    try:
        return get_main_leaderboard_role_ids(db, guild_id)
    finally:
        db.close()
