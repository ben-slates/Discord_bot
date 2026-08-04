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
