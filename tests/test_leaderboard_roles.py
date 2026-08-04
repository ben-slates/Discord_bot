import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.leaderboard import (
    get_leaderboard_channel_id,
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
