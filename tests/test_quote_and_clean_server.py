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
    )

    removed = await run_clean_server(guild, config, now=now)

    assert m_inactive in removed
    m_inactive.kick.assert_awaited_once_with(reason="Clean Server: Inactive for more than 30 days")

    # Ensure other members were NOT kicked
    for safe in [m_bot, m_owner, m_admin, m_high_role, m_new, m_online, m_voice, m_db_active]:
        safe.kick.assert_not_called()


def test_no_legacy_brand_in_codebase():
    import subprocess
    target = "rynex" + "security"
    result = subprocess.run(
        ["grep", "-rnI", "-i", "--exclude-dir=tests", "--exclude-dir=.pytest_cache", "--exclude-dir=.git", target, "."],
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "", f"Found occurrences of {target}: {result.stdout}"


