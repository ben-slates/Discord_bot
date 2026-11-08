"""Comprehensive tests for the CTF Challenges management system."""

import io
import hashlib
import datetime
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import discord

from database import (
    SessionLocal,
    GuildConfig,
    UserData,
    ChallengeSettings,
    Challenge,
    ChallengeWinner,
    ChallengePublication,
)
from cogs.custom_lb import (
    _enable_feature_worker,
    _disable_feature_worker,
    _feature_test_config_worker,
)
from cogs.challenges import (
    ChallengesCog,
    ChallengeVerificationPersistentView,
    ChallengesAdminView,
    _get_challenge_settings_worker,
    _create_challenge_worker,
    _record_announcement_message_worker,
    _record_writeup_publication_worker,
    _get_latest_publication_worker,
    _get_active_challenge_worker,
    _solve_flag_worker,
    build_challenge_embed,
    build_writeup_embed,
)


def _cleanup_guild(guild_id: str):
    db = SessionLocal()
    try:
        challenges = db.query(Challenge).filter_by(guild_id=guild_id).all()
        for ch in challenges:
            db.query(ChallengeWinner).filter_by(challenge_id=ch.id).delete()
            db.query(ChallengePublication).filter_by(challenge_id=ch.id).delete()
        db.query(Challenge).filter_by(guild_id=guild_id).delete()
        db.query(ChallengePublication).filter_by(guild_id=guild_id).delete()
        db.query(ChallengeSettings).filter_by(guild_id=guild_id).delete()
        db.query(GuildConfig).filter_by(guild_id=guild_id).delete()
        db.query(UserData).filter_by(guild_id=guild_id).delete()
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Phase 2 Tests: Settings & Admin Toggle
# ---------------------------------------------------------------------------

def test_challenges_disabled_by_default():
    guild_id = "test_guild_ch_default_991"
    _cleanup_guild(guild_id)

    db = SessionLocal()
    try:
        cfg = GuildConfig(guild_id=guild_id)
        db.add(cfg)
        db.commit()
    finally:
        db.close()

    settings = _get_challenge_settings_worker(guild_id)
    assert settings["enabled"] is False
    assert settings["announcement_channel_id"] is None
    _cleanup_guild(guild_id)


def test_enable_and_disable_challenges_setting():
    guild_id = "test_guild_ch_enable_992"
    channel_id = 1234567890
    _cleanup_guild(guild_id)

    label, err = _enable_feature_worker(guild_id, "challenges", channel_id)
    assert err is None
    assert label == "Challenges"

    settings = _get_challenge_settings_worker(guild_id)
    assert settings["enabled"] is True
    assert settings["announcement_channel_id"] == str(channel_id)

    test_cfg = _feature_test_config_worker(guild_id, "challenges")
    assert test_cfg == ("Challenges", str(channel_id))

    label, err = _disable_feature_worker(guild_id, "challenges")
    assert err is None
    assert label == "Challenges"

    settings_disabled = _get_challenge_settings_worker(guild_id)
    assert settings_disabled["enabled"] is False
    _cleanup_guild(guild_id)


