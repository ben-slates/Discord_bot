import discord
from discord.ext import commands, tasks
from discord import app_commands
import io
import asyncio
import datetime
import logging
import os
import time
import re
from database import SessionLocal, GuildConfig, Ticket
from utils.db_executor import run_db, run_db_profiled
from utils.diag import instrument_async
from dotenv import load_dotenv
try:
    from google import genai as modern_genai
except ImportError:
    modern_genai = None

load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY and modern_genai is not None:
    GENAI_CLIENT = modern_genai.Client(api_key=GEMINI_API_KEY)
else:
    GENAI_CLIENT = None


async def generate_gemini_content(model: str, prompt: str):
    if GENAI_CLIENT is not None:
        return await GENAI_CLIENT.aio.models.generate_content(model=model, contents=prompt)
    raise RuntimeError("Gemini is unavailable: install google-genai and configure GEMINI_API_KEY.")


class SupportRoleModal(discord.ui.Modal, title="Support Admin Role"):
    def __init__(self, bot, category: discord.CategoryChannel):
        super().__init__()
        self.bot = bot
        self.category = category
        self.role = discord.ui.TextInput(
            label="Admin role mention, ID, or exact name",
            placeholder="@Support Admins or 123456789012345678",
            required=True,
            max_length=100,
        )
        self.add_item(self.role)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.role.value.strip()
        role = None
        match = re.fullmatch(r"<@&(\d+)>|([0-9]+)", raw)
        if match:
            role = interaction.guild.get_role(int(match.group(1) or match.group(2)))
        if role is None:
            role = discord.utils.find(lambda item: item.name.lower() == raw.lower(), interaction.guild.roles)
        if role is None:
            await interaction.response.send_message("I could not find that role. Enter its mention, ID, or exact name.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        cog = self.bot.get_cog("SupportCog")
        if not cog:
            await interaction.followup.send("Support is unavailable while the support cog is loading.", ephemeral=True)
            return
        await cog.configure_support(interaction, self.category, role)


class SupportDashboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Create Ticket", style=discord.ButtonStyle.primary, custom_id="support_create_ticket")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("SupportCog")
        if not cog:
            await interaction.response.send_message("Support is currently unavailable.", ephemeral=True)
            return
        await cog.create_ticket_from_dashboard(interaction)


class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, custom_id="support_close_ticket")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("SupportCog")
        if not cog:
            await interaction.response.send_message("Support is currently unavailable.", ephemeral=True)
            return
        await cog.close_ticket_interaction(interaction)


class ReopenTicketView(discord.ui.View):
    """Action shown after closing a ticket; reopening remains admin-only."""

    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Re-open Ticket", style=discord.ButtonStyle.success, custom_id="support_reopen_ticket")
    async def reopen_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only administrators can reopen tickets.", ephemeral=True)
            return
        cog = interaction.client.get_cog("SupportCog")
        if not cog:
            await interaction.response.send_message("Support is currently unavailable.", ephemeral=True)
            return
        await cog.reopen_ticket_interaction(interaction)

