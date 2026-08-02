from __future__ import annotations

import asyncio
import random
from io import BytesIO
from pathlib import Path

import discord
from PIL import Image, ImageDraw, ImageFont, ImageOps

from card_design import BorderRadius, CardTheme, Glow, Spacing, Typography, draw_badge, draw_card_frame, draw_glass_panel, draw_progress_bar, draw_stat_card
from xp import calculate_progress

BACKGROUND_SUFFIXES = {".webp", ".png", ".jpg", ".jpeg", ".bmp", ".gif"}
_LAST_BACKGROUND_PATHS: dict[str, Path] = {}

RANK_CARD_SIZE = (1180, 500)
RANK_CONTENT_LEFT = 330
RANK_CONTENT_RIGHT = 1095
RANK_HEADER_Y = 48
RANK_HEADER_HEIGHT = 118
RANK_STAT_ROW_Y = 184
RANK_STAT_HEIGHT = 110
RANK_STAT_GAP = 18
RANK_PROGRESS_Y = 316
RANK_PROGRESS_HEIGHT = 156
RANK_PANEL_GAP = Spacing.MD
RANK_PANEL_PADDING = 20
RANK_PROGRESS_BAR_TOP = 72
RANK_PROGRESS_BAR_HEIGHT = 32
RANK_PROGRESS_HINT_TOP = 120
RANK_XP_PANEL_RIGHT = 736
RANK_MAX_BADGE_BOUNDS = (930, 76, 1098, 124)
AVATAR_PANEL_WIDTH = 210
RANK_AVATAR_PANEL_WIDTH = 290
AVATAR_COLUMN_GAP = Spacing.LG
RANK_AVATAR_PANEL_LEFT = 28
RANK_AVATAR_PANEL_BOUNDS = (RANK_AVATAR_PANEL_LEFT, RANK_HEADER_Y, RANK_AVATAR_PANEL_LEFT + RANK_AVATAR_PANEL_WIDTH, RANK_PROGRESS_Y + RANK_PROGRESS_HEIGHT)
RANK_AVATAR_SIZE = 190
RANK_AVATAR_POSITION = (
    RANK_AVATAR_PANEL_BOUNDS[0] + (RANK_AVATAR_PANEL_WIDTH - RANK_AVATAR_SIZE) // 2,
    RANK_AVATAR_PANEL_BOUNDS[1] + (RANK_AVATAR_PANEL_BOUNDS[3] - RANK_AVATAR_PANEL_BOUNDS[1] - RANK_AVATAR_SIZE) // 2,
)

LEVELUP_CARD_SIZE = (1000, 320)
LEVELUP_SIDE_PADDING = Spacing.LG
LEVELUP_COLUMN_GAP = Spacing.LG
LEVELUP_AVATAR_PANEL_WIDTH = AVATAR_PANEL_WIDTH
LEVELUP_AVATAR_SIZE = 150
LEVELUP_HEADER_PANEL_Y = 40
LEVELUP_HEADER_PANEL_HEIGHT = 122
LEVELUP_HEADER_Y = LEVELUP_HEADER_PANEL_Y + 12
LEVELUP_USERNAME_Y = LEVELUP_HEADER_PANEL_Y + 62
LEVELUP_STAT_ROW_Y = LEVELUP_HEADER_PANEL_Y + LEVELUP_HEADER_PANEL_HEIGHT + Spacing.SM
LEVELUP_STAT_HEIGHT = 100
LEVELUP_STAT_GAP = Spacing.SM
LEVELUP_MESSAGE_Y = 282
LEVELUP_TITLE_INSET = 38
SECTION_ACCENT = (0, 212, 255, 255)


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(str(Path(__file__).parent.parent / "assets" / "rankcard" / "DejaVuSans.ttf"), size)
    except OSError:
        return ImageFont.load_default()


def _fit_text(draw: ImageDraw.ImageDraw, text: str, max_width: int, start_size: int, min_size: int):
    for size in range(start_size, min_size - 1, -2):
        font = _load_font(size)
        if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
            return font
    return _load_font(min_size)


