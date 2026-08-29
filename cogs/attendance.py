import discord
from discord.ext import commands, tasks
from discord import app_commands
import datetime
import logging
import time
from collections import defaultdict
import asyncio
import io
import csv
from zoneinfo import ZoneInfo
from database import SessionLocal, GuildConfig, UserData, AttendanceLog, HallOfFameEntry, AdminPresenceInterval
from utils.db_executor import run_db, run_db_profiled
from utils.diag import instrument_async

PKT = ZoneInfo("Asia/Karachi")
ACTIVE_ADMIN_STATUSES = {"online", "dnd"}

class AttendanceCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # in-memory cache: guild_id (str) -> set of user ids who sent a message today
        self._message_activity = defaultdict(set)
        self.daily_cleanup.start()

    def cog_unload(self):
        self.daily_cleanup.cancel()

    @tasks.loop(time=datetime.time(hour=12, minute=0, tzinfo=datetime.timezone(datetime.timedelta(hours=5))))
    async def daily_cleanup(self):
        # Run cleanup in a thread to avoid blocking the event loop
        await run_db(self._daily_cleanup_worker)

    def _daily_cleanup_worker(self):
        # Clear today's in-memory message activity at the start of the daily cleanup
        try:
            self._message_activity.clear()
        except Exception:
            pass

        for guild in self.bot.guilds:
            db = SessionLocal()
            try:
                current_member_ids = {member.id for member in guild.members if not member.bot}
                current_member_id_strings = {str(member_id) for member_id in current_member_ids}

                attendance_logs = db.query(AttendanceLog).filter_by(guild_id=str(guild.id)).all()
                for log in attendance_logs:
                    if str(log.user_id) not in current_member_id_strings:
                        db.delete(log)

                hall_of_fame_entries = db.query(HallOfFameEntry).filter_by(guild_id=str(guild.id)).all()
                for entry in hall_of_fame_entries:
                    if str(entry.user_id) not in current_member_id_strings:
                        db.delete(entry)

                existing_user_ids = {str(user_id[0]) for user_id in db.query(UserData.user_id).all()}
                for user_id in existing_user_ids:
                    if user_id not in current_member_id_strings:
                        user_record = db.query(UserData).filter_by(user_id=int(user_id)).first()
                        if user_record:
                            db.delete(user_record)

                db.commit()
            finally:
                db.close()

    @daily_cleanup.before_loop
    async def before_daily_cleanup(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        pass
            
    def _mark_presence_attendance(self, guild_id: int, user_id: int):
        db = SessionLocal()
        try:
            config = db.query(GuildConfig).filter_by(guild_id=str(guild_id)).first()
            if not config or not config.attendance_enabled:
                return
                
            today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5))).strftime('%Y-%m-%d')
            log = db.query(AttendanceLog).filter_by(guild_id=str(guild_id), user_id=str(user_id), date=today).first()
            
            if not log:
                new_log = AttendanceLog(guild_id=str(guild_id), user_id=str(user_id), date=today)
                db.add(new_log)
                db.commit()
        finally:
            db.close()

    def _attendance_exists(self, guild_id: int, user_id: int) -> bool:
        db = SessionLocal()
        try:
            today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5))).strftime('%Y-%m-%d')
            log = db.query(AttendanceLog).filter_by(guild_id=str(guild_id), user_id=str(user_id), date=today).first()
            return bool(log)
        finally:
            db.close()

    @commands.Cog.listener()
    async def on_presence_update(self, before: discord.Member, after: discord.Member):
        if after.bot or not after.guild:
            return
        status = str(after.status)
        before_is_admin = before.guild_permissions.administrator
        after_is_admin = after.guild_permissions.administrator
        if not after_is_admin:
            status = "offline"
        if (
            before_is_admin == after_is_admin
            and str(before.status) == str(after.status)
            and status in ACTIVE_ADMIN_STATUSES
        ):
            return
        await run_db(_record_admin_presence_transition, after.guild.id, after.id, status)

    def _mark_message_attendance(self, guild_id: int, author_id: int):
        db = SessionLocal()
        try:
            today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5))).strftime('%Y-%m-%d')
            # The prior implementation did two sequential remote SELECTs. This
            # outer join preserves the result while making a normal message one query.
            config, log = db.query(GuildConfig, AttendanceLog).outerjoin(
                AttendanceLog,
                (AttendanceLog.guild_id == str(guild_id)) &
                (AttendanceLog.user_id == str(author_id)) &
                (AttendanceLog.date == today),
            ).filter(GuildConfig.guild_id == str(guild_id)).first() or (None, None)
            if not config or not config.attendance_enabled:
                return
            if not log:
                new_log = AttendanceLog(guild_id=str(guild_id), user_id=str(author_id), date=today)
                db.add(new_log)
                db.commit()
        finally:
            db.close()

    @commands.Cog.listener()
    @instrument_async(threshold=0.2)
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        # Record in-memory that this user sent a message today for quick presence checks
        try:
            self._message_activity[str(message.guild.id)].add(message.author.id)
        except Exception:
            pass

        started = time.perf_counter()
        await run_db_profiled("attendance.message", self._mark_message_attendance, message.guild.id, message.author.id)
        elapsed = time.perf_counter() - started
        if elapsed >= 0.5:
            logging.warning("Attendance message DB timing: db_executor=%.3fs", elapsed)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        # Mark attendance when a user joins a voice channel (and they are not a bot)
        if member.bot or not member.guild:
            return

        # Only mark for active statuses (online or dnd)
        status = str(member.status)
        if status not in ("online", "dnd"):
            return

        # If they joined a voice channel (before was None and after is not None), mark attendance
        if before.channel is None and after.channel is not None:
            await run_db(self._mark_presence_attendance, member.guild.id, member.id)


    @app_commands.command(name="stats", description="Shows overall attendance statistics")
    async def stats(self, interaction: discord.Interaction):
        db = SessionLocal()
        try:
            config = db.query(GuildConfig).filter_by(guild_id=str(interaction.guild_id)).first()
            if not config or not config.attendance_enabled or not config.attendance_channel:
                await interaction.response.send_message("Attendance is not enabled for this server.", ephemeral=True)
                return
            if str(interaction.channel_id) != config.attendance_channel:
                await interaction.response.send_message(f"This command can only be used in <#{config.attendance_channel}>.", ephemeral=True)
                return
            
            total_members = sum(1 for m in interaction.guild.members if not m.bot)
            today_str = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5))).strftime('%Y-%m-%d')
            
            present_count = db.query(AttendanceLog).filter_by(guild_id=str(interaction.guild_id), date=today_str).count()
            absent_count = max(0, total_members - present_count)
            attendance_percentage = (present_count / total_members * 100) if total_members > 0 else 0
            
            embed = discord.Embed(
                title=f"Server Attendance Stats - {today_str}",
                color=discord.Color.gold(),
                timestamp=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5)))
            )
            
            embed.add_field(name="Total Members (Non-Bot)", value=str(total_members), inline=False)
            embed.add_field(name="Present Today", value=str(present_count), inline=True)
            embed.add_field(name="Absent Today", value=str(absent_count), inline=True)
            embed.add_field(name="Attendance Rate", value=f"{attendance_percentage:.1f}%", inline=True)
            
            await interaction.response.send_message(embed=embed)
        finally:
            db.close()

    @app_commands.command(name="export", description="Admin: Export server attendance to CSV")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        month="Format: YYYY-MM (e.g. 2024-05)",
        day="Format: YYYY-MM-DD (e.g. 2024-05-15)",
        user="Specific user to export"
    )
    async def export(self, interaction: discord.Interaction, month: str = None, day: str = None, user: discord.User = None):
        db = SessionLocal()
        try:
            config = db.query(GuildConfig).filter_by(guild_id=str(interaction.guild_id)).first()
            if not config or not config.attendance_enabled or not config.attendance_channel:
                await interaction.response.send_message("Attendance is not enabled for this server.", ephemeral=True)
                return
            if str(interaction.channel_id) != config.attendance_channel:
                await interaction.response.send_message(f"This command can only be used in <#{config.attendance_channel}>.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            query = db.query(AttendanceLog).filter_by(guild_id=str(interaction.guild_id))
            if user:
                query = query.filter_by(user_id=str(user.id))
            if day:
                query = query.filter_by(date=day)
            elif month:
                query = query.filter(AttendanceLog.date.like(f"{month}-%"))
                
            logs = query.all()
            
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["User ID", "Username", "Date", "Timestamp"])
            for log in logs:
                member = interaction.guild.get_member(int(log.user_id))
                username = member.name if member else "Unknown"
                writer.writerow([log.user_id, username, log.date, log.timestamp.isoformat()])
                
            output.seek(0)
            file = discord.File(io.BytesIO(output.getvalue().encode()), filename=f"attendance_export_{interaction.guild_id}.csv")
            await interaction.followup.send(f"Here is the attendance data ({len(logs)} records):", file=file)
        finally:
            db.close()

    @app_commands.command(name="today", description="Shows today's attendance")
    async def today(self, interaction: discord.Interaction):
        db = SessionLocal()
        try:
            config = db.query(GuildConfig).filter_by(guild_id=str(interaction.guild_id)).first()
            if not config or not config.attendance_enabled or not config.attendance_channel:
                await interaction.response.send_message("Attendance is not enabled for this server.", ephemeral=True)
                return
            if str(interaction.channel_id) != config.attendance_channel:
                await interaction.response.send_message(f"This command can only be used in <#{config.attendance_channel}>.", ephemeral=True)
                return
            today_str = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5))).strftime('%Y-%m-%d')
            logs = db.query(AttendanceLog).filter_by(guild_id=str(interaction.guild_id), date=today_str).all()
            
            embed = discord.Embed(
                title=f"Attendance for Today ({today_str})",
                color=discord.Color.green(),
                timestamp=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5)))
            )
            
            if not logs:
                embed.description = "No one has been marked present today yet."
            else:
                description = ""
                for i, log in enumerate(logs[:50]): # Display up to 50 to avoid embed limits
                    member = interaction.guild.get_member(int(log.user_id))
                    display_name = member.display_name if member else f"Unknown ({log.user_id})"
                    description += f"{i+1}. {display_name}\n"
                    
                if len(logs) > 50:
                    description += f"\n*...and {len(logs) - 50} more*"
                    
                embed.description = description
                
            embed.set_footer(text=f"Total Present: {len(logs)}")
            await interaction.response.send_message(embed=embed)
        finally:
            db.close()

    @app_commands.command(name="user", description="Shows attendance history of a member")
    async def user(self, interaction: discord.Interaction, member: discord.User = None):
        db = SessionLocal()
        try:
            config = db.query(GuildConfig).filter_by(guild_id=str(interaction.guild_id)).first()
            if not config or not config.attendance_enabled or not config.attendance_channel:
                await interaction.response.send_message("Attendance is not enabled for this server.", ephemeral=True)
                return
            if str(interaction.channel_id) != config.attendance_channel:
                await interaction.response.send_message(
                    f"This command can only be used in <#{config.attendance_channel}>",
                    ephemeral=True,
                )
                return

            target = member or interaction.user
            # If the provided user is not a Member (left/removed), attempt to resolve Member for display purposes
            member_obj = None
            try:
                member_obj = interaction.guild.get_member(int(target.id)) if interaction.guild else None
            except Exception:
                member_obj = None

            display_name = member_obj.display_name if member_obj else getattr(target, "name", f"Unknown ({target.id})")
            target_id_str = str(target.id)

            total_presents = db.query(AttendanceLog).filter_by(
                guild_id=str(interaction.guild_id),
                user_id=target_id_str,
            ).count()
            if total_presents == 0:
                await interaction.response.send_message("No data found for this user.", ephemeral=True)
                return

            embed = discord.Embed(
                title=f"Attendance Record: {display_name}",
                color=discord.Color.blue(),
            )
            embed.add_field(name="Username", value=getattr(target, "name", str(target.id)), inline=True)
            embed.add_field(name="Total Presents", value=str(total_presents), inline=True)

            recent_days = []
            current_time = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5)))

            for i in range(7):
                date_to_check = (current_time - datetime.timedelta(days=i)).strftime('%Y-%m-%d')
                log = db.query(AttendanceLog).filter_by(
                    guild_id=str(interaction.guild_id),
                    user_id=target_id_str,
                    date=date_to_check,
                ).first()

                if log:
                    recent_days.append(f"{date_to_check}: Present")
                else:
                    recent_days.append(f"{date_to_check}: Absent")

            embed.add_field(name="Last 7 Days", value="\n".join(recent_days), inline=False)
            await interaction.response.send_message(embed=embed)
        finally:
            db.close()

    @app_commands.command(name="month", description="Shows monthly attendance for a selected month (YYYY-MM)")
    @app_commands.describe(month_str="Format: YYYY-MM (e.g., 2024-05). Leave blank for current month.")
    async def month(self, interaction: discord.Interaction, month_str: str = None):
        db = SessionLocal()
        try:
            config = db.query(GuildConfig).filter_by(guild_id=str(interaction.guild_id)).first()
            if not config or not config.attendance_enabled or not config.attendance_channel:
                await interaction.response.send_message("Attendance is not enabled for this server.", ephemeral=True)
                return
            if str(interaction.channel_id) != config.attendance_channel:
                await interaction.response.send_message(
                    f"This command can only be used in <#{config.attendance_channel}>",
                    ephemeral=True,
                )
                return

            if not month_str:
                month_str = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5))).strftime('%Y-%m')

            try:
                datetime.datetime.strptime(month_str, '%Y-%m')
            except ValueError:
                await interaction.response.send_message("Invalid format. Please use YYYY-MM.", ephemeral=True)
                return

            logs = db.query(AttendanceLog).filter_by(guild_id=str(interaction.guild_id)).filter(
                AttendanceLog.date.like(f"{month_str}-%")
            ).all()

            unique_days = set(log.date for log in logs)
            days_in_month = len(unique_days)
            total_presents = len(logs)

            embed = discord.Embed(
                title=f"Monthly Summary: {month_str}",
                color=discord.Color.purple(),
            )

            if days_in_month == 0:
                embed.description = "No attendance data found for this month."
            else:
                avg_daily = total_presents / days_in_month
                embed.add_field(name="Days Tracked", value=str(days_in_month), inline=True)
                embed.add_field(name="Total Presents (All Users)", value=str(total_presents), inline=True)
                embed.add_field(name="Avg Daily Attendance", value=f"{avg_daily:.1f}", inline=True)

            await interaction.response.send_message(embed=embed)
        finally:
            db.close()

    @app_commands.command(name="activity", description="(Admin) View in-memory message activity and current voice participants")
    @app_commands.default_permissions(administrator=True)
    async def activity(self, interaction: discord.Interaction):
        db = SessionLocal()
        try:
            config = db.query(GuildConfig).filter_by(guild_id=str(interaction.guild_id)).first()
            if not config or not config.attendance_enabled or not config.attendance_channel:
                await interaction.response.send_message("Attendance is not enabled for this server.", ephemeral=True)
                return
            if str(interaction.channel_id) != config.attendance_channel:
                await interaction.response.send_message(f"This command can only be used in <#{config.attendance_channel}>.", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)

            gid = str(interaction.guild_id)
            msg_ids = list(self._message_activity.get(gid, []))[:25]
            members_from_msgs = []
            for uid in msg_ids:
                m = interaction.guild.get_member(int(uid))
                if m:
                    members_from_msgs.append(f"{m.display_name} ({m.id})")
                else:
                    members_from_msgs.append(f"Unknown ({uid})")

            # voice participants
            voice_members = []
            try:
                for vc in interaction.guild.voice_channels:
                    for m in vc.members:
                        if not m.bot:
                            voice_members.append(f"{m.display_name} in #{vc.name}")
            except Exception:
                pass

            embed = discord.Embed(title="Attendance Activity (in-memory)", color=discord.Color.blurple())
            embed.add_field(name="Message Activity (last 25, since restart)", value=("\n".join(members_from_msgs) if members_from_msgs else "None"), inline=False)
            embed.add_field(name="Voice Participants (now)", value=("\n".join(voice_members[:50]) if voice_members else "None"), inline=False)
            embed.set_footer(text="In-memory data resets on bot restart or daily cleanup")
            await interaction.followup.send(embed=embed)
        finally:
            db.close()

    @commands.Cog.listener()
    async def on_ready(self):
        # One-time automatic scan at startup: scan recent messages (25 per channel)
        # and current voice participants for each guild where attendance is enabled.
        if getattr(self, "_initial_scan_done", False):
            return
        self._initial_scan_done = True
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            # Only track administrator presence for guilds using attendance.
            config = await run_db(self._get_guild_config, guild.id)
            if not config or not config.attendance_enabled:
                continue
            now = datetime.datetime.now(datetime.timezone.utc)
            active_admin_ids = [
                member.id for member in guild.members
                if not member.bot and member.guild_permissions.administrator
                and str(member.status) in ACTIVE_ADMIN_STATUSES
            ]
            await run_db(_initialize_admin_presence, guild.id, active_admin_ids, now)
            marked = set()
            # Scan recent messages, but only mark messages from TODAY (timezone UTC+5)
            tz = datetime.timezone(datetime.timedelta(hours=5))
            today_str = datetime.datetime.now(tz).strftime('%Y-%m-%d')
            for channel in getattr(guild, 'text_channels', []):
                try:
                    perms = channel.permissions_for(guild.me)
                    if not (perms.view_channel and perms.read_message_history):
                        continue
                    async for msg in channel.history(limit=25):
                        if not msg.author or msg.author.bot or not msg.created_at:
                            continue
                        try:
                            msg_date = msg.created_at.astimezone(tz).strftime('%Y-%m-%d')
                        except Exception:
                            msg_date = msg.created_at.strftime('%Y-%m-%d')
                        # Only consider messages from today
                        if msg_date != today_str:
                            continue
                        await run_db(self._mark_message_attendance, guild.id, msg.author.id)
                        marked.add(msg.author.id)
                except Exception:
                    continue
                await asyncio.sleep(0.05)

            # Mark current voice participants
            try:
                for vc in guild.voice_channels:
                    for m in vc.members:
                        if m.bot:
                            continue
                        await run_db(self._mark_presence_attendance, guild.id, m.id)
                        marked.add(m.id)
            except Exception:
                pass

            # Update in-memory activity
            try:
                gid = str(guild.id)
                for uid in list(marked)[:1000]:
                    self._message_activity[gid].add(uid)
            except Exception:
                pass

    def _get_guild_config(self, guild_id: int):
        db = SessionLocal()
        try:
            return db.query(GuildConfig).filter_by(guild_id=str(guild_id)).first()
        finally:
            db.close()

    @app_commands.command(name="check-status", description="Admin: check an administrator's monthly active-status attendance")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(member="The administrator whose status should be checked")
    async def check_status(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)
        config = await run_db(self._get_guild_config, interaction.guild_id)
        if not config or not config.attendance_enabled or not config.attendance_channel:
            await interaction.followup.send("Attendance is not enabled for this server.", ephemeral=True)
            return
        if str(interaction.channel_id) != config.attendance_channel:
            await interaction.followup.send(f"This command can only be used in <#{config.attendance_channel}>.", ephemeral=True)
            return
        if not member.guild_permissions.administrator:
            await interaction.followup.send("The selected member must have the Administrator permission.", ephemeral=True)
            return
        now = datetime.datetime.now(datetime.timezone.utc)
        month_start = now.astimezone(PKT).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        rows = await run_db(_get_admin_month_status, interaction.guild_id, member.id, month_start, now)
        present_days = sum(row[2] for row in rows)
        details = "\n".join(
            f"{date}: {hours:.2f}h — {'Present' if present else 'Absent'}"
            for date, hours, present in rows
        ) or "No days tracked yet."
        embed = discord.Embed(
            title=f"Administrator Status: {member.display_name}",
            description=details,
            color=discord.Color.green() if present_days else discord.Color.orange(),
        )
        embed.set_footer(text=f"{present_days}/{len(rows)} days present (minimum 8 hours online or dnd)")
        await interaction.followup.send(embed=embed, ephemeral=True)


