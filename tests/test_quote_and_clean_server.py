import datetime
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from database import SessionLocal, GuildConfig, UserData, AttendanceLog
from cogs.custom_lb import _enable_feature_worker, _disable_feature_worker, _feature_test_config_worker
from cogs.notifications import run_clean_server, _set_last_quote


def test_quote_frequency_interval():
    now = datetime.datetime(2026, 10, 19, 8, 0, tzinfo=datetime.timezone.utc)
    today_date = now.date()

    def should_send_quote(last_date_str):
        if not last_date_str:
            return True
        last_date = datetime.date.fromisoformat(last_date_str)
        return (today_date - last_date).days >= 3

    assert should_send_quote(None) is True
    assert should_send_quote("") is True
    assert should_send_quote("2026-10-18") is False  # 1 day ago
    assert should_send_quote("2026-10-17") is False  # 2 days ago
    assert should_send_quote("2026-10-16") is True   # 3 days ago
    assert should_send_quote("2026-10-10") is True   # 9 days ago


def test_clean_server_enable_and_disable():
    guild_id = "test_guild_clean_server_123"
    channel_id = 987654321

    label, err = _enable_feature_worker(guild_id, "clean_server", channel_id)
    assert err is None
    assert label == "Clean Server"

    db = SessionLocal()
    try:
        config = db.query(GuildConfig).filter_by(guild_id=guild_id).first()
        assert config is not None
        assert config.clean_server_enabled is True
        assert config.clean_server_channel == str(channel_id)
        assert config.clean_server_activated_at is not None
    finally:
        db.close()

    result = _feature_test_config_worker(guild_id, "clean_server")
    assert result == ("Clean Server", str(channel_id))

    label, err = _disable_feature_worker(guild_id, "clean_server")
    assert err is None
    assert label == "Clean Server"

    db = SessionLocal()
    try:
        config = db.query(GuildConfig).filter_by(guild_id=guild_id).first()
        assert config.clean_server_enabled is False
        assert config.clean_server_channel is None
        assert config.clean_server_last_run is None
        assert config.clean_server_activated_at is None
    finally:
        db.close()


def test_clean_server_enable_without_channel():
    guild_id = "test_guild_clean_server_no_channel_456"

    # Enable clean_server with no channel (channel_id=None)
    label, err = _enable_feature_worker(guild_id, "clean_server", None)
    assert err is None
    assert label == "Clean Server"

    db = SessionLocal()
    try:
        config = db.query(GuildConfig).filter_by(guild_id=guild_id).first()
        assert config is not None
        assert config.clean_server_enabled is True
        assert config.clean_server_channel is None
    finally:
        db.close()

    # Testing config should indicate enabled server-wide ("server")
    result = _feature_test_config_worker(guild_id, "clean_server")
    assert result == ("Clean Server", "server")


@pytest.mark.asyncio
async def test_run_clean_server_inactive_detection():
    guild_id = "999"
    now = datetime.datetime(2026, 10, 19, 12, 0, tzinfo=datetime.timezone.utc)

    db = SessionLocal()
    try:
        # User who was active 5 days ago in DB
        db.query(UserData).filter_by(guild_id=guild_id).delete()
        active_user = UserData(
            guild_id=guild_id,
            user_id=1001,
            last_active_at=now - datetime.timedelta(days=5),
        )
        # User whose last activity in DB was 45 days ago
        old_user = UserData(
            guild_id=guild_id,
            user_id=1002,
            last_active_at=now - datetime.timedelta(days=45),
            updated_at=now - datetime.timedelta(days=45),
        )
        db.add_all([active_user, old_user])
        db.commit()
    finally:
        db.close()

    class MockRole:
        def __init__(self, position):
            self.position = position
        def __ge__(self, other):
            return self.position >= getattr(other, "position", 0)

    # Create mock members
    def make_member(user_id, name, bot=False, admin=False, role_pos=1, joined_days=60, status="offline", voice=False):
        m = MagicMock()
        m.id = user_id
        m.display_name = name
        m.bot = bot
        m.guild_permissions.administrator = admin
        m.top_role = MockRole(role_pos)
        m.joined_at = now - datetime.timedelta(days=joined_days)
        m.status = status
        m.raw_status = status
        m.voice = SimpleNamespace(channel=MagicMock() if voice else None)
        m.kick = AsyncMock()
        return m

    bot_me = SimpleNamespace(top_role=MockRole(10), guild_permissions=SimpleNamespace(kick_members=True))

    m_bot = make_member(9001, "BotUser", bot=True)
    m_owner = make_member(8001, "OwnerUser")
    m_admin = make_member(8002, "AdminUser", admin=True)
    m_high_role = make_member(8003, "HighRoleUser", role_pos=20)
    m_new = make_member(8004, "NewMember", joined_days=5)
    m_online = make_member(8005, "OnlineMember", status="online")
    m_voice = make_member(8006, "VoiceMember", voice=True)
    m_db_active = make_member(1001, "DbActiveMember")
    m_inactive = make_member(1002, "InactiveMember")  # Should be kicked!

    guild = MagicMock()
    guild.id = int(guild_id)
    guild.owner_id = 8001
    guild.me = bot_me
    guild.chunked = True
    guild.members = [m_bot, m_owner, m_admin, m_high_role, m_new, m_online, m_voice, m_db_active, m_inactive]
    guild.get_channel = MagicMock(return_value=None)

    config = SimpleNamespace(
        guild_id=str(guild.id),
        clean_server_enabled=True,
        clean_server_channel=None,
        bot_logs_channel=None,
        clean_server_activated_at=now - datetime.timedelta(days=35),
    )

    removed = await run_clean_server(guild, config, now=now)

    assert m_inactive in removed
    m_inactive.kick.assert_awaited_once_with(reason="Clean Server: Inactive for 30 consecutive days")

    # Ensure other members were NOT kicked
    for safe in [m_bot, m_owner, m_admin, m_high_role, m_new, m_online, m_voice, m_db_active]:
        safe.kick.assert_not_called()


