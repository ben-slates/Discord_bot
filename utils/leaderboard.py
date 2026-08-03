def should_include_member_for_custom_leaderboard(member) -> bool:
    role_names = [role.name.lower() for role in getattr(member, "roles", [])]
    return any("batch 1" in role_name or "batch1" in role_name for role_name in role_names)