def _draw_section_label(draw: ImageDraw.ImageDraw, position: tuple[int, int], text: str, font_size: int = 14, tracking: int = 1) -> None:
    """Render the shared cyan, tracked label treatment used across this file."""
    x, y = position
    font = _load_font(font_size)
    if tracking == 0:
        draw.text((x, y), text.upper(), font=font, fill=SECTION_ACCENT)
        return
    for character in text.upper():
        draw.text((x, y), character, font=font, fill=SECTION_ACCENT)
        x += int(draw.textlength(character, font=font)) + tracking


def _get_random_bg(width: int, height: int, bg_folder: str) -> Image.Image | None:
    directory = Path(__file__).parent.parent / "assets" / bg_folder
    files = [path for path in directory.rglob("*") if path.is_file() and path.suffix.lower() in BACKGROUND_SUFFIXES] if directory.exists() else []
    if len(files) > 1 and (last_path := _LAST_BACKGROUND_PATHS.get(bg_folder)) in files:
        files = [path for path in files if path != last_path]
    try:
        if not files:
            return None
        selected = random.choice(files)
        _LAST_BACKGROUND_PATHS[bg_folder] = selected
        return ImageOps.fit(Image.open(selected).convert("RGBA"), (width, height))
    except Exception as error:
        print(f"Unable to load card background: {error}")
        return None


def _base_card(width: int, height: int, folder: str) -> Image.Image:
    background = _get_random_bg(width, height, folder)
    card = background or Image.new("RGBA", (width, height), (38, 58, 92, 255))
    draw_card_frame(ImageDraw.Draw(card), (width, height))
    return card


def _avatar(card: Image.Image, raw: bytes, position: tuple[int, int], size: int, accent: tuple[int, int, int, int]) -> None:
    avatar = ImageOps.fit(Image.open(BytesIO(raw)).convert("RGBA"), (size, size))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    card.paste(avatar, position, mask)
    x, y = position
    ImageDraw.Draw(card).ellipse((x - 8, y - 8, x + size + 8, y + size + 8), outline=accent, width=4)


def _stat(card: Image.Image, bounds, label: str, value: str, accent=CardTheme.TEXT, value_fit=_fit_text) -> None:
    draw_stat_card(card, bounds, label, value, accent, _load_font, value_fit)
    # Match StatCard's exact label glyph positions so the cyan accent replaces,
    # rather than visually duplicates, the shared muted label.
    _draw_section_label(ImageDraw.Draw(card), (bounds[0] + Spacing.SM + 2, bounds[1] + 13), label, tracking=0)


def _fit_level_value(draw: ImageDraw.ImageDraw, text: str, max_width: int, _start_size: int, _min_size: int):
    """Give achievement values a stronger hierarchy while retaining StatCard."""
    return _fit_text(draw, text, max_width, 34, 22)


def _draw_levelup_stat(card: Image.Image, bounds, label: str, value: str, accent) -> None:
    _stat(card, bounds, label, value, accent, _fit_level_value)


def _draw_profile_panel(card: Image.Image, draw: ImageDraw.ImageDraw, username: str, profile_title: str) -> None:
    bounds = (RANK_CONTENT_LEFT, RANK_HEADER_Y, RANK_CONTENT_RIGHT, RANK_HEADER_Y + RANK_HEADER_HEIGHT)
    draw_glass_panel(card, bounds, BorderRadius.PANEL, Glow.BLUE)
    text_left = RANK_CONTENT_LEFT + Spacing.LG - 4
    _draw_section_label(draw, (text_left, bounds[1] + 13), profile_title)
    draw.text((text_left, bounds[1] + 33), username,
              font=_fit_text(draw, username, bounds[2] - text_left - Spacing.MD, 46, 16), fill=Typography.HEADING)


