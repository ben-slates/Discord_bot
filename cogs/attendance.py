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


def _attendance_channel_config(guild_id: int):
    db = SessionLocal()
    try:
        config = db.query(GuildConfig).filter_by(guild_id=str(guild_id)).first()
        if not config or not config.attendance_enabled or not config.attendance_channel:
            return None
        return str(config.attendance_channel)
    finally:
        db.close()


def _attendance_stats_worker(guild_id: int, today: str):
    db = SessionLocal()
    try:
        return db.query(AttendanceLog).filter_by(guild_id=str(guild_id), date=today).count()
    finally:
        db.close()


def _today_attendance_worker(guild_id: int, today: str):
    db = SessionLocal()
    try:
        return [row.user_id for row in db.query(AttendanceLog).filter_by(guild_id=str(guild_id), date=today).all()]
    finally:
        db.close()


def _user_attendance_worker(guild_id: int, user_id: int, dates: list[str]):
    db = SessionLocal()
    try:
        logs = db.query(AttendanceLog).filter(
            AttendanceLog.guild_id == str(guild_id),
            AttendanceLog.user_id == str(user_id),
        ).all()
        present_dates = {row.date for row in logs}
        return len(logs), present_dates
    finally:
        db.close()


def _month_attendance_worker(guild_id: int, month: str):
    db = SessionLocal()
    try:
        logs = db.query(AttendanceLog).filter(
            AttendanceLog.guild_id == str(guild_id),
            AttendanceLog.date.like(f"{month}-%"),
        ).all()
        return len(logs), len({row.date for row in logs})
    finally:
        db.close()


def _export_attendance_worker(guild_id: int, month=None, day=None, user_id=None):
    db = SessionLocal()
    try:
        query = db.query(AttendanceLog).filter_by(guild_id=str(guild_id))
        if user_id:
            query = query.filter_by(user_id=str(user_id))
        if day:
            query = query.filter_by(date=day)
        elif month:
            query = query.filter(AttendanceLog.date.like(f"{month}-%"))
        return [(row.user_id, row.date, row.timestamp.isoformat()) for row in query.all()]
    finally:
        db.close()