class TicketSupportView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Mark as Resolved", style=discord.ButtonStyle.success, custom_id="ticket_resolved")
    async def resolved(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("SupportCog")
        if cog:
            cog.human_requested.add(interaction.channel.id)
        await interaction.response.send_message(
            "Glad it helped! Click below to close this ticket when you are ready.",
            view=CloseTicketView(),
            ephemeral=True,
        )

    @discord.ui.button(label="Talk to Human / Unsatisfied", style=discord.ButtonStyle.danger, custom_id="ticket_human")
    async def human(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("SupportCog")
        if cog:
            cog.human_requested.add(interaction.channel.id)
        config = await run_db(cog._get_support_config, interaction.guild.id) if cog else None
        admin_role = cog.get_support_admin_role(interaction.guild, config) if cog else None
        mention = admin_role.mention if admin_role else "@here"
        await interaction.response.send_message(f"{mention} {interaction.user.mention} is unsatisfied with the AI answer and requested human assistance!")

class SupportCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._command_names = {"ticket", "adduser", "removeuser", "close", "reopen", "transcript", "closeall"}
        self._support_command_objects = {}
        self.bot.add_view(TicketSupportView())
        self.bot.add_view(SupportDashboardView())
        self.auto_delete_tickets.start()
        self.dashboard_check.start()
        self.chat_sessions = {}
        self.human_requested = set()

    def cog_unload(self):
        self.auto_delete_tickets.cancel()
        self.dashboard_check.cancel()

    @tasks.loop(minutes=30)
    async def auto_delete_tickets(self):
        # Run DB work in a thread to avoid blocking the event loop
        await run_db(self._auto_delete_tickets_worker)

    def _auto_delete_tickets_worker(self):
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
                            # Do not discard the ticket row until Discord confirms deletion.
                            future = asyncio.run_coroutine_threadsafe(
                                channel.delete(reason="Auto-deleted 12 hours after closing"), self.bot.loop
                            )
                            future.result(timeout=30)
                        except Exception:
                            continue
                db.delete(ticket)
            db.commit()
        except Exception:
            pass
        finally:
            db.close()
            
    @auto_delete_tickets.before_loop
    async def before_auto_delete(self):
        await self.bot.wait_until_ready()

    # Keep the public ticket dashboard available if it is accidentally deleted.
    @tasks.loop(minutes=1)
    async def dashboard_check(self):
        for guild in self.bot.guilds:
            try:
                config = await run_db(self._get_support_config, guild.id)
                if config and config.support_enabled and config.support_category and config.support_admin_role:
                    await self.ensure_dashboard(guild, int(config.support_category))
            except Exception:
                logging.exception("Failed to check support dashboard for guild %s", guild.id)

    @dashboard_check.before_loop
    async def before_dashboard_check(self):
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

    def get_support_admin_role(self, guild, config=None):
        config = config or self._get_support_config(guild.id)
        role_id = getattr(config, "support_admin_role", None) if config else None
        if role_id:
            role = guild.get_role(int(role_id))
            if role:
                return role
        return next((role for role in guild.roles if "admin" in role.name.lower()), None)

    async def configure_support(self, interaction, category, role):
        await run_db(_save_support_config, interaction.guild_id, category.id, role.id)
        try:
            await self.ensure_dashboard(interaction.guild, category.id)
        except Exception:
            logging.exception("Support enabled but dashboard creation failed for guild %s", interaction.guild_id)
        await interaction.followup.send(
            f"Support enabled for {category.mention}. The admin role {role.mention} will be notified when AI needs help.",
            ephemeral=True,
        )

    async def ensure_dashboard(self, guild, category_id):
        category = guild.get_channel(int(category_id))
        if not isinstance(category, discord.CategoryChannel):
            logging.warning("Configured support category %s was not found in guild %s", category_id, guild.id)
            return False
        channels = sorted(category.text_channels, key=lambda item: (item.position, item.id))
        if not channels:
            logging.warning("Support category %s has no text channels in guild %s", category.id, guild.id)
            return False
        channel = channels[0]
        try:
            async for message in channel.history(limit=100):
                if message.author == self.bot.user and message.embeds and message.embeds[0].title == "Support Dashboard":
                    return True
        except discord.HTTPException:
            logging.exception("Could not inspect support dashboard channel %s", channel.id)
        config = await run_db(self._get_support_config, guild.id)
        role = self.get_support_admin_role(guild, config)
        role_mention = role.mention if role else "@here"
        embed = discord.Embed(
            title="Support Dashboard",
            description=(
                "Need help? Click **Create Ticket** below to open a private support ticket.\n"
                f"{role_mention} will be notified if the AI assistant cannot help."
            ),
            color=discord.Color.blurple(),
        )
        await channel.send(embed=embed, view=SupportDashboardView(), allowed_mentions=discord.AllowedMentions(roles=True, everyone=True))
        logging.info("Support dashboard sent to channel %s in guild %s", channel.id, guild.id)
        return True

    async def _get_support_config_async(self, guild_id):
        return await run_db(self._get_support_config, guild_id)

    async def _is_support_enabled(self, guild_id):
        config = await self._get_support_config_async(guild_id)
        return bool(config and config.support_enabled and config.support_category)

    async def _is_in_support_category(self, channel, guild_id):
        if not channel or not guild_id or not getattr(channel, "guild", None):
            return False
        config = await self._get_support_config_async(guild_id)
        if not config or not config.support_enabled or not config.support_category:
            return False
        try:
            return channel.category_id == int(config.support_category)
        except (TypeError, ValueError):
            return False

    async def _can_use_support_commands(self, interaction):
        config = await self._get_support_config_async(interaction.guild_id)
        if not config or not config.support_enabled or not config.support_category:
            return False
        if not await self._is_in_support_category(interaction.channel, interaction.guild_id):
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
    @instrument_async(threshold=0.2)
    async def on_message(self, message: discord.Message):
        handler_started = time.perf_counter()
        if message.author.bot or not message.guild:
            return

        config = await run_db_profiled("support.message.config", self._get_support_config, message.guild.id)
        if not config or not config.support_enabled or not config.support_category:
            return

        try:
            in_support_category = message.channel.category_id == int(config.support_category)
        except (TypeError, ValueError):
            in_support_category = False
        if not in_support_category:
            return

        if message.channel.name.startswith("ticket-"):
            is_open = await run_db_profiled("support.message.ticket", self._is_ticket_open, message.channel.id)
            if not is_open:
                return
                
            if message.channel.id in self.human_requested:
                return

            async with message.channel.typing():
                try:
                    gemini_elapsed = 0.0
                    admin_role = self.get_support_admin_role(message.guild, config)
                    admin_mention = admin_role.mention if admin_role else "@here"
                    history_key = message.channel.id
                    if history_key not in self.chat_sessions:
                        self.chat_sessions[history_key] = []

                    self.chat_sessions[history_key].append(f"User: {message.content}")
                    context_str = "\n".join(self.chat_sessions[history_key][-10:])
                    prompt = f"{context_str}\n\nYou are a helpful IT/Community Support AI for this Discord server. Provide a concise, helpful response based on the conversation history."
                    alert_mention = None
                    if GENAI_CLIENT is None:
                        ai_answer = "The AI assistant is currently unavailable. An administrator has been notified to assist you."
                        alert_mention = admin_mention
                    else:
                        try:
                            gemini_started = time.perf_counter()
                            response = await generate_gemini_content("gemini-2.5-flash", prompt)
                            gemini_elapsed = time.perf_counter() - gemini_started
                            ai_answer = getattr(response, "text", str(response))
                        except Exception as primary_error:
                            logging.warning("Primary Gemini model failed: %s", primary_error)
                            try:
                                gemini_started = time.perf_counter()
                                response = await generate_gemini_content("gemini-2.5-flash-lite", prompt)
                                gemini_elapsed = time.perf_counter() - gemini_started
                                ai_answer = getattr(response, "text", str(response))
                            except Exception as fallback_error:
                                logging.error("Gemini fallback failed: %s", fallback_error)
                                ai_answer = "The AI assistant could not respond. An administrator has been notified to assist you."
                                alert_mention = admin_mention
                    self.chat_sessions[history_key].append(f"AI: {ai_answer}")
                except Exception as e:
                    ai_answer = f"I'm sorry, I couldn't generate an AI response at the moment. Error: {str(e)}"
                    alert_mention = self.get_support_admin_role(message.guild, config)
                    alert_mention = alert_mention.mention if alert_mention else "@here"

            embed = discord.Embed(title="AI Assistant", description=ai_answer, color=discord.Color.blue())
            send_started = time.perf_counter()
            await message.channel.send(
                content=alert_mention,
                embed=embed,
                view=TicketSupportView(),
                allowed_mentions=discord.AllowedMentions(roles=True, everyone=True),
            )
            send_elapsed = time.perf_counter() - send_started
            total_elapsed = time.perf_counter() - handler_started
            if total_elapsed >= 0.2:
                logging.warning(
                    "Support message timing: total=%.3fs gemini=%.3fs discord_send=%.3fs local=%.3fs",
                    total_elapsed, gemini_elapsed, send_elapsed,
                    total_elapsed - gemini_elapsed - send_elapsed,
                )

    # --- USER COMMANDS ---
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def create_ticket_from_dashboard(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        db = SessionLocal()
        try:
            config = self._get_support_config(interaction.guild_id)
            if not config or not config.support_enabled or not config.support_category:
                await interaction.followup.send(" Support is disabled or not configured.", ephemeral=True)
                return

            cat_id = config.support_category
            if not cat_id:
                await interaction.followup.send(" Support category not configured.", ephemeral=True)
                return

            category = interaction.guild.get_channel(int(cat_id))
            if not await self._can_use_support_commands(interaction):
                await interaction.followup.send(" Support commands must be used inside the configured support category.", ephemeral=True)
                return

            if not isinstance(category, discord.CategoryChannel):
                await interaction.followup.send(" The configured support target is not a category.", ephemeral=True)
                return
            if not category:
                await interaction.followup.send(" Invalid support category.", ephemeral=True)
                return

            existing = db.query(Ticket).filter_by(guild_id=str(interaction.guild_id), owner_id=str(interaction.user.id), status="open").first()
            if existing:
                await interaction.followup.send(" You already have an open ticket.", ephemeral=True)
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

                # Keep the confirmation private to the member who clicked Create Ticket.
                await interaction.followup.send(f" Ticket created: {channel.mention}", ephemeral=True)
                
                embed = discord.Embed(
                    title="Support Ticket",
                    description=(
                        "**Please type your question or issue below!**\n"
                        "Our AI assistant will try to help you first before a human steps in."
                    ),
                    color=discord.Color.green(),
                )
                await channel.send(f"{interaction.user.mention}", embed=embed, view=TicketSupportView())
            except discord.Forbidden:
                await interaction.followup.send(" Missing permissions.", ephemeral=True)
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
            # Log blocked /question attempts to bot-log channel for auditing
            try:
                log_ch = await self.bot._get_configured_log_channel(interaction.guild_id)
                if log_ch:
                    truncated = (text[:800] + "...") if len(text) > 800 else text
                    embed = discord.Embed(title="Blocked /question attempt", color=discord.Color.orange())
                    embed.add_field(name="User", value=f"{interaction.user} ({interaction.user.id})", inline=True)
                    embed.add_field(name="Channel", value=f"#{interaction.channel.name} ({interaction.channel.id})", inline=True)
                    embed.add_field(name="Content", value=truncated, inline=False)
                    await log_ch.send(embed=embed)
            except Exception:
                pass
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
        if not await self._is_in_support_category(interaction.channel, interaction.guild_id):
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
        if not await self._is_in_support_category(interaction.channel, interaction.guild_id):
            await interaction.followup.send(" Must be used in the configured support category.")
            return
        if not interaction.channel.name.startswith("ticket-"):
            await interaction.followup.send(" Must be used in a ticket channel.")
            return
            
        await interaction.channel.set_permissions(member, overwrite=None)
        await interaction.followup.send(f" Removed {member.mention} from the ticket.")

    async def close(self, interaction: discord.Interaction):
        await self.close_ticket_interaction(interaction)

    async def close_ticket_interaction(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        config = self._get_support_config(interaction.guild_id)
        if not config or not config.support_enabled or not config.support_category:
            await interaction.followup.send(" Support is disabled or not configured.", ephemeral=True)
            return
        if not await self._can_use_support_commands(interaction):
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
                await interaction.followup.send(
                    " Ticket marked as closed. An administrator can re-open it with the button below, or it will be deleted shortly.",
                    view=ReopenTicketView(),
                )
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
    async def reopen(self, interaction: discord.Interaction):
        await self.reopen_ticket_interaction(interaction)

    async def reopen_ticket_interaction(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        config = self._get_support_config(interaction.guild_id)
        if not config or not config.support_enabled or not config.support_category:
            await interaction.followup.send(" Support is disabled or not configured.", ephemeral=True)
            return
        if not await self._can_use_support_commands(interaction):
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
                
            await interaction.followup.send(
                " Ticket reopened. Use the buttons below when you are ready.",
                view=TicketSupportView(),
            )
            
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
        if not await self._can_use_support_commands(interaction):
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
        if not await self._can_use_support_commands(interaction):
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


def _save_support_config(guild_id, category_id, role_id):
    db = SessionLocal()
    try:
        config = db.query(GuildConfig).filter_by(guild_id=str(guild_id)).first()
        if not config:
            config = GuildConfig(guild_id=str(guild_id))
            db.add(config)
        config.support_enabled = True
        config.support_category = str(category_id)
        config.support_admin_role = str(role_id)
        db.commit()
    finally:
        db.close()