# ---------------------------------------------------------------------------
# Phase 3 Tests: Challenge Creation & Announcements
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_publish_challenge_and_embed_structure():
    guild_id = "993001"
    channel_id = 987654321
    _cleanup_guild(guild_id)

    _enable_feature_worker(guild_id, "challenges", channel_id)

    bot = MagicMock()
    cog = ChallengesCog(bot)

    mock_channel = MagicMock(spec=discord.TextChannel)
    mock_channel.id = channel_id
    mock_channel.mention = f"<#{channel_id}>"
    mock_channel.permissions_for = MagicMock(
        return_value=SimpleNamespace(view_channel=True, send_messages=True)
    )

    sent_message = MagicMock(spec=discord.Message)
    sent_message.id = 555666777
    mock_channel.send = AsyncMock(return_value=sent_message)

    mock_guild = MagicMock(spec=discord.Guild)
    mock_guild.id = int(guild_id)
    mock_guild.me = MagicMock()
    mock_guild.get_channel = MagicMock(return_value=mock_channel)

    flag_plain = "CTF{super_secret_flag_123}"
    flag_hash = hashlib.sha256(flag_plain.encode("utf-8")).hexdigest()

    err, ch_mention = await cog.publish_new_challenge(
        mock_guild, "Cipherfall", "https://ctf.rynex.com/cipherfall", flag_hash
    )
    assert err is None
    assert ch_mention == mock_channel.mention

    # Verify embed details
    mock_channel.send.assert_awaited_once()
    call_kwargs = mock_channel.send.call_args.kwargs
    embed = call_kwargs["embed"]
    view = call_kwargs["view"]

    assert "Cipherfall" in embed.description
    assert "https://ctf.rynex.com/cipherfall" in embed.description

    # Verify password copy box is in embed
    pw_fields = [f for f in embed.fields if "Password" in f.name]
    assert len(pw_fields) == 1
    assert "```rynextechnologies```" in pw_fields[0].value

    assert isinstance(view, ChallengeVerificationPersistentView)
    # Check Download button and Verify Flag button
    download_btn = [c for c in view.children if getattr(c, "url", None) == "https://ctf.rynex.com/cipherfall"]
    verify_btn = [c for c in view.children if getattr(c, "custom_id", None) == "challenge_verify_flag"]
    assert len(download_btn) == 1
    assert download_btn[0].label == "Download Challenge Files"
    assert len(verify_btn) == 1

    # Verify saved state in DB
    active_data = _get_active_challenge_worker(guild_id)
    assert active_data is not None
    assert active_data["challenge"]["name"] == "Cipherfall"
    assert active_data["challenge"]["announcement_message_id"] == str(sent_message.id)
    assert active_data["challenge"]["flag_hash"] == flag_hash
    assert len(active_data["winners"]) == 0

    _cleanup_guild(guild_id)


# ---------------------------------------------------------------------------
# Phase 4 Tests: Flag Verification & Leaderboard Atomic Assignment
# ---------------------------------------------------------------------------

def test_flag_verification_full_cycle():
    guild_id = "test_guild_ch_solve_994"
    channel_id = 111222333
    _cleanup_guild(guild_id)

    _enable_feature_worker(guild_id, "challenges", channel_id)

    correct_flag = "CTF{crypt0_m4st3r_2026}"
    flag_hash = hashlib.sha256(correct_flag.encode("utf-8")).hexdigest()

    ch = _create_challenge_worker(guild_id, "CryptoMaster", "https://ctf.rynex.com/crypto", flag_hash)
    _record_announcement_message_worker(ch["id"], guild_id, str(channel_id), "888888")

    # 1. Incorrect flag is rejected
    res_bad = _solve_flag_worker(guild_id, 1001, "CTF{wrong_flag}")
    assert res_bad["status"] == "incorrect"

    # 2. First solver gets First Blood (rank 1) and 110 XP (50 + 60)
    res_u1 = _solve_flag_worker(guild_id, 1001, correct_flag)
    assert res_u1["status"] == "success"
    assert res_u1["rank"] == 1
    assert res_u1["xp_awarded"] == 110
    assert len(res_u1["winners"]) == 1

    # 3. Duplicate submission by User 1 is rejected and awards no extra XP
    res_u1_dup = _solve_flag_worker(guild_id, 1001, correct_flag)
    assert res_u1_dup["status"] == "already_solved"
    assert res_u1_dup["rank"] == 1

    # 4. Second solver gets Second Blood (rank 2) and 80 XP (50 + 30)
    res_u2 = _solve_flag_worker(guild_id, 1002, correct_flag)
    assert res_u2["status"] == "success"
    assert res_u2["rank"] == 2
    assert res_u2["xp_awarded"] == 80
    assert len(res_u2["winners"]) == 2

    # 5. Third solver gets Third Blood (rank 3) and 65 XP (50 + 15)
    res_u3 = _solve_flag_worker(guild_id, 1003, correct_flag)
    assert res_u3["status"] == "success"
    assert res_u3["rank"] == 3
    assert res_u3["xp_awarded"] == 65
    assert len(res_u3["winners"]) == 3

    # 6. Fourth and subsequent solvers get acknowledged, receive base 50 XP, but do NOT enter top-3 leaderboard
    res_u4 = _solve_flag_worker(guild_id, 1004, correct_flag)
    assert res_u4["status"] == "late_solve"
    assert res_u4["xp_awarded"] == 50

    # Duplicate late solver submission is rejected and gives no duplicate XP
    res_u4_dup = _solve_flag_worker(guild_id, 1004, correct_flag)
    assert res_u4_dup["status"] == "already_solved"

    res_u5 = _solve_flag_worker(guild_id, 1005, correct_flag)
    assert res_u5["status"] == "late_solve"
    assert res_u5["xp_awarded"] == 50

    # Verify XP balances in UserData database
    db = SessionLocal()
    try:
        from database import UserData
        u1_rec = db.query(UserData).filter_by(guild_id=guild_id, user_id=1001).first()
        u2_rec = db.query(UserData).filter_by(guild_id=guild_id, user_id=1002).first()
        u3_rec = db.query(UserData).filter_by(guild_id=guild_id, user_id=1003).first()
        u4_rec = db.query(UserData).filter_by(guild_id=guild_id, user_id=1004).first()
        u5_rec = db.query(UserData).filter_by(guild_id=guild_id, user_id=1005).first()

        assert u1_rec.xp == 110  # 50 + 60
        assert u2_rec.xp == 80   # 50 + 30
        assert u3_rec.xp == 65   # 50 + 15
        assert u4_rec.xp == 50   # 50
        assert u5_rec.xp == 50   # 50
    finally:
        db.close()

    # Check database state
    active = _get_active_challenge_worker(guild_id)
    assert len(active["winners"]) == 3
    assert active["winners"][0] == (1, 1001)
    assert active["winners"][1] == (2, 1002)
    assert active["winners"][2] == (3, 1003)

    _cleanup_guild(guild_id)