@pytest.mark.asyncio
async def test_clean_server_no_kicks_before_30_days():
    guild_id = "888"
    now = datetime.datetime(2026, 10, 20, 12, 0, tzinfo=datetime.timezone.utc)

    # Inactive member who hasn't been active
    m_inactive = MagicMock()
    m_inactive.id = 7001
    m_inactive.name = "old_user"
    m_inactive.display_name = "Old User"
    m_inactive.bot = False
    m_inactive.guild_permissions.administrator = False
    m_inactive.top_role = MagicMock(position=1, __ge__=lambda self, o: False)
    m_inactive.joined_at = now - datetime.timedelta(days=100)
    m_inactive.status = "offline"
    m_inactive.voice = None
    m_inactive.kick = AsyncMock()

    guild = MagicMock()
    guild.id = int(guild_id)
    guild.owner_id = 9999
    guild.me = MagicMock(top_role=MagicMock(position=10))
    guild.chunked = True
    guild.members = [m_inactive]

    # Feature activated only 10 days ago!
    config = SimpleNamespace(
        guild_id=guild_id,
        clean_server_enabled=True,
        clean_server_channel=None,
        bot_logs_channel=None,
        clean_server_activated_at=now - datetime.timedelta(days=10),
    )

    removed = await run_clean_server(guild, config, now=now)

    # Must NOT remove anyone before 30 consecutive days from activation
    assert removed == []
    m_inactive.kick.assert_not_called()


@pytest.mark.asyncio
async def test_clean_server_multiple_messages_reporting():
    guild_id = "777"
    now = datetime.datetime(2026, 10, 20, 12, 0, tzinfo=datetime.timezone.utc)

    # Create 40 inactive members
    members = []
    for i in range(40):
        m = MagicMock()
        m.id = 8000 + i
        m.name = f"user_{i}"
        m.display_name = f"Display Name {i}"
        m.bot = False
        m.guild_permissions.administrator = False
        m.top_role = MagicMock(position=1, __ge__=lambda self, o: False)
        m.joined_at = now - datetime.timedelta(days=100)
        m.status = "offline"
        m.voice = None
        m.kick = AsyncMock()
        members.append(m)

    import discord
    target_channel = MagicMock(spec=discord.TextChannel)
    target_channel.send = AsyncMock()

    guild = MagicMock()
    guild.id = int(guild_id)
    guild.owner_id = 9999
    guild.me = MagicMock(top_role=MagicMock(position=10))
    guild.chunked = True
    guild.members = members
    guild.get_channel = MagicMock(return_value=target_channel)

    # Activated 35 days ago (>= 30 days)
    config = SimpleNamespace(
        guild_id=guild_id,
        clean_server_enabled=True,
        clean_server_channel="123456",
        bot_logs_channel=None,
        clean_server_activated_at=now - datetime.timedelta(days=35),
    )

    removed = await run_clean_server(guild, config, now=now)

    assert len(removed) == 40
    # The summary embed was sent, plus multiple message chunks
    assert target_channel.send.call_count >= 2

    # Verify that all 40 usernames and display names were sent across the messages
    all_sent_texts = " ".join([str(call.args[0]) for call in target_channel.send.call_args_list if call.args])
    for i in range(40):
        assert f"@user_{i}" in all_sent_texts
        assert f"Display Name {i}" in all_sent_texts


def test_no_legacy_brand_in_codebase():
    import subprocess
    target = "rynex" + "security"
    result = subprocess.run(
        ["grep", "-rnI", "-i", "--exclude-dir=tests", "--exclude-dir=.pytest_cache", "--exclude-dir=.git", target, "."],
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "", f"Found occurrences of {target}: {result.stdout}"


def test_certificate_feature_removed():
    from pathlib import Path
    from cogs.custom_lb import FeatureDisableSelectView, SettingsPanelView
    from cogs.verification import VerificationDashboardView

    # Verify cog and asset files are removed
    root = Path(__file__).resolve().parents[1]
    assert not (root / "cogs" / "certificate.py").exists()
    assert not (root / "assets" / "certificate_template").exists()
    assert not (root / "assets" / "batch1").exists()

    # Verify feature disable options do not include certificate
    disable_view = FeatureDisableSelectView(MagicMock())
    labels = [opt.label for opt in disable_view.feature.options]
    values = [opt.value for opt in disable_view.feature.options]
    assert "certificate" not in values
    assert "Certificate" not in labels

    # Verify Settings panel does not include Enable Certification
    panel_view = SettingsPanelView(MagicMock())
    button_labels = [item.label for item in panel_view.children if hasattr(item, "label")]
    assert "Enable Certification" not in button_labels

    # Verify Verification dashboard does not include Validate Certificate
    v_view = VerificationDashboardView(MagicMock())
    v_button_labels = [item.label for item in v_view.children if hasattr(item, "label")]
    assert "Validate Certificate" not in v_button_labels
    assert "Verify" in v_button_labels
    assert "Check UUID" in v_button_labels

    # Verify disabling certificate is rejected as unknown feature
    label, err = _disable_feature_worker("test_guild_cert", "certificate")
    assert err is not None
    assert label is None



