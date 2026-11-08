"""Challenges Management System Cog for Rynex Technologies Discord Bot.

Provides:
- Admin challenge setup and announcement publishing
- Concurrency-safe, atomic flag verification and First/Second/Third Blood leaderboard
- Markdown writeup publishing via text input or file attachment
- Persistent interactions that survive bot restarts
- Automated message replacement and cleanup
"""

import io
import re
import time
import hmac
import hashlib
import datetime
import logging
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy.exc import IntegrityError

from database import (
    SessionLocal,
    GuildConfig,
    ChallengeSettings,
    Challenge,
    ChallengeWinner,
    ChallengePublication,
    UserData,
)
from utils.db_executor import run_db


# ---------------------------------------------------------------------------
# Database Workers (Synchronous - invoked via run_db)
# ---------------------------------------------------------------------------

def _get_challenge_settings_worker(guild_id: str) -> dict:
    db = SessionLocal()
    try:
        cfg = db.query(GuildConfig).filter_by(guild_id=str(guild_id)).first()
        cs = db.query(ChallengeSettings).filter_by(guild_id=str(guild_id)).first()

        enabled = (cs.enabled if cs else False) or (cfg.challenges_enabled if cfg else False)
        channel_id = (cs.announcement_channel_id if cs and cs.announcement_channel_id else None) or (
            cfg.challenges_channel if cfg and cfg.challenges_channel else None
        )
        return {
            "enabled": bool(enabled),
            "announcement_channel_id": str(channel_id) if channel_id else None,
        }
    finally:
        db.close()


def _create_challenge_worker(
    guild_id: str,
    name: str,
    url: str,
    flag_hash: str,
    category: str = "General",
    role_id: str | None = None,
) -> dict:
    db = SessionLocal()
    try:
        # Mark previous active challenges for this guild as inactive
        prev_actives = db.query(Challenge).filter_by(guild_id=str(guild_id), is_active=True).all()
        for prev in prev_actives:
            prev.is_active = False

        new_challenge = Challenge(
            guild_id=str(guild_id),
            name=name,
            url=url,
            flag_hash=flag_hash,
            category=category or "General",
            role_id=str(role_id) if role_id else None,
            is_active=True,
            created_at=datetime.datetime.now(datetime.timezone.utc),
        )
        db.add(new_challenge)
        db.commit()
        db.refresh(new_challenge)
        return {
            "id": new_challenge.id,
            "guild_id": new_challenge.guild_id,
            "name": new_challenge.name,
            "category": new_challenge.category,
            "role_id": new_challenge.role_id,
            "url": new_challenge.url,
            "flag_hash": new_challenge.flag_hash,
            "created_at": new_challenge.created_at,
            "is_active": new_challenge.is_active,
        }
    finally:
        db.close()


def _record_announcement_message_worker(challenge_id: int, guild_id: str, channel_id: str, message_id: str):
    db = SessionLocal()
    try:
        challenge = db.query(Challenge).filter_by(id=int(challenge_id)).first()
        if challenge:
            challenge.announcement_channel_id = str(channel_id)
            challenge.announcement_message_id = str(message_id)

        pub = ChallengePublication(
            guild_id=str(guild_id),
            challenge_id=int(challenge_id),
            publication_type="challenge",
            channel_id=str(channel_id),
            message_id=str(message_id),
            published_at=datetime.datetime.now(datetime.timezone.utc),
        )
        db.add(pub)
        db.commit()
    finally:
        db.close()


def _record_writeup_publication_worker(challenge_id: int, guild_id: str, channel_id: str, message_id: str):
    db = SessionLocal()
    try:
        pub = ChallengePublication(
            guild_id=str(guild_id),
            challenge_id=int(challenge_id) if challenge_id else None,
            publication_type="writeup",
            channel_id=str(channel_id),
            message_id=str(message_id),
            published_at=datetime.datetime.now(datetime.timezone.utc),
        )
        db.add(pub)
        db.commit()
    finally:
        db.close()


def _get_latest_publication_worker(guild_id: str, pub_type: str) -> dict | None:
    db = SessionLocal()
    try:
        pub = (
            db.query(ChallengePublication)
            .filter_by(guild_id=str(guild_id), publication_type=pub_type)
            .order_by(ChallengePublication.id.desc())
            .first()
        )
        if not pub:
            return None
        return {
            "id": pub.id,
            "guild_id": pub.guild_id,
            "challenge_id": pub.challenge_id,
            "publication_type": pub.publication_type,
            "channel_id": pub.channel_id,
            "message_id": pub.message_id,
            "published_at": pub.published_at,
        }
    finally:
        db.close()