def test_concurrency_safe_rank_assignment():
    guild_id = "test_guild_ch_concurrent_995"
    channel_id = 444555666
    _cleanup_guild(guild_id)

    _enable_feature_worker(guild_id, "challenges", channel_id)

    flag = "CTF{concurrency_test_flag}"
    flag_hash = hashlib.sha256(flag.encode("utf-8")).hexdigest()
    ch = _create_challenge_worker(guild_id, "RaceCondition", "https://ctf.rynex.com/race", flag_hash)

    # Simulate 10 users attempting to solve simultaneously
    user_ids = list(range(2001, 2011))

    def solve_worker(uid):
        return uid, _solve_flag_worker(guild_id, uid, flag)

    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(solve_worker, user_ids))

    successes = [r for uid, r in results if r["status"] == "success"]
    late_solves = [r for uid, r in results if r["status"] == "late_solve"]

    # Exactly 3 users must receive ranks 1, 2, 3
    assert len(successes) == 3
    ranks = sorted([s["rank"] for s in successes])
    assert ranks == [1, 2, 3]

    # Remaining 7 users must receive late_solve
    assert len(late_solves) == 7

    _cleanup_guild(guild_id)


# ---------------------------------------------------------------------------
# Phase 6 & 7 Tests: Writeups, Replacements, and Cleanup
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_publish_writeup_text_and_attachment():
    guild_id = "996001"
    channel_id = 777888999
    _cleanup_guild(guild_id)

    _enable_feature_worker(guild_id, "challenges", channel_id)

    bot = MagicMock()
    cog = ChallengesCog(bot)

    mock_channel = MagicMock(spec=discord.TextChannel)
    mock_channel.id = channel_id
    mock_channel.mention = f"<#{channel_id}>"
    mock_channel.permissions_for = MagicMock(
        return_value=SimpleNamespace(view_channel=True, send_messages=True)
    )

    sent_msg = MagicMock(spec=discord.Message)
    sent_msg.id = 90001
    mock_channel.send = AsyncMock(return_value=sent_msg)

    mock_guild = MagicMock(spec=discord.Guild)
    mock_guild.id = int(guild_id)
    mock_guild.me = MagicMock()
    mock_guild.get_channel = MagicMock(return_value=mock_channel)

    # Active challenge
    flag_hash = hashlib.sha256(b"flag").hexdigest()
    _create_challenge_worker(guild_id, "WebExploit", "https://ctf.rynex.com/web", flag_hash)

    # 1. Short Markdown text
    err, ch_mention = await cog.publish_writeup(mock_guild, content="# Short Writeup\nUse SQLi on id.")
    assert err is None
    assert ch_mention == mock_channel.mention
    mock_channel.send.assert_awaited()
    call_args = mock_channel.send.call_args
    assert "file" not in call_args.kwargs or call_args.kwargs.get("file") is None

    # 2. Long Markdown (>2000 chars) automatically attaches .md file
    long_content = "# Long Solution\n" + ("A" * 2500)
    mock_channel.send.reset_mock()
    sent_msg_2 = MagicMock(spec=discord.Message)
    sent_msg_2.id = 90002
    mock_channel.send = AsyncMock(return_value=sent_msg_2)

    err, ch_mention = await cog.publish_writeup(mock_guild, content=long_content)
    assert err is None
    call_args_2 = mock_channel.send.call_args
    assert "file" in call_args_2.kwargs
    attached_file = call_args_2.kwargs["file"]
    assert isinstance(attached_file, discord.File)
    assert attached_file.filename.endswith(".md")

    # 3. File attachment
    mock_attachment = MagicMock(spec=discord.Attachment)
    mock_attachment.filename = "solution.md"
    mock_attachment.size = 1024
    mock_attachment.read = AsyncMock(return_value=b"# Uploaded solution content")

    mock_channel.send.reset_mock()
    sent_msg_3 = MagicMock(spec=discord.Message)
    sent_msg_3.id = 90003
    mock_channel.send = AsyncMock(return_value=sent_msg_3)

    err, ch_mention = await cog.publish_writeup(mock_guild, attachment=mock_attachment)
    assert err is None
    assert "file" in mock_channel.send.call_args.kwargs

    # 4. Reject invalid attachment extension
    mock_invalid_attachment = MagicMock(spec=discord.Attachment)
    mock_invalid_attachment.filename = "malicious.exe"
    mock_invalid_attachment.size = 1024

    err_invalid, _ = await cog.publish_writeup(mock_guild, attachment=mock_invalid_attachment)
    assert err_invalid is not None
    assert "Markdown file" in err_invalid

    _cleanup_guild(guild_id)


