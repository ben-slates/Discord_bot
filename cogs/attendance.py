import discord
from discord.ext import commands, tasks
from discord import app_commands
import datetime
import io
import csv
from database import SessionLocal, GuildConfig, UserData, AttendanceLog, HallOfFameEntry

class AttendanceCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.daily_cleanup.start()

    def cog_unload(self):
        self.daily_cleanup.cancel()

    @tasks.loop(time=datetime.time(hour=12, minute=0, tzinfo=datetime.timezone(datetime.timedelta(hours=5))))
    async def daily_cleanup(self):
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

    @commands.Cog.listener()
    async def on_presence_update(self, before: discord.Member, after: discord.Member):
        # Auto-mark attendance if a user comes online (is active for even 1 second)
        if after.bot or str(after.status) == "offline":
            return
        import asyncio
        await asyncio.to_thread(self._mark_presence_attendance, after.guild.id, after.id)

    def _mark_message_attendance(self, guild_id: int, author_id: int):
        db = SessionLocal()
        try:
            config = db.query(GuildConfig).filter_by(guild_id=str(guild_id)).first()
            if not config or not config.attendance_enabled:
                return
            
            today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5))).strftime('%Y-%m-%d')
            log = db.query(AttendanceLog).filter_by(guild_id=str(guild_id), user_id=str(author_id), date=today).first()
            if not log:
                new_log = AttendanceLog(guild_id=str(guild_id), user_id=str(author_id), date=today)
                db.add(new_log)
                db.commit()
        finally:
            db.close()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        import asyncio
        await asyncio.to_thread(self._mark_message_attendance, message.guild.id, message.author.id)


    @app_commands.command(name="stats", description="Shows overall attendance statistics")
    async def stats(self, interaction: discord.Interaction):
        if interaction.channel_id != 1529889568172544170:
            await interaction.response.send_message("This command can only be used in <#1529889568172544170>.", ephemeral=True)
            return
        db = SessionLocal()
        try:
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
    async def export(self, interaction: discord.Interaction, month: str = None, day: str = None, user: discord.Member = None):
        if interaction.channel_id != 1529889568172544170:
            await interaction.response.send_message("This command can only be used in <#1529889568172544170>.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        db = SessionLocal()
        try:
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
        if interaction.channel_id != 1529889568172544170:
            await interaction.response.send_message("This command can only be used in <#1529889568172544170>.", ephemeral=True)
            return
        db = SessionLocal()
        try:
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
    async def user(self, interaction: discord.Interaction, member: discord.Member = None):
        if interaction.channel_id != 1529889568172544170:
            await interaction.response.send_message("This command can only be used in <#1529889568172544170>.", ephemeral=True)
            return
        target = member or interaction.user
        target_id_str = str(target.id)
        
        db = SessionLocal()
        try:
            total_presents = db.query(AttendanceLog).filter_by(guild_id=str(interaction.guild_id), user_id=target_id_str).count()
            if total_presents == 0:
                await interaction.response.send_message("No data found for this user.", ephemeral=True)
                return
            
            embed = discord.Embed(
                title=f"Attendance Record: {target.display_name}",
                color=discord.Color.blue()
            )
            
            embed.add_field(name="Username", value=target.name, inline=True)
            embed.add_field(name="Total Presents", value=str(total_presents), inline=True)
            
            # Calculate recent attendance
            recent_days = []
            current_time = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5)))
            
            for i in range(7):
                date_to_check = (current_time - datetime.timedelta(days=i)).strftime('%Y-%m-%d')
                log = db.query(AttendanceLog).filter_by(guild_id=str(interaction.guild_id), user_id=target_id_str, date=date_to_check).first()
                
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
        if interaction.channel_id != 1529889568172544170:
            await interaction.response.send_message("This command can only be used in <#1529889568172544170>.", ephemeral=True)
            return
        if not month_str:
            month_str = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5))).strftime('%Y-%m')
            
        try:
            # Validate format
            datetime.datetime.strptime(month_str, '%Y-%m')
        except ValueError:
            await interaction.response.send_message("Invalid format. Please use YYYY-MM.", ephemeral=True)
            return
            
        db = SessionLocal()
        try:
            logs = db.query(AttendanceLog).filter_by(guild_id=str(interaction.guild_id)).filter(AttendanceLog.date.like(f"{month_str}-%")).all()
            
            # Count unique days tracked in this month
            unique_days = set(log.date for log in logs)
            days_in_month = len(unique_days)
            total_presents = len(logs)
            
            embed = discord.Embed(
                title=f"Monthly Summary: {month_str}",
                color=discord.Color.purple()
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

async def setup(bot):
    await bot.add_cog(AttendanceCog(bot))