def _award_challenge_xp(db, guild_id: str, user_id: int, rank: int | None) -> int:
    """Awards XP for solving a challenge:
    - Base: 50 XP
    - 1st Blood: 50 + 60 = 110 XP
    - 2nd Blood: 50 + 30 = 80 XP
    - 3rd Blood: 50 + 15 = 65 XP
    - Subsequent solvers: 50 XP
    """
    if rank == 1:
        xp_to_award = 50 + 60
    elif rank == 2:
        xp_to_award = 50 + 30
    elif rank == 3:
        xp_to_award = 50 + 15
    else:
        xp_to_award = 50

    user = db.query(UserData).filter_by(guild_id=str(guild_id), user_id=int(user_id)).first()
    if not user:
        user = UserData(guild_id=str(guild_id), user_id=int(user_id), xp=0, level=1)
        db.add(user)

    user.xp = (user.xp or 0) + xp_to_award
    user.last_active_at = datetime.datetime.now(datetime.timezone.utc)
    try:
        from utils.xp import calculate_level
        user.level = calculate_level(user.xp)
    except Exception:
        pass

    return xp_to_award


def _get_active_challenge_worker(guild_id: str) -> dict | None:
    db = SessionLocal()
    try:
        challenge = db.query(Challenge).filter_by(guild_id=str(guild_id), is_active=True).first()
        if not challenge:
            challenge = (
                db.query(Challenge)
                .filter_by(guild_id=str(guild_id))
                .order_by(Challenge.id.desc())
                .first()
            )
        if not challenge:
            return None
        all_winners = (
            db.query(ChallengeWinner)
            .filter_by(challenge_id=challenge.id)
            .order_by(ChallengeWinner.rank.asc())
            .all()
        )
        top_winners = [w for w in all_winners if w.rank <= 3]
        return {
            "challenge": {
                "id": challenge.id,
                "guild_id": challenge.guild_id,
                "name": challenge.name,
                "category": getattr(challenge, "category", "General") or "General",
                "role_id": getattr(challenge, "role_id", None),
                "url": challenge.url,
                "flag_hash": challenge.flag_hash,
                "created_at": challenge.created_at,
                "is_active": challenge.is_active,
                "announcement_channel_id": challenge.announcement_channel_id,
                "announcement_message_id": challenge.announcement_message_id,
            },
            "winners": [(w.rank, w.discord_user_id) for w in top_winners],
            "total_solvers": len(all_winners),
        }
    finally:
        db.close()


def _solve_flag_worker(guild_id: str, user_id: int, submitted_flag: str) -> dict:
    """Atomic flag verification and leaderboard assignment with XP reward."""
    db = SessionLocal()
    try:
        # 1. Ensure Challenges is enabled
        cfg = db.query(GuildConfig).filter_by(guild_id=str(guild_id)).first()
        cs = db.query(ChallengeSettings).filter_by(guild_id=str(guild_id)).first()
        enabled = (cs.enabled if cs else False) or (cfg.challenges_enabled if cfg else False)
        if not enabled:
            return {"status": "disabled"}

        # 2. Fetch current active challenge
        challenge = db.query(Challenge).filter_by(guild_id=str(guild_id), is_active=True).first()
        if not challenge:
            return {"status": "no_active_challenge"}

        # 3. Validate flag hash safely using constant-time comparison
        submitted_hash = hashlib.sha256(submitted_flag.strip().encode("utf-8")).hexdigest()
        if not hmac.compare_digest(submitted_hash, challenge.flag_hash):
            return {"status": "incorrect"}

        # 4. Check if this user has already solved this challenge
        existing = (
            db.query(ChallengeWinner)
            .filter_by(challenge_id=challenge.id, discord_user_id=int(user_id))
            .first()
        )
        if existing:
            return {
                "status": "already_solved",
                "rank": existing.rank if existing.rank <= 3 else None,
                "challenge_id": challenge.id,
                "name": challenge.name,
            }

        # 5. Check occupied ranks and atomically assign rank and XP
        max_attempts = 15
        assigned_rank = None
        is_top_three = False
        xp_awarded = 0

        for attempt in range(max_attempts):
            existing = (
                db.query(ChallengeWinner)
                .filter_by(challenge_id=challenge.id, discord_user_id=int(user_id))
                .first()
            )
            if existing:
                return {
                    "status": "already_solved",
                    "rank": existing.rank if existing.rank <= 3 else None,
                    "challenge_id": challenge.id,
                    "name": challenge.name,
                }

            winners = (
                db.query(ChallengeWinner)
                .filter_by(challenge_id=challenge.id)
                .order_by(ChallengeWinner.rank.asc())
                .all()
            )

            assigned_rank = len(winners) + 1
            is_top_three = (assigned_rank <= 3)

            new_winner = ChallengeWinner(
                challenge_id=challenge.id,
                discord_user_id=int(user_id),
                rank=assigned_rank,
                solved_at=datetime.datetime.now(datetime.timezone.utc),
            )
            db.add(new_winner)
            xp_awarded = _award_challenge_xp(db, guild_id, user_id, assigned_rank if is_top_three else None)

            try:
                db.commit()
                break
            except IntegrityError:
                db.rollback()
                time.sleep(0.005 * (attempt + 1))
        else:
            existing = (
                db.query(ChallengeWinner)
                .filter_by(challenge_id=challenge.id, discord_user_id=int(user_id))
                .first()
            )
            if existing:
                return {
                    "status": "already_solved",
                    "rank": existing.rank if existing.rank <= 3 else None,
                    "challenge_id": challenge.id,
                    "name": challenge.name,
                }
            return {
                "status": "error",
                "challenge_id": challenge.id,
                "name": challenge.name,
            }

        # Fetch top-three winners for embed refresh
        top_winners = (
            db.query(ChallengeWinner)
            .filter_by(challenge_id=challenge.id)
            .filter(ChallengeWinner.rank <= 3)
            .order_by(ChallengeWinner.rank.asc())
            .all()
        )

        if is_top_three:
            return {
                "status": "success",
                "rank": assigned_rank,
                "xp_awarded": xp_awarded,
                "challenge_id": challenge.id,
                "name": challenge.name,
                "category": getattr(challenge, "category", "General") or "General",
                "url": challenge.url,
                "announcement_channel_id": challenge.announcement_channel_id,
                "announcement_message_id": challenge.announcement_message_id,
                "winners": [(w.rank, w.discord_user_id) for w in top_winners],
            }
        else:
            return {
                "status": "late_solve",
                "rank": assigned_rank,
                "xp_awarded": xp_awarded,
                "challenge_id": challenge.id,
                "name": challenge.name,
                "category": getattr(challenge, "category", "General") or "General",
            }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Embed Builders
