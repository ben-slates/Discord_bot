import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import time
import datetime
from database import SessionLocal, GuildConfig, UserData, CustomLeaderboard, AttendanceLog
import sys
import os
from utils.leaderboard import (
    get_leaderboard_channel_id,
    get_main_leaderboard_role_ids,
    should_include_member_for_custom_leaderboard,
    should_include_member_for_main_leaderboard,
)

DEFAULT_LEVELING_CHANNEL_ID = 1519264254178623488

def calculate_required_xp(level: int) -> int:
    if level < 1:
        return 0
    return (20 * (level**2)) + (100 * level) + 250

def calculate_level(total_xp: int, max_level: int = 100) -> int:
    if total_xp < 0:
        return 1
    level = 1
    while level < max_level and total_xp >= calculate_required_xp(level):
        level += 1
    return min(level, max_level)

class LevelingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.voice_xp_loop.start()

    def cog_unload(self):
        self.voice_xp_loop.cancel()

    def get_user(self, db, user_id):
        user = db.query(UserData).filter_by(user_id=int(user_id)).first()
        if not user:
            user = UserData(user_id=int(user_id))
            db.add(user)
            db.commit()
            db.refresh(user)
        return user

    def _get_card_data(self, guild_id: int, user_id: int):
        """Single database read path shared by rank and developer card tests."""
        db = SessionLocal()
        try:
            config = db.query(GuildConfig).filter_by(guild_id=str(guild_id)).first()
            user = self.get_user(db, user_id)
            higher_xp = db.query(UserData).filter(UserData.xp > user.xp).count()
            # Ties use the stable user id to make the displayed position deterministic.
            tied_before = db.query(UserData).filter(UserData.xp == user.xp, UserData.user_id < user.user_id).count()
            return (
                user.level, user.xp, higher_xp + tied_before + 1, user.daily_xp_earned,
                config.daily_xp_limit if config else 100,
                config.leveling_channel if config else None,
            )
        finally:
            db.close()

    def _process_message_xp(self, guild_id: int, author_id: int, message_content: str):
        db = SessionLocal()
        try:
            config = db.query(GuildConfig).filter_by(guild_id=str(guild_id)).first()
            if not config or not config.leveling_enabled:
                return False, 0, 0, None
            
            if len(message_content) < config.min_message_length:
                return False, 0, 0, None

            user = self.get_user(db, author_id)
            
            today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5))).strftime('%Y-%m-%d')
            if not user.daily_xp_date or str(user.daily_xp_date) != today:
                user.daily_xp_date = today
                user.daily_xp_earned = 0
            
            if user.daily_xp_earned < config.daily_xp_limit:
                user.xp += config.xp_per_message
                user.daily_xp_earned += config.xp_per_message
                user.messages += 1
                user.last_message = message_content[:255]
                
                new_level = calculate_level(user.xp)
                level_up = False
                if new_level > user.level:
                    user.level = new_level
                    level_up = True
                
                db.commit()
                return level_up, new_level, user.xp, config.leveling_channel
            return False, 0, 0, None
        finally:
            db.close()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        
        import asyncio
        level_up, new_level, current_xp, leveling_channel = await asyncio.to_thread(
            self._process_message_xp, message.guild.id, message.author.id, message.content
        )
        
        if level_up:
            db = SessionLocal()
            try:
                config = db.query(GuildConfig).filter_by(guild_id=str(message.guild.id)).first()
                if not config or not getattr(config, "level_up_announcements_enabled", False):
                    return

                target_channel_id = getattr(config, "level_up_announcements_channel", None) or leveling_channel or str(DEFAULT_LEVELING_CHANNEL_ID)
                ch = message.guild.get_channel(int(target_channel_id))
                if ch:
                    await self.send_level_up_announcement(message.author, new_level, current_xp, ch)
            finally:
                db.close()

    @app_commands.command(name="rank", description="Check your rank")
    async def rank(self, interaction: discord.Interaction, member: discord.Member = None):
        db = SessionLocal()
        try:
            config = db.query(GuildConfig).filter_by(guild_id=str(interaction.guild_id)).first()
            channel_id = get_leaderboard_channel_id(config)
            if not channel_id:
                await interaction.response.send_message("Leaderboard is not enabled for this server.", ephemeral=True)
                return
            if str(interaction.channel_id) != channel_id:
                await interaction.response.send_message(f"This command can only be used in <#{channel_id}>.", ephemeral=True)
                return

            member = member or interaction.user
            user = self.get_user(db, member.id)
            
            all_users = db.query(UserData).order_by(UserData.xp.desc()).all()
            rank_pos = None
            for idx, u in enumerate(all_users, start=1):
                if u.user_id == member.id:
                    rank_pos = idx
                    break
                    
            xp_needed = str(max(calculate_required_xp(user.level) - user.xp, 0))
            
            embed = discord.Embed(
                title=f"{member.display_name}'s Rank", 
                description="Progress, leaderboard position, and daily earning status.", 
                color=discord.Color.blue()
            )
            embed.set_thumbnail(url=member.display_avatar.url if member.display_avatar else None)
            embed.add_field(name="Current Level", value=f"`{user.level}`", inline=True)
            embed.add_field(name="Total XP", value=f"`{user.xp}`", inline=True)
            embed.add_field(name="Rank Position", value=f"`#{rank_pos}`" if rank_pos else "`Unranked`", inline=True)
            embed.add_field(name="XP Needed For Next Level", value=f"`{xp_needed}`", inline=True)
            embed.add_field(name="Daily XP Earned", value=f"`{user.daily_xp_earned}`", inline=True)
            embed.set_footer(text="Rank updates automatically as you stay active.")
            await interaction.response.send_message(embed=embed)
        finally:
            db.close()

    @app_commands.command(name="leaderboard", description="View top XP earners")
    async def leaderboard(self, interaction: discord.Interaction):
        db = SessionLocal()
        try:
            config = db.query(GuildConfig).filter_by(guild_id=str(interaction.guild_id)).first()
            channel_id = get_leaderboard_channel_id(config)
            if not channel_id:
                await interaction.response.send_message("Leaderboard is not enabled for this server.", ephemeral=True)
                return
            if str(interaction.channel_id) != channel_id:
                await interaction.response.send_message(f"This command can only be used in <#{channel_id}>.", ephemeral=True)
                return

            custom_lb = db.query(CustomLeaderboard).filter_by(channel_id=str(interaction.channel_id)).first()
            if custom_lb:
                scores = []
                channel = interaction.guild.get_channel(interaction.channel_id)
                if channel:
                    all_users = db.query(UserData).all()
                    user_map = {u.user_id: u for u in all_users}
                    
                    att_logs = db.query(AttendanceLog).filter_by(guild_id=str(interaction.guild_id)).all()
                    att_map = {}
                    for log in att_logs:
                        uid = int(log.user_id)
                        att_map[uid] = att_map.get(uid, 0) + 1
                        
                    for member in channel.members:
                        if member.bot: continue
                        
                        if not should_include_member_for_custom_leaderboard(member, required_role_ids=[lb.required_role_id] if lb.required_role_id else []): # type: ignore
                            continue
                        
                        user = user_map.get(member.id)
                        if not user: continue
                        att_count = att_map.get(member.id, 0)
                        score = user.xp + (att_count * 50)
                        scores.append((member, score, user.level, user.xp, att_count))
                
                scores.sort(key=lambda x: x[1], reverse=True)
                top_3 = scores[:3]
                
                embed = discord.Embed(title=f"Top 3 {custom_lb.name}", description="Ranked by XP and Attendance", color=discord.Color.blue())
                if not top_3:
                    embed.description = "No data yet!"
                else:
                    for idx, (member, score, level, xp, att_count) in enumerate(top_3, 1):
                        embed.add_field(
                            name=f"#{idx} {member.display_name}",
                            value=f"Level {level} | {xp} XP | {att_count} Days\nCombined Score: {score}",
                            inline=False
                        )
                await interaction.response.send_message(embed=embed)
                return
            
            allowed_role_ids = get_main_leaderboard_role_ids(db, interaction.guild_id)
            ranked_users = []
            for user in db.query(UserData).order_by(UserData.xp.desc()).all():
                discord_user = interaction.guild.get_member(user.user_id)
                if not discord_user or discord_user.bot:
                    continue
                if not should_include_member_for_main_leaderboard(discord_user, allowed_role_ids):
                    continue
                ranked_users.append(user)
                if len(ranked_users) >= 10:
                    break
            
            embed = discord.Embed(title="Community Leaderboard", description="Top 10 users by total XP.", color=discord.Color.gold())
            if not ranked_users:
                embed.description = "No leaderboard data is available yet."
                await interaction.response.send_message(embed=embed)
                return
            
            for index, u in enumerate(ranked_users, start=1):
                discord_user = interaction.guild.get_member(u.user_id)
                name = discord_user.display_name if discord_user else f"Unknown ({u.user_id})"
                prefix = f"#{index}"
                embed.add_field(
                    name=f"{prefix} {name}",
                    value=f"Level {u.level}\n{u.xp} XP",
                    inline=False
                )
                
            await interaction.response.send_message(embed=embed)
        finally:
            db.close()



    @app_commands.command(name="rankcard", description="Generate a rank card image.")
    async def rankcard(self, interaction: discord.Interaction, member: discord.Member = None):
        db = SessionLocal()
        try:
            config = db.query(GuildConfig).filter_by(guild_id=str(interaction.guild_id)).first()
            channel_id = get_leaderboard_channel_id(config)
            if not channel_id:
                await interaction.response.send_message("Leaderboard is not enabled for this server.", ephemeral=True)
                return
            if str(interaction.channel_id) != channel_id:
                await interaction.response.send_message(f"This command can only be used in <#{channel_id}>.", ephemeral=True)
                return

            await interaction.response.defer()
            target = member or interaction.user
            
            try:
                sys.path.append(os.path.join(os.path.dirname(__file__), "..", "utils"))
                from rankcard import generate_rank_card # type: ignore
                
                level, xp, rank_pos, daily_xp, daily_limit, _ = await asyncio.to_thread(self._get_card_data, interaction.guild_id, target.id)
                file = await generate_rank_card(
                    target, level, xp, rank_pos, daily_xp, daily_limit, 100,
                    profile_title=interaction.guild.name if interaction.guild else "Community Profile",
                )
                await interaction.followup.send(file=file)
            except ImportError:
                await interaction.followup.send("The rankcard module could not be loaded. Please ensure Pillow is installed.", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"Error generating rank card: {e}", ephemeral=True)
        finally:
            db.close()

    def _batch_add_voice_xp(self, voice_users):
        db = SessionLocal()
        level_ups = []
        try:
            today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5))).strftime('%Y-%m-%d')
            for guild_id, user_id, xp_per_min, daily_limit in voice_users:
                user = self.get_user(db, user_id)
                if not user.daily_xp_date or str(user.daily_xp_date) != today:
                    user.daily_xp_date = today
                    user.daily_xp_earned = 0
                
                if user.daily_xp_earned < daily_limit:
                    user.xp += xp_per_min
                    user.daily_xp_earned += xp_per_min
                    user.voice_minutes += 1
                    
                    new_level = calculate_level(user.xp)
                    if new_level > user.level:
                        user.level = new_level
                        config = db.query(GuildConfig).filter_by(guild_id=str(guild_id)).first()
                        ch_id = config.leveling_channel if config else None
                        level_ups.append((guild_id, user_id, new_level, user.xp, ch_id))
            db.commit()
            return level_ups
        except Exception as e:
            print(f"Voice XP error: {e}")
            return []
        finally:
            db.close()

    async def send_level_up_announcement(self, member: discord.Member, new_level: int, current_xp: int, channel: discord.TextChannel):
        try:
            sys.path.append(os.path.join(os.path.dirname(__file__), "..", "utils"))
            from rankcard import generate_levelup_card # type: ignore
            
            card_file = await generate_levelup_card(
                member,
                new_level,
                max_level=100,
                previous_level=max(1, new_level - 1),
            )

            msg = f"🎉 {member.mention} upgraded to level {new_level}!"

            await channel.send(content=msg, file=card_file)
        except Exception as e:
            print(f"Error sending level up announcement: {e}")

    @tasks.loop(minutes=1)
    async def voice_xp_loop(self):
        voice_users = []
        for guild in self.bot.guilds:
            config_db = SessionLocal()
            try:
                config = config_db.query(GuildConfig).filter_by(guild_id=str(guild.id)).first()
                if not config or not config.leveling_enabled:
                    continue
                daily_limit = config.daily_xp_limit
                xp_per_min = 3  # Lower rate for voice channels
            except Exception as e:
                print(f"Error fetching config: {e}")
                continue
            finally:
                config_db.close()

            for vc in guild.voice_channels:
                # Require at least 2 non-bot members to prevent AFK farming
                non_bots = [m for m in vc.members if not m.bot and not m.voice.self_deaf and not m.voice.deaf]
                if len(non_bots) >= 2:
                    for member in non_bots:
                        voice_users.append((guild.id, member.id, xp_per_min, daily_limit))
                        
        if voice_users:
            import asyncio
            level_ups = await asyncio.to_thread(self._batch_add_voice_xp, voice_users)
            for guild_id, user_id, new_level, current_xp, ch_id in level_ups:
                guild = self.bot.get_guild(guild_id)
                if guild:
                    member = guild.get_member(user_id)
                    if member:
                        config_db = SessionLocal()
                        try:
                            config = config_db.query(GuildConfig).filter_by(guild_id=str(guild.id)).first()
                            if not config or not getattr(config, "level_up_announcements_enabled", False):
                                continue
                            target_ch_id = getattr(config, "level_up_announcements_channel", None) or ch_id or str(DEFAULT_LEVELING_CHANNEL_ID)
                            ch = guild.get_channel(int(target_ch_id))
                            if ch:
                                await self.send_level_up_announcement(member, new_level, current_xp, ch)
                        finally:
                            config_db.close()

    @voice_xp_loop.before_loop
    async def before_voice_xp(self):
        await self.bot.wait_until_ready()

async def setup(bot):
    await bot.add_cog(LevelingCog(bot))