def _draw_stat_row(card: Image.Image, level: int, xp: int, rank: int | None, max_level: int) -> bool:
    row_width = RANK_CONTENT_RIGHT - RANK_CONTENT_LEFT
    stat_width = (row_width - RANK_STAT_GAP * 2) // 3
    stat_bounds = []
    for index in range(3):
        left = RANK_CONTENT_LEFT + index * (stat_width + RANK_STAT_GAP)
        right = RANK_CONTENT_RIGHT if index == 2 else left + stat_width
        stat_bounds.append((left, RANK_STAT_ROW_Y, right, RANK_STAT_ROW_Y + RANK_STAT_HEIGHT))
    is_max = level >= max_level
    _stat(card, stat_bounds[0], "Server Rank", f"#{rank}" if rank else "Unranked")
    _stat(card, stat_bounds[1], "Current Level", "MAX LEVEL" if is_max else f"Level {level}", CardTheme.GREEN if is_max else CardTheme.BLUE)
    _stat(card, stat_bounds[2], "Total XP", f"{xp:,}")
    return is_max


def _draw_progress_section(
    card: Image.Image,
    draw: ImageDraw.ImageDraw,
    bounds: tuple[int, int, int, int],
    title: str,
    value: str,
    ratio: float,
    kind: str,
    hint: str,
) -> None:
    """Render either of the identically padded Rank Card meter panels."""
    draw_glass_panel(card, bounds, BorderRadius.PANEL, Glow.BLUE)
    left = bounds[0] + RANK_PANEL_PADDING
    right = bounds[2] - RANK_PANEL_PADDING
    _draw_section_label(draw, (left, bounds[1] + 24), title, 18)
    draw.text((right, bounds[1] + 28), value, font=_fit_text(draw, value, 250, 22, 16), fill=Typography.PRIMARY, anchor="ra")
    draw_progress_bar(card, (left, bounds[1] + RANK_PROGRESS_BAR_TOP, right, bounds[1] + RANK_PROGRESS_BAR_TOP + RANK_PROGRESS_BAR_HEIGHT), ratio, kind)
    draw.text((left, bounds[1] + RANK_PROGRESS_HINT_TOP), hint, font=_load_font(15), fill=Typography.MUTED)


def _render_rank_card(avatar_bytes, username, profile_title, level, xp, rank, daily_xp_earned, daily_xp_cap, max_level):
    width, height = RANK_CARD_SIZE
    card = _base_card(width, height, "rankcard_bg")
    draw = ImageDraw.Draw(card)
    draw_glass_panel(card, RANK_AVATAR_PANEL_BOUNDS, BorderRadius.PANEL, Glow.BLUE)
    _avatar(card, avatar_bytes, RANK_AVATAR_POSITION, RANK_AVATAR_SIZE, CardTheme.BLUE)
    _draw_profile_panel(card, draw, username, profile_title)
    is_max = _draw_stat_row(card, level, xp, rank, max_level)

    progress, needed, ratio, at_max = calculate_progress(xp, max_level=max_level)
    xp_panel = (RANK_CONTENT_LEFT, RANK_PROGRESS_Y, RANK_XP_PANEL_RIGHT, RANK_PROGRESS_Y + RANK_PROGRESS_HEIGHT)
    daily_panel = (xp_panel[2] + RANK_PANEL_GAP, RANK_PROGRESS_Y, RANK_CONTENT_RIGHT, RANK_PROGRESS_Y + RANK_PROGRESS_HEIGHT)
    _draw_progress_section(card, draw, xp_panel, "XP Progress", "MAX LEVEL" if at_max else f"{progress:,} / {needed:,} XP",
                           1.0 if at_max else ratio, "xp", "You are at the level cap." if at_max else "Keep chatting to reach the next level.")
    _draw_progress_section(card, draw, daily_panel, "Daily XP", f"{daily_xp_earned:,} / {daily_xp_cap:,}",
                           daily_xp_earned / max(daily_xp_cap, 1), "daily", f"{max(daily_xp_cap - daily_xp_earned, 0):,} XP remaining today")
    if is_max:
        draw_badge(card, RANK_MAX_BADGE_BOUNDS, "MAX LEVEL", CardTheme.GREEN, _load_font(18))
    output = BytesIO(); card.save(output, "PNG"); output.seek(0)
    return discord.File(output, filename="rankcard.png")