class AttendanceDashboardView(discord.ui.View):
    """Persistent public attendance actions for the configured channel."""

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Stats", style=discord.ButtonStyle.primary, custom_id="attendance_stats")
    async def stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.cog.send_stats(interaction)

    @discord.ui.button(label="Today", style=discord.ButtonStyle.secondary, custom_id="attendance_today")
    async def today(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.cog.send_today(interaction)

    @discord.ui.button(label="My Record", style=discord.ButtonStyle.secondary, custom_id="attendance_user")
    async def user(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.cog.send_user_record(interaction, interaction.user)


class AttendanceMonthModal(discord.ui.Modal, title="Monthly Attendance"):
    def __init__(self, cog):
        super().__init__()
        self.cog = cog
        self.month = discord.ui.TextInput(label="Month", placeholder="YYYY-MM (leave blank for current month)", required=False, max_length=7)
        self.add_item(self.month)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        await self.cog.send_month(interaction, self.month.value.strip() or None)


class AttendanceExportModal(discord.ui.Modal):
    def __init__(self, cog, mode):
        title = "Export Attendance by Month" if mode == "month" else "Export Attendance by Day"
        super().__init__(title=title)
        self.cog = cog
        self.mode = mode
        self.value = discord.ui.TextInput(
            label="Month (YYYY-MM)" if mode == "month" else "Day (YYYY-MM-DD)",
            placeholder="2026-09" if mode == "month" else "2026-09-17",
            required=True,
            max_length=10,
        )
        self.add_item(self.value)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        await self.cog.send_export(
            interaction,
            month=self.value.value.strip() if self.mode == "month" else None,
            day=self.value.value.strip() if self.mode == "day" else None,
        )


class AttendanceAdminSelectView(discord.ui.View):
    def __init__(self, cog, action, guild=None):
        super().__init__(timeout=300)
        self.cog = cog
        self.action = action
        if action == "status":
            admins = [member for member in guild.members if not member.bot and member.guild_permissions.administrator]
            self.members = discord.ui.Select(
                placeholder="Select an administrator",
                options=[discord.SelectOption(label=member.display_name[:100], value=str(member.id)) for member in admins[:25]]
                or [discord.SelectOption(label="No administrators found", value="0")],
            )
        else:
            self.members = discord.ui.UserSelect(placeholder="Select a user")
        self.add_item(self.members)
        self.members.callback = self._selected

    async def _selected(self, interaction):
        if self.action == "status":
            member = interaction.guild.get_member(int(self.members.values[0])) if self.members.values and self.members.values[0] != "0" else None
        else:
            member = self.members.values[0] if self.members.values else None
        if self.action == "status" and (not isinstance(member, discord.Member) or not member.guild_permissions.administrator):
            await interaction.response.send_message("Select a member with Administrator permission.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        if self.action == "status":
            await self.cog.send_check_status(interaction, member)
        else:
            await self.cog.send_export(interaction, user=member)


class AttendanceExportView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=300)
        self.cog = cog

    @discord.ui.button(label="Month", style=discord.ButtonStyle.primary)
    async def month(self, interaction, button):
        await interaction.response.send_modal(AttendanceExportModal(self.cog, "month"))

    @discord.ui.button(label="Day", style=discord.ButtonStyle.primary)
    async def day(self, interaction, button):
        await interaction.response.send_modal(AttendanceExportModal(self.cog, "day"))

    @discord.ui.button(label="User", style=discord.ButtonStyle.secondary)
    async def user(self, interaction, button):
        await interaction.response.send_message("Select the user whose attendance you want to export:", view=AttendanceAdminSelectView(self.cog, "export"), ephemeral=True)


class AttendanceSettingsView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=300)
        self.cog = cog

    @discord.ui.button(label="Activity", style=discord.ButtonStyle.primary)
    async def activity(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        await self.cog.send_activity(interaction)

    @discord.ui.button(label="Check Status", style=discord.ButtonStyle.secondary)
    async def check_status(self, interaction, button):
        await interaction.response.send_message("Select an administrator to check:", view=AttendanceAdminSelectView(self.cog, "status", interaction.guild), ephemeral=True)

    @discord.ui.button(label="Export", style=discord.ButtonStyle.success)
    async def export(self, interaction, button):
        await interaction.response.send_message("Choose the attendance data to export:", view=AttendanceExportView(self.cog), ephemeral=True)

    @discord.ui.button(label="Month", style=discord.ButtonStyle.secondary)
    async def month(self, interaction, button):
        await interaction.response.send_modal(AttendanceMonthModal(self.cog))

class AttendanceCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # in-memory cache: guild_id (str) -> set of user ids who sent a message today
        self._message_activity = defaultdict(set)
        self.daily_cleanup.start()
        self.dashboard_check.start()
        self.bot.add_view(AttendanceDashboardView(self))

    def cog_unload(self):
        self.daily_cleanup.cancel()
        self.dashboard_check.cancel()

    @tasks.loop(minutes=1)
    async def dashboard_check(self):
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            channel_id = await run_db(_attendance_channel_config, guild.id)
            if not channel_id:
                continue
            await self.ensure_dashboard(guild, channel_id)

    async def ensure_dashboard(self, guild, channel_id):
        """Create the public attendance panel when it is missing."""
        try:
            channel = guild.get_channel(int(channel_id))
        except (TypeError, ValueError):
            return False
        if not isinstance(channel, discord.TextChannel):
            return False
        try:
            async for message in channel.history(limit=10):
                if message.author == self.bot.user and message.embeds and message.embeds[0].title == "Attendance Dashboard":
                    return True
            embed = discord.Embed(
                title="Attendance Dashboard",
                description="View today’s attendance, server stats, or your own attendance record.",
                color=discord.Color.blurple(),
            )
            await channel.send(embed=embed, view=AttendanceDashboardView(self))
            logging.info("Attendance dashboard sent to channel %s in guild %s", channel.id, guild.id)
            return True
        except discord.HTTPException:
            logging.exception("Could not maintain attendance dashboard in channel %s", channel.id)
            return False

    @dashboard_check.before_loop
    async def before_dashboard_check(self):
        await self.bot.wait_until_ready()

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

    async def _require_attendance_channel(self, interaction):
        channel_id = await run_db(_attendance_channel_config, interaction.guild_id)
        if not channel_id:
            await interaction.followup.send("Attendance is not enabled for this server.", ephemeral=True)
            return False
        if str(interaction.channel_id) != channel_id:
            await interaction.followup.send(f"This action can only be used in <#{channel_id}>.", ephemeral=True)
            return False
        return True

    async def send_stats(self, interaction):
        if not await self._require_attendance_channel(interaction):
            return
        today = datetime.datetime.now(PKT).strftime('%Y-%m-%d')
        present_count = await run_db(_attendance_stats_worker, interaction.guild_id, today)
        total_members = sum(1 for member in interaction.guild.members if not member.bot)
        absent_count = max(0, total_members - present_count)
        attendance_percentage = (present_count / total_members * 100) if total_members else 0
        embed = discord.Embed(title=f"Server Attendance Stats - {today}", color=discord.Color.gold(), timestamp=datetime.datetime.now(PKT))
        embed.add_field(name="Total Members (Non-Bot)", value=str(total_members), inline=False)
        embed.add_field(name="Present Today", value=str(present_count), inline=True)
        embed.add_field(name="Absent Today", value=str(absent_count), inline=True)
        embed.add_field(name="Attendance Rate", value=f"{attendance_percentage:.1f}%", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def send_today(self, interaction):
        if not await self._require_attendance_channel(interaction):
            return
        today = datetime.datetime.now(PKT).strftime('%Y-%m-%d')
        user_ids = await run_db(_today_attendance_worker, interaction.guild_id, today)
        embed = discord.Embed(title=f"Attendance for Today ({today})", color=discord.Color.green(), timestamp=datetime.datetime.now(PKT))
        if not user_ids:
            embed.description = "No one has been marked present today yet."
        else:
            names = []
            for index, user_id in enumerate(user_ids[:50], start=1):
                member = interaction.guild.get_member(int(user_id))
                names.append(f"{index}. {member.display_name if member else f'Unknown ({user_id})'}")
            embed.description = "\n".join(names) + (f"\n\n*...and {len(user_ids) - 50} more*" if len(user_ids) > 50 else "")
        embed.set_footer(text=f"Total Present: {len(user_ids)}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def send_user_record(self, interaction, target):
        if not await self._require_attendance_channel(interaction):
            return
        current_time = datetime.datetime.now(PKT)
        dates = [(current_time - datetime.timedelta(days=offset)).strftime('%Y-%m-%d') for offset in range(7)]
        total, present_dates = await run_db(_user_attendance_worker, interaction.guild_id, target.id, dates)
        if total == 0:
            await interaction.followup.send("No data found for this user.", ephemeral=True)
            return
        member = interaction.guild.get_member(target.id)
        display_name = member.display_name if member else getattr(target, "name", f"Unknown ({target.id})")
        embed = discord.Embed(title=f"Attendance Record: {display_name}", color=discord.Color.blue())
        embed.add_field(name="Total Presents", value=str(total), inline=True)
        embed.add_field(name="Last 7 Days", value="\n".join(f"{date}: {'Present' if date in present_dates else 'Absent'}" for date in dates), inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def send_month(self, interaction, month):
        if not await self._require_attendance_channel(interaction):
            return
        month = month or datetime.datetime.now(PKT).strftime('%Y-%m')
        try:
            datetime.datetime.strptime(month, '%Y-%m')
        except ValueError:
            await interaction.followup.send("Invalid format. Please use YYYY-MM.", ephemeral=True)
            return
        total, days = await run_db(_month_attendance_worker, interaction.guild_id, month)
        embed = discord.Embed(title=f"Monthly Summary: {month}", color=discord.Color.purple())
        if not days:
            embed.description = "No attendance data found for this month."
        else:
            embed.add_field(name="Days Tracked", value=str(days), inline=True)
            embed.add_field(name="Total Presents (All Users)", value=str(total), inline=True)
            embed.add_field(name="Avg Daily Attendance", value=f"{total / days:.1f}", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def send_activity(self, interaction):
        if not await self._require_attendance_channel(interaction):
            return
        msg_members = [interaction.guild.get_member(uid) for uid in list(self._message_activity.get(str(interaction.guild_id), []))[:25]]
        messages = "\n".join(f"{member.display_name} ({member.id})" for member in msg_members if member) or "None"
        voice = [f"{member.display_name} in #{channel.name}" for channel in interaction.guild.voice_channels for member in channel.members if not member.bot]
        embed = discord.Embed(title="Attendance Activity (in-memory)", color=discord.Color.blurple())
        embed.add_field(name="Message Activity (last 25, since restart)", value=messages, inline=False)
        embed.add_field(name="Voice Participants (now)", value="\n".join(voice[:50]) or "None", inline=False)
        embed.set_footer(text="In-memory data resets on bot restart or daily cleanup")
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def send_check_status(self, interaction, member):
        if not await self._require_attendance_channel(interaction):
            return
        if not isinstance(member, discord.Member) or not member.guild_permissions.administrator:
            await interaction.followup.send("The selected member must have the Administrator permission.", ephemeral=True)
            return
        now = datetime.datetime.now(datetime.timezone.utc)
        month_start = now.astimezone(PKT).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        rows = await run_db(_get_admin_month_status, interaction.guild_id, member.id, month_start, now)
        present_days = sum(row[2] for row in rows)
        details = "\n".join(f"{date}: {hours:.2f}h — {'Present' if present else 'Absent'}" for date, hours, present in rows) or "No days tracked yet."
        embed = discord.Embed(title=f"Administrator Status: {member.display_name}", description=details, color=discord.Color.green() if present_days else discord.Color.orange())
        embed.set_footer(text=f"{present_days}/{len(rows)} days present (minimum 8 hours online or dnd)")
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def send_export(self, interaction, month=None, day=None, user=None):
        if not await self._require_attendance_channel(interaction):
            return
        if month:
            try: datetime.datetime.strptime(month, '%Y-%m')
            except ValueError:
                await interaction.followup.send("Invalid month format. Please use YYYY-MM.", ephemeral=True); return
        if day:
            try: datetime.datetime.strptime(day, '%Y-%m-%d')
            except ValueError:
                await interaction.followup.send("Invalid day format. Please use YYYY-MM-DD.", ephemeral=True); return
        rows = await run_db(_export_attendance_worker, interaction.guild_id, month, day, user.id if user else None)
        output = io.StringIO(); writer = csv.writer(output); writer.writerow(["User ID", "Username", "Date", "Timestamp"])
        for user_id, date, timestamp in rows:
            member = interaction.guild.get_member(int(user_id))
            writer.writerow([user_id, member.name if member else "Unknown", date, timestamp])
        file = discord.File(io.BytesIO(output.getvalue().encode()), filename=f"attendance_export_{interaction.guild_id}.csv")
        await interaction.followup.send(f"Here is the attendance data ({len(rows)} records):", file=file, ephemeral=True)


    @app_commands.command(name="setting-attendance", description="Admin: open attendance settings")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def setting_attendance(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        channel_id = await run_db(_attendance_channel_config, interaction.guild_id)
        if not channel_id:
            await interaction.followup.send("Attendance is not enabled for this server.", ephemeral=True)
            return
        if str(interaction.channel_id) != channel_id:
            await interaction.followup.send(f"This command can only be used in <#{channel_id}>.", ephemeral=True)
            return
        embed = discord.Embed(
            title="Attendance Settings",
            description=(
                "**Activity** — current message and voice activity.\n"
                "**Check Status** — an administrator’s 8-hour active-status attendance.\n"
                "**Export** — download attendance CSV by month, day, or user.\n"
                "**Month** — monthly attendance summary."
            ),
            color=discord.Color.blurple(),
        )
        await interaction.followup.send(embed=embed, view=AttendanceSettingsView(self), ephemeral=True)

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
