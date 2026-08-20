import discord
from discord.ext import commands
from discord import app_commands
import re


class AdminCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="message", description="Admin: send a message to a specified channel")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(channel="The text, voice, or stage-channel chat to send the message to", content="The message to send")
    async def message(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | discord.VoiceChannel | discord.StageChannel,
        content: str = None,
    ):
        # Use a modal to accept multi-line content from admins. If `content`
        # is provided it will be used to pre-fill the modal; the final send
        # happens when the modal is submitted.
        class MessageModal(discord.ui.Modal, title="Send Message"):
            def __init__(
                self,
                target_channel: discord.TextChannel | discord.VoiceChannel | discord.StageChannel,
                prefill: str | None = None,
            ):
                super().__init__()
                self.target_channel = target_channel
                self.body = discord.ui.TextInput(label="Message content", style=discord.TextStyle.paragraph, required=True, default=prefill or "")
                self.add_item(self.body)

            async def on_submit(self, modal_interaction: discord.Interaction):
                bot = modal_interaction.client
                forbidden_re = getattr(bot, "FORBIDDEN_RE", None)
                forbidden_words = getattr(bot, "FORBIDDEN_WORDS", set())
                # Preserve original input exactly; do not strip or normalize whitespace.
                original_text = self.body.value or ""

                # Use a lowercase tokenized copy for forbidden-word checks without mutating original_text
                txt = original_text.lower()
                try:
                    is_bad = bool(forbidden_re.search(txt)) if forbidden_re else any(token in forbidden_words for token in __import__('re').findall(r"\w+", txt))
                except Exception:
                    is_bad = any(token in forbidden_words for token in __import__('re').findall(r"\w+", txt))

                if is_bad:
                    try:
                        await modal_interaction.response.send_message("Message blocked: contains forbidden or disallowed language.", ephemeral=True)
                    except Exception:
                        pass
                    # Log attempt to configured bot-log channel for auditing
                    try:
                        log_ch = await bot._get_configured_log_channel(modal_interaction.guild_id)
                        if log_ch:
                            truncated = (original_text[:800] + "...") if len(original_text) > 800 else original_text
                            embed = discord.Embed(title="Blocked /message attempt", color=discord.Color.orange())
                            embed.add_field(name="Admin", value=f"{modal_interaction.user} ({modal_interaction.user.id})", inline=True)
                            embed.add_field(name="Target Channel", value=f"{self.target_channel.mention} ({self.target_channel.id})", inline=True)
                            embed.add_field(name="Content", value=truncated, inline=False)
                            await log_ch.send(embed=embed)
                    except Exception:
                        pass
                    return

                # Not blocked — attempt to send
                try:
                    # Send while preserving exact formatting. If the message exceeds Discord's
                    # per-message character limit, split by lines (preserving newlines) so text
                    # formatting remains intact.
                    def split_preserve_lines(text: str, limit: int = 2000):
                        # Keep line breaks as part of lines so blank lines are preserved
                        lines = text.splitlines(keepends=True)
                        chunks = []
                        cur = ""
                        for line in lines:
                            if len(cur) + len(line) <= limit:
                                cur += line
                            else:
                                if cur:
                                    chunks.append(cur)
                                # If single line is longer than limit, hard-split it
                                if len(line) > limit:
                                    start = 0
                                    while start < len(line):
                                        chunks.append(line[start:start+limit])
                                        start += limit
                                    cur = ""
                                else:
                                    cur = line
                        if cur:
                            chunks.append(cur)
                        return chunks

                    allowed = discord.AllowedMentions(everyone=True, users=True, roles=True)
                    chunks = split_preserve_lines(original_text, limit=2000)
                    for chunk in chunks:
                        await self.target_channel.send(chunk, allowed_mentions=allowed)
                    try:
                        await modal_interaction.response.send_message(f"Message sent to {self.target_channel.mention}.", ephemeral=True)
                    except Exception:
                        pass
                except discord.Forbidden:
                    try:
                        await modal_interaction.response.send_message("I don't have permission to send messages in that channel.", ephemeral=True)
                    except Exception:
                        pass
                except discord.HTTPException as e:
                    try:
                        await modal_interaction.response.send_message(f"Failed to send message: {e}", ephemeral=True)
                    except Exception:
                        pass

        # Defer and show modal for multi-line input
        await interaction.response.send_modal(MessageModal(channel, prefill=content))
        return
        # Moderate admin-provided content using the bot's forbidden-word rules
        forbidden_re = getattr(self.bot, "FORBIDDEN_RE", None)
        forbidden_words = getattr(self.bot, "FORBIDDEN_WORDS", set())
        txt = (content or "").strip().lower()
        is_bad = False
        try:
            is_bad = bool(forbidden_re.search(txt)) if forbidden_re else any(token in forbidden_words for token in re.findall(r"\w+", txt))
        except Exception:
            is_bad = any(token in forbidden_words for token in re.findall(r"\w+", txt))

        if is_bad:
            # Notify the admin that the message was blocked
            try:
                await interaction.followup.send("Message blocked: contains forbidden or disallowed language.", ephemeral=True)
            except Exception:
                pass

            # Log attempt to configured bot-log channel for auditing
            try:
                log_ch = await self.bot._get_configured_log_channel(interaction.guild_id)
                if log_ch:
                    truncated = (content[:800] + "...") if len(content) > 800 else content
                    embed = discord.Embed(title="Blocked /message attempt", color=discord.Color.orange())
                    embed.add_field(name="Admin", value=f"{interaction.user} ({interaction.user.id})", inline=True)
                    embed.add_field(name="Target Channel", value=f"{channel.mention} ({channel.id})", inline=True)
                    embed.add_field(name="Content", value=truncated, inline=False)
                    await log_ch.send(embed=embed)
            except Exception:
                pass
            return
        # Ensure the channel is text-based and belongs to the same guild
        if not channel or (interaction.guild and channel.guild.id != interaction.guild.id):
            await interaction.followup.send("Invalid channel.", ephemeral=True)
            return

        try:
            await channel.send(content)
            await interaction.followup.send(f"Message sent to {channel.mention}.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("I don't have permission to send messages in that channel.", ephemeral=True)
        except discord.HTTPException as e:
            await interaction.followup.send(f"Failed to send message: {e}", ephemeral=True)


async def setup(bot):
    await bot.add_cog(AdminCog(bot))