async def generate_rank_card(member, level, xp, rank, daily_xp_earned, daily_xp_cap, max_level, profile_title="Community Profile"):
    return await asyncio.to_thread(_render_rank_card, await member.display_avatar.replace(size=256).read(), member.display_name, profile_title, level, xp, rank, daily_xp_earned, daily_xp_cap, max_level)


def _render_levelup_card(avatar_bytes, username, previous_level, new_level, max_level):
    width, height = LEVELUP_CARD_SIZE
    is_max = new_level >= max_level
    accent = SECTION_ACCENT
    card = _base_card(width, height, "Levelupcard_bg")

    avatar_panel = (
        LEVELUP_SIDE_PADDING,
        LEVELUP_SIDE_PADDING,
        LEVELUP_SIDE_PADDING + LEVELUP_AVATAR_PANEL_WIDTH,
        height - LEVELUP_SIDE_PADDING,
    )
    avatar_x = avatar_panel[0] + (LEVELUP_AVATAR_PANEL_WIDTH - LEVELUP_AVATAR_SIZE) // 2
    avatar_y = (height - LEVELUP_AVATAR_SIZE) // 2
    draw_glass_panel(card, avatar_panel, BorderRadius.PANEL, (*accent[:3], 42))
    _avatar(card, avatar_bytes, (avatar_x, avatar_y), LEVELUP_AVATAR_SIZE, accent)

    content_left = avatar_panel[2] + LEVELUP_COLUMN_GAP
    content_right = width - LEVELUP_SIDE_PADDING
    draw = ImageDraw.Draw(card)
    header = "MAX LEVEL REACHED!" if is_max else "LEVEL UP!"
    header_bounds = (content_left, LEVELUP_HEADER_PANEL_Y, content_right, LEVELUP_HEADER_PANEL_Y + LEVELUP_HEADER_PANEL_HEIGHT)
    draw_glass_panel(card, header_bounds, BorderRadius.PANEL, Glow.BLUE)
    draw.text((content_left + LEVELUP_TITLE_INSET, LEVELUP_HEADER_Y), header,
              font=_fit_text(draw, header, content_right - content_left - LEVELUP_TITLE_INSET, 42, 30), fill=accent)
    draw.text((content_left + Spacing.MD, LEVELUP_USERNAME_Y), username,
              font=_fit_text(draw, username, content_right - content_left - Spacing.MD * 2, 36, 16), fill=Typography.HEADING)

    content_width = content_right - content_left
    stat_width = (content_width - LEVELUP_STAT_GAP) // 2
    previous_bounds = (content_left, LEVELUP_STAT_ROW_Y, content_left + stat_width, LEVELUP_STAT_ROW_Y + LEVELUP_STAT_HEIGHT)
    new_bounds = (previous_bounds[2] + LEVELUP_STAT_GAP, LEVELUP_STAT_ROW_Y, content_right, LEVELUP_STAT_ROW_Y + LEVELUP_STAT_HEIGHT)
    _draw_levelup_stat(card, previous_bounds, "Previous Level", str(previous_level), Typography.PRIMARY)
    _draw_levelup_stat(card, new_bounds, "New Level", str(new_level), Typography.PRIMARY)

    message = "Congratulations on reaching the level cap!" if is_max else f"Congratulations on reaching Level {new_level}!"
    draw.text((content_left, LEVELUP_MESSAGE_Y), message, font=_load_font(18), fill=SECTION_ACCENT)
    output = BytesIO(); card.save(output, "PNG"); output.seek(0)
    return discord.File(output, filename="levelupcard.png")


async def generate_levelup_card(member, level, xp=None, rank=None, max_level=100, previous_level=None):
    # xp/rank remain accepted for call-site compatibility; level-up cards deliberately do not show rank statistics.
    previous_level = previous_level if previous_level is not None else max(1, level - 1)
    return await asyncio.to_thread(_render_levelup_card, await member.display_avatar.replace(size=256).read(), member.display_name, previous_level, level, max_level)
