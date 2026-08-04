from database import GuildConfig


def should_include_member_for_custom_leaderboard(member) -> bool:
    role_names = [role.name.lower() for role in getattr(member, "roles", [])]
    return any("batch 1" in role_name or "batch1" in role_name for role_name in role_names)


def parse_main_leaderboard_role_ids(value) -> list[str]:
    if not value:
        return []
    role_ids = [role_id.strip() for role_id in str(value).split(",") if role_id.strip()]
    return role_ids[:1]


def get_main_leaderboard_role_ids(db, guild_id) -> list[str]:
    config = db.query(GuildConfig).filter_by(guild_id=str(guild_id)).first()
    if not config:
        return []
    return parse_main_leaderboard_role_ids(getattr(config, "main_leaderboard_role_ids", None))


def should_include_member_for_main_leaderboard(member, allowed_role_ids=None) -> bool:
    if not allowed_role_ids:
        return True

    allowed = {str(role_id) for role_id in allowed_role_ids}
    return any(str(role.id) in allowed for role in getattr(member, "roles", []))