# ---------------------------------------------------------------------------

def build_challenge_embed(
    name: str,
    url: str,
    winners: list[tuple[int, int]],
    category: str = "General",
) -> discord.Embed:
    """Builds the public challenge announcement embed with real-time leaderboard and category."""
    cat = category or "General"
    embed = discord.Embed(
        title="Rynex Technologies — CTF Challenge",
        description=(
            f"**Challenge:** {name}\n"
            f"**Category:** `{cat}`\n\n"
            "A new challenge is now available! Click **Download Challenge Files** below to retrieve the challenge files, extract the archive with the password provided, and submit the correct flag to claim your position on the leaderboard.\n\n"
            f"🔗 **Download Link:** [{name}.zip]({url})"
        ),
        color=discord.Color.blurple(),
    )

    embed.add_field(
        name="🔑 Archive Password (click to copy)",
        value="```rynextechnologies```\n*Password for challenge zip: `rynextechnologies`*",
        inline=False,
    )

    winner_map = {rank: uid for rank, uid in winners}
    first_blood = f"<@{winner_map[1]}>" if 1 in winner_map else "Available"
    second_blood = f"<@{winner_map[2]}>" if 2 in winner_map else "Available"
    third_blood = f"<@{winner_map[3]}>" if 3 in winner_map else "Available"

    leaderboard_display = (
        f"🥇 **First Blood** — {first_blood}\n"
        f"🥈 **Second Blood** — {second_blood}\n"
        f"🥉 **Third Blood** — {third_blood}"
    )

    embed.add_field(name="Challenge Leaderboard", value=leaderboard_display, inline=False)
    embed.set_footer(text="Click 'Verify Flag' below to submit your flag.")
    return embed


