from __future__ import annotations


def calculate_required_xp(level: int) -> int:
    if level < 1:
        raise ValueError("Level must be at least 1.")
    return (20 * (level**2)) + (100 * level) + 250


def calculate_level(total_xp: int, max_level: int = 100) -> int:
    if total_xp < 0:
        raise ValueError("Total XP cannot be negative.")

    level = 1
    while level < max_level and total_xp >= calculate_required_xp(level):
        level += 1
    return min(level, max_level)


def calculate_progress(total_xp: int, max_level: int = 100) -> tuple[int, int, float, bool]:
    level = calculate_level(total_xp, max_level=max_level)
    if level >= max_level:
        return 0, 0, 1.0, True

    previous_threshold = 0 if level == 1 else calculate_required_xp(level - 1)
    next_threshold = calculate_required_xp(level)
    current_progress = max(total_xp - previous_threshold, 0)
    needed_for_next = max(next_threshold - previous_threshold, 1)
    progress_ratio = current_progress / needed_for_next
    return current_progress, needed_for_next, min(max(progress_ratio, 0.0), 1.0), False


def get_message_xp(level: int) -> int:
    return 10
