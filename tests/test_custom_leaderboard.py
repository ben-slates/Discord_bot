import pytest
from types import SimpleNamespace

from utils.leaderboard import should_include_member_for_custom_leaderboard


def test_custom_leaderboard_filters_by_required_role_ids():
    matching_member = SimpleNamespace(roles=[SimpleNamespace(id=42, name="Member")])
    non_matching_member = SimpleNamespace(roles=[SimpleNamespace(id=99, name="Guest")])

    assert should_include_member_for_custom_leaderboard(matching_member, required_role_ids=["42"])
    assert not should_include_member_for_custom_leaderboard(non_matching_member, required_role_ids=["42"])


def test_custom_leaderboard_defaults_to_batch1_members_when_no_role_is_required():
    batch1_member = SimpleNamespace(roles=[SimpleNamespace(id=42, name="Batch 1")])
    non_batch1_member = SimpleNamespace(roles=[SimpleNamespace(id=99, name="Guest")])

    assert should_include_member_for_custom_leaderboard(batch1_member, required_role_ids=[])
    assert not should_include_member_for_custom_leaderboard(non_batch1_member, required_role_ids=[])


@pytest.mark.asyncio
async def test_daily_leaderboard_respects_feature_disabled():
    import datetime
    from unittest.mock import AsyncMock, MagicMock, patch
    from cogs.notifications import NotificationsCog

    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    cog = NotificationsCog(bot)

    mock_channel = MagicMock()
    mock_channel.send = AsyncMock()
    mock_channel.history = MagicMock()

    mock_guild = MagicMock()
    mock_guild.id = 123456
    mock_guild.get_channel = MagicMock(return_value=mock_channel)
    bot.get_guild = MagicMock(return_value=mock_guild)

    # Config where leaderboard_enabled is False
    disabled_config = SimpleNamespace(
        guild_id="123456",
        leaderboard_enabled=False,
        leaderboard_channel="999",
        quote_of_day_enabled=False,
        attendance_enabled=False,
        clean_server_enabled=False,
    )

    now_8am = datetime.datetime(2026, 11, 8, 8, 0, 0)
    with patch("cogs.notifications.run_db", new=AsyncMock(return_value=[disabled_config])):
        await cog._run_daily_summary(now_8am)

    # Must NOT send any leaderboard message when disabled
    mock_channel.send.assert_not_called()