def build_writeup_embed(
    challenge_name: str,
    description: str,
    winners: list[tuple[int, int]] | None = None,
    category: str | None = None,
    total_solvers: int = 0,
) -> discord.Embed:
    """Builds the official writeup announcement embed including the challenge leaderboard."""
    embed = discord.Embed(
        title=f"Rynex Technologies — Challenge Writeup: {challenge_name}",
        description=description,
        color=discord.Color.teal(),
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    if category and category != "General":
        embed.add_field(name="Category", value=f"`{category}`", inline=True)

    winner_map = {rank: uid for rank, uid in (winners or [])}
    first_blood = f"<@{winner_map[1]}>" if 1 in winner_map else "Available"
    second_blood = f"<@{winner_map[2]}>" if 2 in winner_map else "Available"
    third_blood = f"<@{winner_map[3]}>" if 3 in winner_map else "Available"

    leaderboard_display = (
        f"🥇 **First Blood** — {first_blood}\n"
        f"🥈 **Second Blood** — {second_blood}\n"
        f"🥉 **Third Blood** — {third_blood}"
    )
    if total_solvers > 3:
        extra_count = total_solvers - 3
        leaderboard_display += f"\n*(+ {extra_count} additional solver{'s' if extra_count > 1 else ''})*"

    embed.add_field(name="🏆 Challenge Leaderboard", value=leaderboard_display, inline=False)
    embed.set_footer(text="Official Solution & Walkthrough")
    return embed


def format_rank_label(rank: int) -> str:
    if rank == 1:
        return "🥇 First Blood"
    elif rank == 2:
        return "🥈 Second Blood"
    elif rank == 3:
        return "🥉 Third Blood"
    return f"Rank #{rank}"


# ---------------------------------------------------------------------------
# Persistent Announcement View & Verification Modal
# ---------------------------------------------------------------------------

class VerifyFlagModal(discord.ui.Modal, title="Submit Challenge Flag"):
    def __init__(self, cog: "ChallengesCog"):
        super().__init__()
        self.cog = cog
        self.flag = discord.ui.TextInput(
            label="Enter Flag",
            placeholder="CTF{...}",
            required=True,
            max_length=200,
            style=discord.TextStyle.short,
        )
        self.add_item(self.flag)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        user_id = interaction.user.id
        guild_id = str(interaction.guild_id)
        submitted = self.flag.value or ""

        # Verify flag atomically in executor
        result = await run_db(_solve_flag_worker, guild_id, user_id, submitted)
        status = result.get("status")

        if status == "disabled":
            await interaction.followup.send(
                "Challenges are currently disabled on this server.", ephemeral=True
            )
        elif status == "no_active_challenge":
            await interaction.followup.send(
                "There is no active challenge at this time.", ephemeral=True
            )
        elif status == "incorrect":
            await interaction.followup.send(
                "❌ Incorrect flag. Please review your solution and try again!",
                ephemeral=True,
            )
        elif status == "already_solved":
            rank = result.get("rank")
            if rank and rank <= 3:
                rank_str = format_rank_label(rank)
                await interaction.followup.send(
                    f"You have already solved this challenge! Your position: **{rank_str}**.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "You have already solved this challenge and claimed your XP!",
                    ephemeral=True,
                )
        elif status == "late_solve":
            xp = result.get("xp_awarded", 50)
            await interaction.followup.send(
                f"🎉 That is the correct flag! You've been awarded **+{xp} XP**!\n*(Note: The top-three leaderboard positions have already been claimed)*",
                ephemeral=True,
            )
        elif status == "success":
            rank = result.get("rank")
            rank_str = format_rank_label(rank)
            xp = result.get("xp_awarded")
            await interaction.followup.send(
                f"🎉 Correct flag! Congratulations on claiming **{rank_str}**!\n⭐ **+{xp} XP** has been added to your profile!",
                ephemeral=True,
            )

            # Update the original announcement embed without creating a new message
            await self.cog.update_announcement_leaderboard(
                interaction.guild,
                result.get("announcement_channel_id"),
                result.get("announcement_message_id"),
                result.get("name"),
                result.get("url"),
                result.get("winners", []),
                category=result.get("category", "General"),
            )
        else:
            await interaction.followup.send(
                "An unexpected verification error occurred.", ephemeral=True
            )


class ChallengeVerificationPersistentView(discord.ui.View):
    """Persistent view attached to challenge announcements across bot restarts."""

    def __init__(self, cog: "ChallengesCog", challenge_url: str | None = None):
        super().__init__(timeout=None)
        self.cog = cog
        if challenge_url:
            download_btn = discord.ui.Button(
                label="Download Challenge Files",
                url=challenge_url,
                style=discord.ButtonStyle.link,
                emoji="📥",
                row=0,
            )
            self.add_item(download_btn)
            self._children.sort(key=lambda item: 0 if getattr(item, "url", None) else 1)

    @discord.ui.button(
        label="Verify Flag",
        style=discord.ButtonStyle.success,
        emoji="🚩",
        custom_id="challenge_verify_flag",
        row=0,
    )
    async def verify_flag_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        # Quick server-side check before showing modal
        settings = await run_db(_get_challenge_settings_worker, str(interaction.guild_id))
        if not settings.get("enabled"):
            await interaction.response.send_message(
                "Challenges are currently disabled.", ephemeral=True
            )
            return

        await interaction.response.send_modal(VerifyFlagModal(self.cog))


# ---------------------------------------------------------------------------
# Admin Modals and Views
# ---------------------------------------------------------------------------

class CategorySelect(discord.ui.Select):
    """Dropdown menu for selecting challenge category."""

    def __init__(self, selected: str = "General"):
        options = [
            discord.SelectOption(label="General", description="General CTF Challenge", emoji="🎯", default=(selected == "General")),
            discord.SelectOption(label="Web", description="Web Security / Exploitation", emoji="🌐", default=(selected == "Web")),
            discord.SelectOption(label="Crypto", description="Cryptography & Ciphers", emoji="🔐", default=(selected == "Crypto")),
            discord.SelectOption(label="Reverse Engineering", description="Reverse Engineering / Binaries", emoji="⚙️", default=(selected == "Reverse Engineering")),
            discord.SelectOption(label="Forensics", description="Digital Forensics & Memory Analysis", emoji="🔍", default=(selected == "Forensics")),
            discord.SelectOption(label="Pwn", description="Binary Exploitation / Memory Corruption", emoji="💥", default=(selected == "Pwn")),
            discord.SelectOption(label="OSINT", description="Open Source Intelligence", emoji="🕵️", default=(selected == "OSINT")),
            discord.SelectOption(label="Misc", description="Miscellaneous Challenges", emoji="🧩", default=(selected == "Misc")),
        ]
        super().__init__(
            placeholder="Select Challenge Category (Dropdown)...",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_category = self.values[0]
        for opt in self.options:
            opt.default = (opt.value == self.values[0])
        await interaction.response.edit_message(view=self.view)


class SetChallengeModal(discord.ui.Modal, title="Set CTF Challenge"):
    def __init__(
        self,
        cog: "ChallengesCog",
        default_category: str | None = None,
        default_role_id: str | None = None,
    ):
        super().__init__()
        self.cog = cog
        self.name = discord.ui.TextInput(
            label="Challenge Name",
            placeholder="e.g. Cipherfall",
            required=True,
            max_length=100,
        )
        self.category = discord.ui.TextInput(
            label="Category",
            placeholder="e.g. Web, Crypto, Reverse, Forensics, Pwn, OSINT",
            required=False,
            max_length=50,
            default=default_category or "General",
        )
        self.url = discord.ui.TextInput(
            label="Challenge ZIP URL",
            placeholder="https://example.com/challenges/cipherfall.zip",
            required=True,
            max_length=500,
        )
        self.flag = discord.ui.TextInput(
            label="Original Flag",
            placeholder="e.g. CTF{secret_flag_here}",
            required=True,
            max_length=200,
        )
        self.role = discord.ui.TextInput(
            label="Ping Role (Optional ID or @Role)",
            placeholder="Paste Role ID or @Role to tag (optional)",
            required=False,
            max_length=100,
            default=str(default_role_id) if default_role_id else "",
        )
        self.add_item(self.name)
        self.add_item(self.category)
        self.add_item(self.url)
        self.add_item(self.flag)
        self.add_item(self.role)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        name = (self.name.value or "").strip()
        category = (self.category.value or "").strip() or "General"
        url = (self.url.value or "").strip()
        flag = (self.flag.value or "").strip()
        role_raw = (self.role.value or "").strip()

        if not name or not url or not flag:
            await interaction.followup.send(
                "Challenge Name, URL, and Flag are required.", ephemeral=True
            )
            return

        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            await interaction.followup.send(
                "Invalid Challenge URL. Please provide a valid HTTP or HTTPS link.",
                ephemeral=True,
            )
            return

        role_id_to_tag = None
        if role_raw:
            role_match = re.match(r"^<@&?(\d+)>$", role_raw)
            if role_match:
                role_id_to_tag = int(role_match.group(1))
            elif role_raw.isdigit():
                role_id_to_tag = int(role_raw)
            else:
                clean_name = role_raw.lstrip("@").strip().lower()
                matched_role = discord.utils.find(
                    lambda r: r.name.lower() == clean_name,
                    interaction.guild.roles,
                )
                if matched_role:
                    role_id_to_tag = matched_role.id

            if not role_id_to_tag or not interaction.guild.get_role(role_id_to_tag):
                await interaction.followup.send(
                    f"❌ Could not find a role matching `{role_raw}` in this server. Please provide a valid Role ID or mention.",
                    ephemeral=True,
                )
                return

        # Store SHA-256 hash only. Never store or log plaintext flag.
        flag_hash = hashlib.sha256(flag.encode("utf-8")).hexdigest()

        err, channel_mention = await self.cog.publish_new_challenge(
            interaction.guild,
            name,
            url,
            flag_hash,
            category=category,
            role_id=role_id_to_tag,
        )
        if err:
            await interaction.followup.send(f"❌ Failed to publish challenge: {err}", ephemeral=True)
            return

        tag_info = f" (tagged <@&{role_id_to_tag}>)" if role_id_to_tag else ""
        await interaction.followup.send(
            f"✅ Challenge **{name}** [{category}] published successfully in {channel_mention}!{tag_info}",
            ephemeral=True,
        )


class SendWriteupContentModal(discord.ui.Modal, title="Send Writeup — Markdown"):
    def __init__(self, cog: "ChallengesCog"):
        super().__init__()
        self.cog = cog
        self.content = discord.ui.TextInput(
            label="Writeup Content (Markdown)",
            style=discord.TextStyle.paragraph,
            placeholder="# Challenge Solution\n\nWalkthrough steps, payloads, explanations...",
            required=True,
            max_length=4000,
        )
        self.add_item(self.content)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        text = self.content.value or ""
        err, channel_mention = await self.cog.publish_writeup(
            interaction.guild, content=text, attachment=None
        )
        if err:
            await interaction.followup.send(f"❌ Failed to send writeup: {err}", ephemeral=True)
            return

        await interaction.followup.send(
            f"✅ Writeup published successfully in {channel_mention}!", ephemeral=True
        )


class ChallengesAdminView(discord.ui.View):
    """Admin-only interactive management panel for Challenges."""

    def __init__(self, cog: "ChallengesCog"):
        super().__init__(timeout=300)
        self.cog = cog
        self.selected_category = "General"
        self.add_item(CategorySelect(self.selected_category))

    async def _admin_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Only administrators can use challenge management actions.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Set Challenge", style=discord.ButtonStyle.primary, emoji="🎯", row=1)
    async def set_challenge_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._admin_check(interaction):
            return
        await interaction.response.send_modal(
            SetChallengeModal(self.cog, default_category=self.selected_category)
        )

    @discord.ui.button(label="Send Writeup", style=discord.ButtonStyle.secondary, emoji="📄", row=1)
    async def send_writeup_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._admin_check(interaction):
            return
        await interaction.response.send_modal(SendWriteupContentModal(self.cog))

    @discord.ui.button(label="Status & Leaderboard", style=discord.ButtonStyle.secondary, emoji="📊", row=1)
    async def status_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._admin_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        embed = await self.cog.get_status_embed(interaction.guild)
        await interaction.followup.send(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# Cog Implementation
# ---------------------------------------------------------------------------

class ChallengesCog(commands.Cog):
    """Cog handling CTF challenge publication, verification, and writeups."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Register persistent view so button interactions are routed across restarts
        self.bot.add_view(ChallengeVerificationPersistentView(self))

    async def send_admin_panel(self, interaction: discord.Interaction):
        """Sends the administrator challenges management view."""
        if not interaction.user.guild_permissions.administrator:
            if interaction.response.is_done():
                await interaction.followup.send("Only administrators can access this panel.", ephemeral=True)
            else:
                await interaction.response.send_message("Only administrators can access this panel.", ephemeral=True)
            return

        settings = await run_db(_get_challenge_settings_worker, str(interaction.guild_id))
        status_text = "Enabled" if settings.get("enabled") else "Disabled"
        ch_id = settings.get("announcement_channel_id")
        ch_mention = f"<#{ch_id}>" if ch_id else "*None configured*"

        embed = discord.Embed(
            title="Challenges Management Panel",
            description=(
                f"**System Status:** `{status_text}`\n"
                f"**Announcement Channel:** {ch_mention}\n\n"
                "Use the actions below to manage CTF challenges:\n"
                "• **Set Challenge** — Publish a new challenge and reset the leaderboard\n"
                "• **Send Writeup** — Publish the official markdown writeup\n"
                "• **Status & Leaderboard** — View the active challenge standings"
            ),
            color=discord.Color.blurple(),
        )

        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, view=ChallengesAdminView(self), ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, view=ChallengesAdminView(self), ephemeral=True)

    async def update_announcement_leaderboard(
        self,
        guild: discord.Guild,
        channel_id: str | None,
        message_id: str | None,
        challenge_name: str,
        challenge_url: str,
        winners: list[tuple[int, int]],
        category: str = "General",
    ):
        """Updates the original challenge announcement message's leaderboard."""
        if not channel_id or not message_id:
            return

        try:
            channel = guild.get_channel(int(channel_id))
            if not channel:
                try:
                    channel = await guild.fetch_channel(int(channel_id))
                except Exception:
                    channel = None

            if not isinstance(channel, discord.TextChannel):
                return

            try:
                msg = await channel.fetch_message(int(message_id))
            except (discord.NotFound, discord.Forbidden):
                return

            embed = build_challenge_embed(challenge_name, challenge_url, winners, category=category)
            view = ChallengeVerificationPersistentView(self, challenge_url=challenge_url)
            await msg.edit(embed=embed, view=view)
        except Exception as exc:
            logging.warning("Could not update challenge announcement %s: %s", message_id, exc)

    async def publish_new_challenge(
        self,
        guild: discord.Guild,
        name: str,
        url: str,
        flag_hash: str,
        category: str = "General",
        role_id: int | str | None = None,
    ) -> tuple[str | None, str | None]:
        """Publishes a new challenge and handles previous announcement replacement."""
        guild_id = str(guild.id)
        settings = await run_db(_get_challenge_settings_worker, guild_id)

        if not settings.get("enabled"):
            return "Challenges feature is currently disabled. Please enable it in /setting first.", None

        channel_id = settings.get("announcement_channel_id")
        if not channel_id:
            return "No announcement channel is configured. Please select one in /setting.", None

        channel = guild.get_channel(int(channel_id))
        if not channel:
            try:
                channel = await guild.fetch_channel(int(channel_id))
            except Exception:
                channel = None

        if not isinstance(channel, discord.TextChannel):
            return "The configured announcement channel could not be found or is inaccessible.", None

        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages):
            return f"I lack permission to view and post messages in {channel.mention}.", None

        # 1. Clean up previous challenge announcement if recorded
        prev_pub = await run_db(_get_latest_publication_worker, guild_id, "challenge")
        if prev_pub and prev_pub.get("message_id"):
            try:
                prev_ch = guild.get_channel(int(prev_pub["channel_id"]))
                if not prev_ch:
                    try:
                        prev_ch = await guild.fetch_channel(int(prev_pub["channel_id"]))
                    except Exception:
                        prev_ch = None
                if prev_ch:
                    prev_msg = await prev_ch.fetch_message(int(prev_pub["message_id"]))
                    await prev_msg.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                logging.info("Previous challenge announcement cleanup handled: %s", exc)

        # 2. Store new challenge in DB (marks previous inactive)
        new_ch = await run_db(
            _create_challenge_worker,
            guild_id,
            name,
            url,
            flag_hash,
            category,
            str(role_id) if role_id else None,
        )

        # 3. Post public announcement embed with persistent Verify Flag button and Download link button
        embed = build_challenge_embed(name, url, winners=[], category=category)
        view = ChallengeVerificationPersistentView(self, challenge_url=url)
        content = f"<@&{role_id}>" if role_id else None
        try:
            msg = await channel.send(
                content=content,
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions(roles=True),
            )
        except Exception as exc:
            return f"Failed to send announcement message: {exc}", None

        # 4. Save message and publication reference
        await run_db(
            _record_announcement_message_worker,
            new_ch["id"],
            guild_id,
            str(channel.id),
            str(msg.id),
        )
        return None, channel.mention

    async def publish_writeup(
        self,
        guild: discord.Guild,
        content: str | None = None,
        attachment: discord.Attachment | None = None,
    ) -> tuple[str | None, str | None]:
        """Publishes a challenge writeup and replaces any previous writeup."""
        guild_id = str(guild.id)
        settings = await run_db(_get_challenge_settings_worker, guild_id)

        if not settings.get("enabled"):
            return "Challenges feature is currently disabled.", None

        channel_id = settings.get("announcement_channel_id")
        if not channel_id:
            return "No announcement channel is configured.", None

        channel = guild.get_channel(int(channel_id))
        if not channel:
            try:
                channel = await guild.fetch_channel(int(channel_id))
            except Exception:
                channel = None

        if not isinstance(channel, discord.TextChannel):
            return "The configured announcement channel could not be found.", None

        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages):
            return f"I lack permission to post in {channel.mention}.", None

        active_data = await run_db(_get_active_challenge_worker, guild_id)
        if not active_data or not active_data.get("challenge"):
            return "There is no active challenge to publish a writeup for.", None

        challenge = active_data["challenge"]

        if not content and not attachment:
            return "Please provide markdown content or a markdown file attachment.", None

        file_to_send = None
        description_text = ""

        if attachment:
            # Validate attachment format and size
            if not attachment.filename.lower().endswith(".md"):
                return "The writeup attachment must be a Markdown file with a `.md` extension.", None
            if attachment.size > 8 * 1024 * 1024:
                return "The writeup attachment exceeds the 8MB size limit.", None

            file_bytes = await attachment.read()
            file_to_send = discord.File(io.BytesIO(file_bytes), filename=attachment.filename)
            description_text = (
                f"The official writeup for **{challenge['name']}** has been published!\n\n"
                "Review the full walkthrough in the attached Markdown file below."
            )
        elif content:
            # If markdown is very long (> 2000 chars), attach as .md file to avoid truncation
            if len(content) > 2000:
                safe_slug = re.sub(r"[^a-zA-Z0-9_\-]", "_", challenge["name"]).strip("_") or "challenge"
                file_to_send = discord.File(
                    io.BytesIO(content.encode("utf-8")),
                    filename=f"writeup_{safe_slug}.md",
                )
                description_text = (
                    f"The official writeup for **{challenge['name']}** has been published!\n\n"
                    "Because this writeup is comprehensive, the complete Markdown document is attached below."
                )
            else:
                description_text = content

        # 1. Delete previous writeup announcement if recorded (never touches challenge announcement)
        prev_writeup = await run_db(_get_latest_publication_worker, guild_id, "writeup")
        if prev_writeup and prev_writeup.get("message_id"):
            try:
                prev_ch = guild.get_channel(int(prev_writeup["channel_id"]))
                if not prev_ch:
                    try:
                        prev_ch = await guild.fetch_channel(int(prev_writeup["channel_id"]))
                    except Exception:
                        prev_ch = None
                if prev_ch:
                    prev_msg = await prev_ch.fetch_message(int(prev_writeup["message_id"]))
                    await prev_msg.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                logging.info("Previous writeup announcement cleanup handled: %s", exc)

        # 2. Publish writeup with leaderboard and category
        winners = active_data.get("winners", [])
        total_solvers = active_data.get("total_solvers", len(winners))
        category = challenge.get("category", "General")
        embed = build_writeup_embed(
            challenge["name"],
            description_text,
            winners=winners,
            category=category,
            total_solvers=total_solvers,
        )
        try:
            if file_to_send:
                msg = await channel.send(embed=embed, file=file_to_send)
            else:
                msg = await channel.send(embed=embed)
        except Exception as exc:
            return f"Failed to post writeup to announcement channel: {exc}", None

        # 3. Record writeup publication ID
        await run_db(
            _record_writeup_publication_worker,
            challenge["id"],
            guild_id,
            str(channel.id),
            str(msg.id),
        )
        return None, channel.mention

    async def get_status_embed(self, guild: discord.Guild) -> discord.Embed:
        """Returns status embed for active challenge and leaderboard."""
        guild_id = str(guild.id)
        settings = await run_db(_get_challenge_settings_worker, guild_id)
        active_data = await run_db(_get_active_challenge_worker, guild_id)

        enabled_str = "Enabled" if settings.get("enabled") else "Disabled"
        ch_id = settings.get("announcement_channel_id")
        ch_str = f"<#{ch_id}>" if ch_id else "None"

        embed = discord.Embed(
            title="Challenges Status",
            color=discord.Color.blurple(),
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.add_field(name="Feature Enabled", value=enabled_str, inline=True)
        embed.add_field(name="Announcement Channel", value=ch_str, inline=True)

        if not active_data or not active_data.get("challenge"):
            embed.add_field(name="Active Challenge", value="None", inline=False)
            return embed

        ch = active_data["challenge"]
        winners = active_data.get("winners", [])
        winner_map = {rank: uid for rank, uid in winners}

        b1 = f"<@{winner_map[1]}>" if 1 in winner_map else "Available"
        b2 = f"<@{winner_map[2]}>" if 2 in winner_map else "Available"
        b3 = f"<@{winner_map[3]}>" if 3 in winner_map else "Available"

        cat_str = ch.get("category", "General")
        embed.add_field(
            name="Active Challenge",
            value=(
                f"**Name:** {ch['name']}\n"
                f"**Category:** `{cat_str}`\n"
                f"[Challenge Link]({ch['url']})"
            ),
            inline=False,
        )
        embed.add_field(
            name="Current Leaderboard",
            value=(
                f"🥇 First Blood: {b1}\n"
                f"🥈 Second Blood: {b2}\n"
                f"🥉 Third Blood: {b3}"
            ),
            inline=False,
        )
        return embed

    # -----------------------------------------------------------------------
    # Admin Application Commands
    # -----------------------------------------------------------------------

    challenge_admin = app_commands.Group(
        name="challenge-admin",
        description="Admin commands for managing CTF challenges",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    @challenge_admin.command(name="panel", description="Admin: Open the challenges management panel")
    @app_commands.checks.has_permissions(administrator=True)
    async def panel_cmd(self, interaction: discord.Interaction):
        await self.send_admin_panel(interaction)

    @challenge_admin.command(name="set", description="Admin: Set and publish a new CTF challenge")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.choices(
        category=[
            app_commands.Choice(name="General", value="General"),
            app_commands.Choice(name="Web Exploitation", value="Web"),
            app_commands.Choice(name="Cryptography", value="Crypto"),
            app_commands.Choice(name="Reverse Engineering", value="Reverse Engineering"),
            app_commands.Choice(name="Forensics", value="Forensics"),
            app_commands.Choice(name="Binary Exploitation / Pwn", value="Pwn"),
            app_commands.Choice(name="OSINT", value="OSINT"),
            app_commands.Choice(name="Miscellaneous", value="Misc"),
        ]
    )
    @app_commands.describe(
        category="Optional: Challenge category selection",
        ping_role="Optional: Role to ping/tag upon challenge publication",
    )
    async def set_cmd(
        self,
        interaction: discord.Interaction,
        category: app_commands.Choice[str] | None = None,
        ping_role: discord.Role | None = None,
    ):
        cat_val = category.value if category else None
        role_val = str(ping_role.id) if ping_role else None
        await interaction.response.send_modal(
            SetChallengeModal(self, default_category=cat_val, default_role_id=role_val)
        )

    @challenge_admin.command(
        name="writeup",
        description="Admin: Publish the writeup for the active challenge (text or file)",
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        content="Optional: Markdown writeup text",
        file="Optional: Markdown (.md) writeup file attachment",
    )
    async def writeup_cmd(
        self,
        interaction: discord.Interaction,
        content: str | None = None,
        file: discord.Attachment | None = None,
    ):
        if not content and not file:
            await interaction.response.send_modal(SendWriteupContentModal(self))
            return

        await interaction.response.defer(ephemeral=True)
        err, ch_mention = await self.publish_writeup(interaction.guild, content=content, attachment=file)
        if err:
            await interaction.followup.send(f"❌ {err}", ephemeral=True)
        else:
            await interaction.followup.send(f"✅ Writeup published in {ch_mention}!", ephemeral=True)

    @challenge_admin.command(name="status", description="Admin: View active challenge and leaderboard")
    @app_commands.checks.has_permissions(administrator=True)
    async def status_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        embed = await self.get_status_embed(interaction.guild)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ChallengesCog(bot))