@pytest.mark.asyncio
async def test_message_replacement_and_isolation():
    """Verify that replacing a challenge only deletes the previous challenge announcement,
    and replacing a writeup only deletes the previous writeup announcement."""
    guild_id = "997001"
    channel_id = 666777888
    _cleanup_guild(guild_id)

    _enable_feature_worker(guild_id, "challenges", channel_id)

    bot = MagicMock()
    cog = ChallengesCog(bot)

    mock_channel = MagicMock(spec=discord.TextChannel)
    mock_channel.id = channel_id
    mock_channel.mention = f"<#{channel_id}>"
    mock_channel.permissions_for = MagicMock(
        return_value=SimpleNamespace(view_channel=True, send_messages=True)
    )

    msg_ch1 = MagicMock(spec=discord.Message, id=10001)
    msg_ch1.delete = AsyncMock()

    msg_ch2 = MagicMock(spec=discord.Message, id=10002)
    msg_ch2.delete = AsyncMock()

    msg_w1 = MagicMock(spec=discord.Message, id=20001)
    msg_w1.delete = AsyncMock()

    msg_w2 = MagicMock(spec=discord.Message, id=20002)
    msg_w2.delete = AsyncMock()

    # Channel fetch_message routing
    async def fake_fetch_message(mid):
        if mid == 10001:
            return msg_ch1
        if mid == 10002:
            return msg_ch2
        if mid == 20001:
            return msg_w1
        if mid == 20002:
            return msg_w2
        raise discord.NotFound(MagicMock(), "Not Found")

    mock_channel.fetch_message = AsyncMock(side_effect=fake_fetch_message)

    mock_guild = MagicMock(spec=discord.Guild)
    mock_guild.id = int(guild_id)
    mock_guild.me = MagicMock()
    mock_guild.get_channel = MagicMock(return_value=mock_channel)

    flag_hash = hashlib.sha256(b"flag").hexdigest()

    # Step 1: Publish Challenge 1 (ID 10001)
    mock_channel.send = AsyncMock(return_value=msg_ch1)
    await cog.publish_new_challenge(mock_guild, "Challenge 1", "https://ctf.rynex.com/1", flag_hash)
    msg_ch1.delete.assert_not_called()

    # Step 2: Publish Writeup 1 (ID 20001)
    mock_channel.send = AsyncMock(return_value=msg_w1)
    await cog.publish_writeup(mock_guild, content="# Writeup 1")
    # Verify Challenge 1 announcement was NOT deleted!
    msg_ch1.delete.assert_not_called()
    msg_w1.delete.assert_not_called()

    # Step 3: Publish Challenge 2 (ID 10002)
    mock_channel.send = AsyncMock(return_value=msg_ch2)
    await cog.publish_new_challenge(mock_guild, "Challenge 2", "https://ctf.rynex.com/2", flag_hash)
    # Challenge 1 must be deleted! Writeup 1 must NOT be deleted!
    msg_ch1.delete.assert_awaited_once()
    msg_w1.delete.assert_not_called()

    # Step 4: Publish Writeup 2 (ID 20002)
    mock_channel.send = AsyncMock(return_value=msg_w2)
    await cog.publish_writeup(mock_guild, content="# Writeup 2")
    # Writeup 1 must be deleted! Challenge 2 must NOT be deleted!
    msg_w1.delete.assert_awaited_once()
    msg_ch2.delete.assert_not_called()

    _cleanup_guild(guild_id)


