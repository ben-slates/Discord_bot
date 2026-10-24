import sys
from pathlib import Path
from types import SimpleNamespace
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.leaderboard import (
    get_leaderboard_channel_id,
    get_level_up_channel_id,
    parse_main_leaderboard_role_ids,
    should_include_member_for_main_leaderboard,
)


class DummyRole(SimpleNamespace):
    pass


class DummyMember(SimpleNamespace):
    pass


def test_allows_member_when_no_role_filter_is_set():
    member = DummyMember(roles=[DummyRole(id=1, name="Member")])

    assert should_include_member_for_main_leaderboard(member, []) is True


def test_allows_member_when_one_of_the_roles_matches():
    member = DummyMember(roles=[DummyRole(id=42, name="Alpha")])

    assert should_include_member_for_main_leaderboard(member, ["42"]) is True


def test_blocks_member_when_no_configured_role_matches():
    member = DummyMember(roles=[DummyRole(id=7, name="Beta")])

    assert should_include_member_for_main_leaderboard(member, ["42", "99"]) is False


def test_parse_keeps_only_the_first_configured_role():
    assert parse_main_leaderboard_role_ids("42,99,100") == ["42"]


def test_returns_channel_only_when_leaderboard_is_enabled():
    enabled_config = SimpleNamespace(leaderboard_enabled=True, leaderboard_channel="123")
    disabled_config = SimpleNamespace(leaderboard_enabled=False, leaderboard_channel="123")

    assert get_leaderboard_channel_id(enabled_config) == "123"
    assert get_leaderboard_channel_id(disabled_config) is None


def test_returns_channel_only_when_level_up_is_enabled():
    enabled_config = SimpleNamespace(level_up_announcements_enabled=True, level_up_announcements_channel="456")
    disabled_config = SimpleNamespace(level_up_announcements_enabled=False, level_up_announcements_channel="456")
    fallback_config = SimpleNamespace(level_up_announcements_enabled=True, level_up_announcements_channel=None, leveling_channel="789")
    leveling_config = SimpleNamespace(level_up_announcements_enabled=False, leveling_enabled=True, leveling_channel="999")

    assert get_level_up_channel_id(enabled_config) == "456"
    assert get_level_up_channel_id(disabled_config) is None
    assert get_level_up_channel_id(fallback_config) == "789"
    assert get_level_up_channel_id(leveling_config) == "999"
    assert get_level_up_channel_id(None) is None


@pytest.mark.asyncio
async def test_rank_and_rankcard_require_level_up_feature():
    from unittest.mock import AsyncMock, MagicMock
    from cogs.leveling import LevelingCog
    from database import SessionLocal, GuildConfig

    bot = MagicMock()
    cog = LevelingCog(bot)
    cog.voice_xp_loop.cancel()

    interaction = MagicMock()
    guild_id = "test_guild_rank_cog_test"
    interaction.guild_id = guild_id
    interaction.channel_id = 111
    interaction.response.send_message = AsyncMock()

    # Disabled level-up feature
    db = SessionLocal()
    cfg = db.query(GuildConfig).filter_by(guild_id=guild_id).first()
    if not cfg:
        cfg = GuildConfig(guild_id=guild_id)
        db.add(cfg)
    cfg.level_up_announcements_enabled = False
    cfg.level_up_announcements_channel = None
    cfg.leveling_enabled = False
    cfg.leveling_channel = None
    db.commit()
    db.close()

    await cog.rank.callback(cog, interaction)
    interaction.response.send_message.assert_awaited_with("Level-up feature is not enabled for this server.", ephemeral=True)

    # Enabled level-up feature but called from wrong channel
    db = SessionLocal()
    cfg = db.query(GuildConfig).filter_by(guild_id=guild_id).first()
    cfg.level_up_announcements_enabled = True
    cfg.level_up_announcements_channel = "222"
    db.commit()
    db.close()

    interaction.response.send_message.reset_mock()
    await cog.rank.callback(cog, interaction)
    interaction.response.send_message.assert_awaited_with("This command can only be used in <#222>.", ephemeral=True)

    interaction.response.send_message.reset_mock()
    await cog.rankcard.callback(cog, interaction)
    interaction.response.send_message.assert_awaited_with("This command can only be used in <#222>.", ephemeral=True)


