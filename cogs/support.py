import discord
from discord.ext import commands, tasks
from discord import app_commands
import io
import asyncio
import datetime
import os
from database import SessionLocal, GuildConfig, Ticket
from dotenv import load_dotenv
import google.generativeai as genai

load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is missing. Add it to the project's .env file.")
genai.configure(api_key=GEMINI_API_KEY)

class TicketSupportView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Mark as Resolved", style=discord.ButtonStyle.success, custom_id="ticket_resolved")
    async def resolved(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("SupportCog")
        if cog:
            cog.human_requested.add(interaction.channel.id)
        await interaction.response.send_message("Glad it helped! Use `/close` to formally close this ticket.", ephemeral=True)

    @discord.ui.button(label="Talk to Human / Unsatisfied", style=discord.ButtonStyle.danger, custom_id="ticket_human")
    async def human(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("SupportCog")
        if cog:
            cog.human_requested.add(interaction.channel.id)
        admin_role = next((r for r in interaction.guild.roles if "admin" in r.name.lower()), None)
        mention = admin_role.mention if admin_role else "@here"
        await interaction.response.send_message(f"{mention} {interaction.user.mention} is unsatisfied with the AI answer and requested human assistance!")

class SupportCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._command_names = {"ticket", "adduser", "removeuser", "close", "reopen", "transcript", "closeall"}
        self._support_command_objects = {}
        self.bot.add_view(TicketSupportView())
        self.auto_delete_tickets.start()
        self.chat_sessions = {}
        self.human_requested = set()

    def cog_unload(self):
        self.auto_delete_tickets.cancel()

    @tasks.loop(minutes=30)
    async def auto_delete_tickets(self):
        db = SessionLocal()
        try:
            threshold = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5))) - datetime.timedelta(hours=12)
            expired_tickets = db.query(Ticket).filter(Ticket.status == "closed", Ticket.closed_at != None, Ticket.closed_at <= threshold).all()
            for ticket in expired_tickets:
                guild = self.bot.get_guild(int(ticket.guild_id))
                if guild:
                    channel = guild.get_channel(int(ticket.channel_id))
                    if channel:
                        try:
                            await channel.delete(reason="Auto-deleted 12 hours after closing")
                        except discord.NotFound:
                            pass
                        except discord.Forbidden:
                            pass
                db.delete(ticket)
            db.commit()
        except:
            pass
        finally:
            db.close()
            
    @auto_delete_tickets.before_loop
    async def before_auto_delete(self):
        await self.bot.wait_until_ready()

    def _is_ticket_open(self, channel_id: int):
        db = SessionLocal()
        try:
            ticket = db.query(Ticket).filter_by(channel_id=str(channel_id), status="open").first()
            return ticket is not None
        finally:
            db.close()

    def _get_support_config(self, guild_id):
        db = SessionLocal()
        try:
            return db.query(GuildConfig).filter_by(guild_id=str(guild_id)).first()
        finally:
            db.close()

    def _is_support_enabled(self, guild_id):
        config = self._get_support_config(guild_id)
        return bool(config and config.support_enabled and config.support_category)

    def _is_in_support_category(self, channel, guild_id):
        if not channel or not guild_id or not getattr(channel, "guild", None):
            return False
        config = self._get_support_config(guild_id)
        if not config or not config.support_enabled or not config.support_category:
            return False
        try:
            return channel.category_id == int(config.support_category)
        except (TypeError, ValueError):
            return False

    def _can_use_support_commands(self, interaction):
        config = self._get_support_config(interaction.guild_id)
        if not config or not config.support_enabled or not config.support_category:
            return False
        if not self._is_in_support_category(interaction.channel, interaction.guild_id):
            return False
        return True

    async def _is_available_in_guild(self, guild_id):
        if not guild_id:
            return False
        config = self._get_support_config(guild_id)
        return bool(config and config.support_enabled and config.support_category)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        db = SessionLocal()
        try:
            config = db.query(GuildConfig).filter_by(guild_id=str(guild.id)).first()
            if not config:
                config = GuildConfig(guild_id=str(guild.id), support_enabled=True)
                db.add(config)
            else:
                config.support_enabled = True
            db.commit()
        except Exception:
            pass
        finally:
            db.close()

        try:
            await self.bot.tree.sync(guild=guild)
        except Exception:
            pass

    async def sync_support_commands(self, guild_id=None):
        if not self._support_command_objects:
            return

        target_guilds = []
        if guild_id is not None:
            guild = self.bot.get_guild(int(guild_id))
            if guild:
                target_guilds.append(guild)
        else:
            target_guilds = list(self.bot.guilds)

        for guild in target_guilds:
            config = self._get_support_config(guild.id)
            enabled = bool(config and config.support_enabled and config.support_category)

            for command_name, command in self._support_command_objects.items():
                if enabled:
                    self.bot.tree.add_command(command, guild=guild, override=True)
                else:
                    self.bot.tree.remove_command(command_name, guild=guild)

            try:
                await self.bot.tree.sync(guild=guild)
            except Exception:
                pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
            
        config = self._get_support_config(message.guild.id)
        if not config or not config.support_enabled or not config.support_category:
            return

        if not self._is_in_support_category(message.channel, message.guild.id):
            return

        if message.channel.name.startswith("ticket-"):
            import asyncio
            is_open = await asyncio.to_thread(self._is_ticket_open, message.channel.id)
            if not is_open:
                return
                
            if message.channel.id in self.human_requested:
                return

            async with message.channel.typing():
                try:
                    history_key = message.channel.id
                    if history_key not in self.chat_sessions:
                        self.chat_sessions[history_key] = []

                    self.chat_sessions[history_key].append(f"User: {message.content}")
                    context_str = "\n".join(self.chat_sessions[history_key][-10:])
                    prompt = f"{context_str}\n\nYou are a helpful IT/Community Support AI for this Discord server. Provide a concise, helpful response based on the conversation history."
                    try:
                        model = genai.GenerativeModel("gemini-2.5-flash")
                        response = model.generate_content(prompt)
                        ai_answer = getattr(response, "text", str(response))
                    except Exception as e:
                        print(f"Primary model failed ({e}), falling back to lite...")
                        try:
                            model = genai.GenerativeModel("gemini-2.5-flash-lite")
                            response = model.generate_content(prompt)
                            ai_answer = getattr(response, "text", str(response))
                        except Exception as fallback_error:
                            ai_answer = f"I'm sorry, I couldn't generate an AI response at the moment. Error: {str(fallback_error)}"
                    self.chat_sessions[history_key].append(f"AI: {ai_answer}")
                except Exception as e:
                    ai_answer = f"I'm sorry, I couldn't generate an AI response at the moment. Error: {str(e)}"

            embed = discord.Embed(title="AI Assistant", description=ai_answer, color=discord.Color.blue())
            await message.channel.send(embed=embed, view=TicketSupportView())

    # --- USER COMMANDS ---
    @app_commands.command(name="ticket", description="Open a support ticket")
    @app_commands.checks.cooldown(1, 5, key=lambda i: (i.guild_id, i.user.id))
    async def ticket(self, interaction: discord.Interaction, reason: str = None):
        await interaction.response.defer(ephemeral=True)
        db = SessionLocal()
        try:
            config = self._get_support_config(interaction.guild_id)
            if not config or not config.support_enabled or not config.support_category:
                await interaction.followup.send(" Support is disabled or not configured.")
                return

            cat_id = config.support_category
            if not cat_id:
                await interaction.followup.send(" Support category not configured.")
                return

            category = interaction.guild.get_channel(int(cat_id))
            if not self._can_use_support_commands(interaction):
                await interaction.followup.send(" Support commands must be used inside the configured support category.")
                return

            if not isinstance(category, discord.CategoryChannel):
                await interaction.followup.send(" The configured support target is not a category.")
                return
            if not category:
                await interaction.followup.send(" Invalid support category.")
                return

            existing = db.query(Ticket).filter_by(guild_id=str(interaction.guild_id), owner_id=str(interaction.user.id), status="open").first()
            if existing:
                await interaction.followup.send(" You already have an open ticket.")
                return

            overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
                interaction.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True)
            }

            try:
                channel = await interaction.guild.create_text_channel(
                    name=f"ticket-{interaction.user.name}",
                    category=category,
                    overwrites=overwrites
                )
                
                new_ticket = Ticket(guild_id=str(interaction.guild_id), channel_id=str(channel.id), owner_id=str(interaction.user.id))
                db.add(new_ticket)
                db.commit()

                await interaction.followup.send(f" Ticket created: {channel.mention}")
                
                embed = discord.Embed(title="Support Ticket", description=f"**Please type your question or issue below!**\nOur AI assistant will try to help you first before a human steps in.\n\nUse `/close` to end, or `/adduser` to invite someone.", color=discord.Color.green())
                await channel.send(f"{interaction.user.mention}", embed=embed)
            except discord.Forbidden:
                await interaction.followup.send(" Missing permissions.")
        finally:
            db.close()

    @app_commands.command(name="question", description="Ask a question in a highlighted announcement box")
    @app_commands.checks.cooldown(1, 10, key=lambda i: (i.guild_id, i.user.id))
    async def question(self, interaction: discord.Interaction, text: str):
        # Moderate the provided question text using the bot's forbidden-word pattern.
        forbidden_re = getattr(self.bot, "FORBIDDEN_RE", None)
        forbidden_words = getattr(self.bot, "FORBIDDEN_WORDS", set())
        txt = (text or "").strip().lower()
        try:
            is_bad = bool(forbidden_re.search(txt)) if forbidden_re else any(token in forbidden_words for token in __import__('re').findall(r"\w+", txt))
        except Exception:
            is_bad = any(token in forbidden_words for token in __import__('re').findall(r"\w+", txt))

        if is_bad:
            await interaction.response.send_message("Your question was not posted because it contains blocked or harmful language.", ephemeral=True)
            return

        embed = discord.Embed(
            title="❓ Question",
            description=text,
            color=discord.Color.dark_blue()
        )
        embed.set_footer(text="Submitted via /question")
        embed.timestamp = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5)))

        channel = interaction.channel
        if channel:
            try:
                await channel.send(embed=embed)
                await interaction.response.send_message("Your question has been posted.", ephemeral=True)
            except discord.Forbidden:
                await interaction.response.send_message("I cannot post the question in this channel.", ephemeral=True)
        else:
            await interaction.response.send_message("Unable to post the question here.", ephemeral=True)

    @app_commands.command(name="adduser", description="Admin: Add a user to this ticket")
    @app_commands.default_permissions(administrator=True)
    async def adduser(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)
        config = self._get_support_config(interaction.guild_id)
        if not config or not config.support_enabled or not config.support_category:
            await interaction.followup.send(" Support is disabled or not configured.")
            return
        if not self._is_in_support_category(interaction.channel, interaction.guild_id):
            await interaction.followup.send(" Must be used in the configured support category.")
            return
        if not interaction.channel.name.startswith("ticket-"):
            await interaction.followup.send(" Must be used in a ticket channel.")
            return
            
        await interaction.channel.set_permissions(member, read_messages=True, send_messages=True)
        await interaction.followup.send(f" Added {member.mention} to the ticket.")

    @app_commands.command(name="removeuser", description="Admin: Remove a user from this ticket")
    @app_commands.default_permissions(administrator=True)
    async def removeuser(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)
        config = self._get_support_config(interaction.guild_id)
        if not config or not config.support_enabled or not config.support_category:
            await interaction.followup.send(" Support is disabled or not configured.")
            return
        if not self._is_in_support_category(interaction.channel, interaction.guild_id):
            await interaction.followup.send(" Must be used in the configured support category.")
            return
        if not interaction.channel.name.startswith("ticket-"):
            await interaction.followup.send(" Must be used in a ticket channel.")
            return
            
        await interaction.channel.set_permissions(member, overwrite=None)
        await interaction.followup.send(f" Removed {member.mention} from the ticket.")

    @app_commands.command(name="close", description="Close current ticket")
    async def close(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        config = self._get_support_config(interaction.guild_id)
        if not config or not config.support_enabled or not config.support_category:
            await interaction.followup.send(" Support is disabled or not configured.", ephemeral=True)
            return
        if not self._can_use_support_commands(interaction):
            await interaction.followup.send(" Must be used in a support ticket channel.", ephemeral=True)
            return
        if not interaction.channel.name.startswith("ticket-"):
            await interaction.followup.send(" Must be used in a ticket channel.", ephemeral=True)
            return
            
        db = SessionLocal()
        try:
            ticket = db.query(Ticket).filter_by(channel_id=str(interaction.channel.id)).first()
            if ticket:
                ticket.status = "closed"
                ticket.closed_at = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5)))
                db.commit()
            
                owner = interaction.guild.get_member(int(ticket.owner_id))
                if owner:
                    try:
                        await interaction.channel.set_permissions(owner, send_messages=False, read_messages=True)
                    except Exception as perm_err:
                        print(f"Perm err: {perm_err}")
            
            try:
                await interaction.followup.send(" Ticket marked as closed. Use `/reopen` to restore, or it will be deleted shortly.")
            except Exception:
                pass
                
            try:
                # Discord rate-limits channel renaming to twice per 10 minutes. 
                # Run it as a background task so it doesn't freeze the bot.
                import asyncio
                asyncio.create_task(interaction.channel.edit(name=f"closed-{interaction.user.name}"))
            except Exception as edit_err:
                print(f"Edit err: {edit_err}")
        except Exception as e:
            try:
                await interaction.followup.send(f" Failed to close ticket: {e}")
            except:
                pass
        finally:
            db.close()

    # --- ADMIN/STAFF COMMANDS ---
    @app_commands.command(name="reopen", description="Admin: Reopen a closed ticket")
    @app_commands.default_permissions(manage_messages=True)
    async def reopen(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        config = self._get_support_config(interaction.guild_id)
        if not config or not config.support_enabled or not config.support_category:
            await interaction.followup.send(" Support is disabled or not configured.", ephemeral=True)
            return
        if not self._can_use_support_commands(interaction):
            await interaction.followup.send(" Must be used in a support ticket channel.", ephemeral=True)
            return
        if not interaction.channel.name.startswith("closed-"):
            await interaction.followup.send(" This is not a closed ticket.", ephemeral=True)
            return
            
        db = SessionLocal()
        try:
            ticket = db.query(Ticket).filter_by(channel_id=str(interaction.channel.id)).first()
            if ticket:
                ticket.status = "open"
                ticket.closed_at = None
                db.commit()
                owner = interaction.guild.get_member(int(ticket.owner_id))
                if owner:
                    await interaction.channel.set_permissions(owner, send_messages=True, read_messages=True)
                
            await interaction.followup.send(" Ticket reopened.")
            
            import asyncio
            asyncio.create_task(interaction.channel.edit(name=interaction.channel.name.replace("closed-", "ticket-")))
        except Exception as e:
            await interaction.followup.send(f" Failed to reopen ticket: {e}")
        finally:
            db.close()

    @app_commands.command(name="transcript", description="Admin: Download ticket transcript")
    @app_commands.default_permissions(manage_messages=True)
    async def transcript(self, interaction: discord.Interaction):
        config = self._get_support_config(interaction.guild_id)
        if not config or not config.support_enabled or not config.support_category:
            await interaction.response.send_message(" Support is disabled or not configured.", ephemeral=True)
            return
        if not self._can_use_support_commands(interaction):
            await interaction.response.send_message(" Must be used in a support ticket channel.", ephemeral=True)
            return
        if not ("ticket-" in interaction.channel.name or "closed-" in interaction.channel.name):
            await interaction.response.send_message(" Must be used in a ticket channel.", ephemeral=True)
            return
            
        await interaction.response.defer()
        messages = [msg async for msg in interaction.channel.history(limit=500, oldest_first=True)]
        
        transcript = f"Transcript for {interaction.channel.name}\n\n"
        for m in messages:
            transcript += f"[{m.created_at.strftime('%Y-%m-%d %H:%M:%S')}] {m.author.name}: {m.content}\n"
            
        file = discord.File(io.BytesIO(transcript.encode()), filename=f"{interaction.channel.name}.txt")
        await interaction.followup.send(" Transcript:", file=file)

    @app_commands.command(name="closeall", description="Admin: Close all open tickets")
    @app_commands.default_permissions(administrator=True)
    async def closeall(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        config = self._get_support_config(interaction.guild_id)
        if not config or not config.support_enabled or not config.support_category:
            await interaction.followup.send(" Support is disabled or not configured.")
            return
        if not self._can_use_support_commands(interaction):
            await interaction.followup.send(" Must be used in the configured support category.")
            return
        db = SessionLocal()
        try:
            open_tickets = db.query(Ticket).filter_by(guild_id=str(interaction.guild_id), status="open").all()
            if not open_tickets:
                await interaction.followup.send(" No open tickets found.")
                return
            
            count = 0
            for ticket in open_tickets:
                try:
                    ticket.status = "closed"
                    ticket.closed_at = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5)))
                    
                    channel = interaction.guild.get_channel(int(ticket.channel_id))
                    if channel:
                        owner = interaction.guild.get_member(int(ticket.owner_id))
                        if owner:
                            await channel.set_permissions(owner, send_messages=False, read_messages=True)
                        
                        name_suffix = owner.name if owner else "unknown"
                        await channel.edit(name=f"closed-{name_suffix}")
                    count += 1
                except:
                    pass
                    
            db.commit()
            await interaction.followup.send(f" Successfully closed {count} open ticket(s).")
        except Exception as e:
            await interaction.followup.send(f" Error: {e}")
        finally:
            db.close()

async def setup(bot):
    cog = SupportCog(bot)
    await bot.add_cog(cog)