# ---------------------------------------------------------------------------
# Phase 9 Tests: Admin Authorization & Disabled State
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unauthorized_user_blocked():
    bot = MagicMock()
    cog = ChallengesCog(bot)
    view = ChallengesAdminView(cog)

    interaction = MagicMock(spec=discord.Interaction)
    interaction.user.guild_permissions.administrator = False
    interaction.response.send_message = AsyncMock()

    allowed = await view._admin_check(interaction)
    assert allowed is False
    interaction.response.send_message.assert_awaited_once_with(
        "Only administrators can use challenge management actions.", ephemeral=True
    )


def test_disabled_challenges_rejects_verification():
    guild_id = "test_guild_ch_dis_998"
    _cleanup_guild(guild_id)

    # Guild with challenges disabled
    db = SessionLocal()
    try:
        cfg = GuildConfig(guild_id=guild_id, challenges_enabled=False)
        db.add(cfg)
        ch = Challenge(
            guild_id=guild_id,
            name="Test",
            url="https://test.com",
            flag_hash="hash",
            is_active=True,
        )
        db.add(ch)
        db.commit()
    finally:
        db.close()

    res = _solve_flag_worker(guild_id, 9999, "any_flag")
    assert res["status"] == "disabled"
    _cleanup_guild(guild_id)


# ---------------------------------------------------------------------------
# Phase 10 Tests: Category, Role Ping, and Writeup Leaderboard Sharing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_challenge_category_and_role_ping():
    guild_id = "998001"
    channel_id = 987654322
    role_id = 1122334455
    _cleanup_guild(guild_id)

    _enable_feature_worker(guild_id, "challenges", channel_id)

    bot = MagicMock()
    cog = ChallengesCog(bot)

    mock_channel = MagicMock(spec=discord.TextChannel)
    mock_channel.id = channel_id
    mock_channel.mention = f"<#{channel_id}>"
    mock_channel.permissions_for = MagicMock(
        return_value=SimpleNamespace(view_channel=True, send_messages=True)
    )

    sent_message = MagicMock(spec=discord.Message, id=777888999)
    mock_channel.send = AsyncMock(return_value=sent_message)

    mock_role = MagicMock(spec=discord.Role, id=role_id)
    mock_role.mention = f"<@&{role_id}>"

    mock_guild = MagicMock(spec=discord.Guild)
    mock_guild.id = int(guild_id)
    mock_guild.me = MagicMock()
    mock_guild.get_channel = MagicMock(return_value=mock_channel)
    mock_guild.get_role = MagicMock(return_value=mock_role)

    flag_hash = hashlib.sha256(b"pwn_the_world").hexdigest()

    err, ch_mention = await cog.publish_new_challenge(
        mock_guild,
        name="BufferBuster",
        url="https://ctf.rynex.com/pwn1",
        flag_hash=flag_hash,
        category="Pwn",
        role_id=role_id,
    )
    assert err is None
    assert ch_mention == mock_channel.mention

    # Verify announcement message was sent with role tag in content and role allowed_mentions
    mock_channel.send.assert_awaited_once()
    kwargs = mock_channel.send.call_args.kwargs
    assert kwargs.get("content") == f"<@&{role_id}>"
    assert kwargs.get("allowed_mentions") is not None
    assert kwargs["allowed_mentions"].roles is True

    # Verify category is in embed description
    embed = kwargs["embed"]
    assert "**Category:** `Pwn`" in embed.description
    assert "**Challenge:** BufferBuster" in embed.description

    # Verify stored in DB
    active = _get_active_challenge_worker(guild_id)
    assert active is not None
    assert active["challenge"]["name"] == "BufferBuster"
    assert active["challenge"]["category"] == "Pwn"
    assert active["challenge"]["role_id"] == str(role_id)

    _cleanup_guild(guild_id)