def _record_admin_presence_transition(guild_id, user_id, status):
    db = SessionLocal()
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        current = db.query(AdminPresenceInterval).filter_by(
            guild_id=str(guild_id), user_id=str(user_id), ended_at=None,
        ).order_by(AdminPresenceInterval.started_at.desc()).first()
        if status in ACTIVE_ADMIN_STATUSES:
            if current and current.status == status:
                return
            if current:
                current.ended_at = now
            db.add(AdminPresenceInterval(
                guild_id=str(guild_id), user_id=str(user_id), status=status,
                started_at=now,
            ))
        elif current:
            current.ended_at = now
        db.commit()
    finally:
        db.close()


def _initialize_admin_presence(guild_id, active_admin_ids, now):
    db = SessionLocal()
    try:
        # Close intervals across a bot restart so downtime is never counted.
        db.query(AdminPresenceInterval).filter_by(
            guild_id=str(guild_id), ended_at=None,
        ).update({AdminPresenceInterval.ended_at: now}, synchronize_session=False)
        for user_id in active_admin_ids:
            db.add(AdminPresenceInterval(
                guild_id=str(guild_id), user_id=str(user_id), status="online",
                started_at=now,
            ))
        db.commit()
    finally:
        db.close()


def _get_admin_month_status(guild_id, user_id, month_start, now):
    db = SessionLocal()
    try:
        intervals = db.query(AdminPresenceInterval).filter(
            AdminPresenceInterval.guild_id == str(guild_id),
            AdminPresenceInterval.user_id == str(user_id),
            AdminPresenceInterval.started_at < now,
            (AdminPresenceInterval.ended_at.is_(None) | (AdminPresenceInterval.ended_at > month_start)),
        ).all()
        local_now = now.astimezone(PKT)
        local_day = month_start.astimezone(PKT)
        result = []
        while local_day.date() <= local_now.date():
            day_start = local_day
            day_end = min(local_day + datetime.timedelta(days=1), local_now)
            seconds = 0.0
            for interval in intervals:
                interval_start = _as_utc(interval.started_at)
                interval_end = _as_utc(interval.ended_at) if interval.ended_at else now
                start = max(interval_start, day_start.astimezone(datetime.timezone.utc))
                end = min(interval_end, day_end.astimezone(datetime.timezone.utc))
                if end > start:
                    seconds += (end - start).total_seconds()
            result.append((local_day.strftime("%Y-%m-%d"), seconds / 3600, seconds >= 8 * 3600))
            local_day += datetime.timedelta(days=1)
        return result
    finally:
        db.close()


def _as_utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)

async def setup(bot):
    await bot.add_cog(AttendanceCog(bot))
