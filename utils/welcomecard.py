from __future__ import annotations
import asyncio
from io import BytesIO
import discord
from PIL import ImageDraw, Image
from card_design import BorderRadius, CardTheme, Glow, Spacing, Typography, draw_card_frame, draw_glass_panel
from rankcard import _avatar, _fit_text, _get_random_bg, _load_font

WELCOME_CARD_SIZE = (1000, 400)
WELCOME_SIDE_PADDING = Spacing.LG + 16
WELCOME_PANEL_GAP = Spacing.SM
WELCOME_AVATAR_PANEL_WIDTH = 220
WELCOME_AVATAR_SIZE = 185
WELCOME_CONTENT_LEFT = 320
WELCOME_CONTENT_RIGHT = 952
WELCOME_HEADER_Y = 58
WELCOME_HEADER_HEIGHT = 96
WELCOME_LABEL_TOP = 5
WELCOME_GUILD_NAME_TOP = 30
WELCOME_USERNAME_TEXT_TOP = 7
WELCOME_USERNAME_HEIGHT = 68
WELCOME_BADGE_HEIGHT = 60
WELCOME_PANEL_TEXT_PADDING = Spacing.MD
WELCOME_ACCENT = (0, 212, 255, 255)


def _draw_spaced_text(draw: ImageDraw.ImageDraw, position: tuple[int, int], text: str, font, fill, spacing: int = 2) -> None:
    """Render the small all-caps eyebrow with restrained letter spacing."""
    x, y = position
    for character in text:
        draw.text((x, y), character, font=font, fill=fill)
        x += int(draw.textlength(character, font=font)) + spacing


def _render_welcome_card(avatar_bytes: bytes, username: str, guild_name: str, member_count: int) -> discord.File:
    width, height = WELCOME_CARD_SIZE
    card = _get_random_bg(width, height, "welcome_bg") or Image.new("RGBA", (width, height), (38, 58, 92, 255))
    draw_card_frame(ImageDraw.Draw(card), (width, height))
    draw = ImageDraw.Draw(card)

    # The wallpaper remains visible; each compact component receives its own glass.
    header_bounds = (WELCOME_CONTENT_LEFT, WELCOME_HEADER_Y, WELCOME_CONTENT_RIGHT, WELCOME_HEADER_Y + WELCOME_HEADER_HEIGHT)
    username_bounds = (WELCOME_CONTENT_LEFT, header_bounds[3] + WELCOME_PANEL_GAP, WELCOME_CONTENT_RIGHT, header_bounds[3] + WELCOME_PANEL_GAP + WELCOME_USERNAME_HEIGHT)
    member_y = username_bounds[3] + WELCOME_PANEL_GAP
    badge_font = _load_font(21)
    badge_text = f"Member #{member_count:,}"
    badge_text_width = int(draw.textbbox((0, 0), badge_text, font=badge_font)[2])
    member_width = badge_text_width + WELCOME_PANEL_TEXT_PADDING * 2
    member_bounds = (WELCOME_CONTENT_LEFT, member_y, WELCOME_CONTENT_LEFT + member_width, member_y + WELCOME_BADGE_HEIGHT)
    avatar_panel = (WELCOME_SIDE_PADDING, WELCOME_HEADER_Y, WELCOME_SIDE_PADDING + WELCOME_AVATAR_PANEL_WIDTH, member_bounds[3])
    avatar_position = (
        avatar_panel[0] + (WELCOME_AVATAR_PANEL_WIDTH - WELCOME_AVATAR_SIZE) // 2,
        avatar_panel[1] + ((avatar_panel[3] - avatar_panel[1]) - WELCOME_AVATAR_SIZE) // 2,
    )

    draw_glass_panel(card, avatar_panel, BorderRadius.PANEL, glow=Glow.BLUE)
    draw_glass_panel(card, header_bounds, BorderRadius.PANEL, glow=Glow.BLUE)
    draw_glass_panel(card, username_bounds, BorderRadius.BADGE, glow=Glow.BLUE)
    draw_glass_panel(card, member_bounds, BorderRadius.BADGE, glow=Glow.GREEN)
    _avatar(card, avatar_bytes, avatar_position, WELCOME_AVATAR_SIZE, CardTheme.BLUE)

    text_left = WELCOME_CONTENT_LEFT + WELCOME_PANEL_TEXT_PADDING
    _draw_spaced_text(draw, (text_left, header_bounds[1] + WELCOME_LABEL_TOP), "WELCOME TO", _load_font(19), WELCOME_ACCENT)
    draw.text((text_left, header_bounds[1] + WELCOME_GUILD_NAME_TOP), guild_name,
              font=_fit_text(draw, guild_name, WELCOME_CONTENT_RIGHT - text_left - WELCOME_PANEL_TEXT_PADDING, 46, 16), fill=Typography.HEADING)
    draw.text((text_left, username_bounds[1] + WELCOME_USERNAME_TEXT_TOP), username,
              font=_fit_text(draw, username, WELCOME_CONTENT_RIGHT - text_left - WELCOME_PANEL_TEXT_PADDING, 42, 16), fill=Typography.HEADING)
    draw.text((text_left, (member_bounds[1] + member_bounds[3]) // 2), badge_text, font=badge_font, fill=WELCOME_ACCENT, anchor="lm")
    output = BytesIO(); card.save(output, "PNG"); output.seek(0)
    return discord.File(output, filename="welcome.png")


async def generate_welcome_card(member: discord.Member) -> discord.File:
    count = sum(not guild_member.bot for guild_member in member.guild.members)
    return await asyncio.to_thread(_render_welcome_card, await member.display_avatar.replace(size=256).read(), member.display_name, member.guild.name, count)