@pytest.mark.asyncio
async def test_writeup_shares_challenge_leaderboard():
    guild_id = "998002"
    channel_id = 987654323
    _cleanup_guild(guild_id)

    _enable_feature_worker(guild_id, "challenges", channel_id)

    bot = MagicMock()
    cog = ChallengesCog(bot)

    mock_channel = MagicMock(spec=discord.TextChannel)
    mock_channel.id = channel_id
    mock_channel.mention = f"<#{channel_id}>"
    mock_channel.permissions_for = MagicMock(
        return_value=SimpleNamespace(view_channel=True, send_messages=True)
    )

    sent_msg = MagicMock(spec=discord.Message, id=888001)
    mock_channel.send = AsyncMock(return_value=sent_msg)

    mock_guild = MagicMock(spec=discord.Guild)
    mock_guild.id = int(guild_id)
    mock_guild.me = MagicMock()
    mock_guild.get_channel = MagicMock(return_value=mock_channel)

    flag = "CTF{web_sec_hero_2026}"
    flag_hash = hashlib.sha256(flag.encode("utf-8")).hexdigest()

    # Create challenge with Web category
    ch = _create_challenge_worker(
        guild_id, "InjectionZero", "https://ctf.rynex.com/web1", flag_hash, category="Web"
    )
    _record_announcement_message_worker(ch["id"], guild_id, str(channel_id), "777001")

    # User 3001 claims First Blood, User 3002 claims Second Blood
    res1 = _solve_flag_worker(guild_id, 3001, flag)
    assert res1["status"] == "success"
    assert res1["rank"] == 1

    res2 = _solve_flag_worker(guild_id, 3002, flag)
    assert res2["status"] == "success"
    assert res2["rank"] == 2

    # Publish writeup
    err, ch_mention = await cog.publish_writeup(
        mock_guild, content="# Official Solution\nExploit parameter `query` via blind boolean SQLi."
    )
    assert err is None
    assert ch_mention == mock_channel.mention

    # Verify writeup embed contains the leaderboard
    mock_channel.send.assert_awaited_once()
    kwargs = mock_channel.send.call_args.kwargs
    writeup_embed = kwargs["embed"]

    assert "Challenge Writeup: InjectionZero" in writeup_embed.title
    # Check category field
    cat_fields = [f for f in writeup_embed.fields if f.name == "Category"]
    assert len(cat_fields) == 1
    assert cat_fields[0].value == "`Web`"

    # Check leaderboard field
    lb_fields = [f for f in writeup_embed.fields if "Leaderboard" in f.name]
    assert len(lb_fields) == 1
    lb_val = lb_fields[0].value
    assert "<@3001>" in lb_val
    assert "<@3002>" in lb_val
    assert "First Blood" in lb_val
    assert "Second Blood" in lb_val
    assert "Third Blood" in lb_val
    assert "Available" in lb_val

    _cleanup_guild(guild_id)


def test_category_select_dropdown_in_admin_view():
    from cogs.challenges import CategorySelect

    bot = MagicMock()
    cog = ChallengesCog(bot)
    view = ChallengesAdminView(cog)

    assert view.selected_category == "General"

    # Find the CategorySelect item in the view
    selects = [item for item in view.children if isinstance(item, CategorySelect)]
    assert len(selects) == 1
    sel = selects[0]

    # Verify options contain CTF categories
    labels = [opt.label for opt in sel.options]
    assert "General" in labels
    assert "Web" in labels
    assert "Crypto" in labels
    assert "Reverse Engineering" in labels
    assert "Pwn" in labels
    assert "Forensics"

