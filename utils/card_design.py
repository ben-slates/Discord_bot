"""The shared frosted-glass rendering engine used by every card.

All primitives work on Pillow RGBA images and deliberately keep their public
API small so card layouts can share one visual language.
"""
from __future__ import annotations

from PIL import Image, ImageChops, ImageDraw, ImageFilter


class Colors:
    GLASS = (20, 25, 40, 82)
    BORDER = (255, 255, 255, 46)
    HEADING = (255, 255, 255, 255)
    PRIMARY = (255, 255, 255, 245)
    SECONDARY = (255, 255, 255, 184)
    MUTED = (255, 255, 255, 140)
    BLUE = (91, 146, 255, 255)
    GREEN = (94, 220, 156, 255)
    PURPLE = (190, 126, 255, 255)
    GOLD = (255, 215, 112, 255)


class Spacing:
    XS = 8
    SM = 16
    MD = 24
    LG = 32


class BorderRadius:
    CARD = 24
    PANEL = 20
    BADGE = 16


class Glow:
    BLUE = (91, 146, 255, 46)
    GREEN = (94, 220, 156, 46)
    PURPLE = (190, 126, 255, 46)


class Typography:
    HEADING = Colors.HEADING
    PRIMARY = Colors.PRIMARY
    SECONDARY = Colors.SECONDARY
    MUTED = Colors.MUTED


class CardTheme:
    """Compatibility aliases for existing card layouts."""
    RADIUS = BorderRadius.PANEL
    PANEL_FILL = Colors.GLASS
    PANEL_BORDER = Colors.BORDER
    TEXT = Typography.PRIMARY
    MUTED_TEXT = Typography.MUTED
    BLUE = Colors.BLUE
    GREEN = Colors.GREEN
    PURPLE = Colors.PURPLE
    GOLD = Colors.GOLD


_OUTER_BORDER = (255, 255, 255, 46)  # rgba(255,255,255,.18)
_INNER_BORDER = (255, 255, 255, 20)  # rgba(255,255,255,.08)
_AA_SCALE = 3


def _rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    """Return an anti-aliased rounded mask at native image dimensions."""
    width, height = size
    scaled = Image.new("L", (width * _AA_SCALE, height * _AA_SCALE), 0)
    ImageDraw.Draw(scaled).rounded_rectangle(
        (0, 0, width * _AA_SCALE - 1, height * _AA_SCALE - 1),
        radius=max(0, radius * _AA_SCALE), fill=255,
    )
    return scaled.resize(size, Image.Resampling.LANCZOS)


def _vertical_gradient(size: tuple[int, int], stops: tuple[tuple[float, tuple[int, int, int, int]], ...]) -> Image.Image:
    """Build a compact RGBA vertical gradient from normalized colour stops."""
    width, height = size
    gradient = Image.new("RGBA", size)
    pixels = gradient.load()
    for y in range(height):
        position = y / max(height - 1, 1)
        lower, upper = stops[0], stops[-1]
        for index in range(len(stops) - 1):
            if stops[index][0] <= position <= stops[index + 1][0]:
                lower, upper = stops[index], stops[index + 1]
                break
        span = max(upper[0] - lower[0], 0.0001)
        factor = min(max((position - lower[0]) / span, 0.0), 1.0)
        colour = tuple(round(lower[1][channel] + (upper[1][channel] - lower[1][channel]) * factor) for channel in range(4))
        for x in range(width):
            pixels[x, y] = colour
    return gradient


def _alpha_composite_masked(destination: Image.Image, source: Image.Image, position: tuple[int, int], mask: Image.Image | None = None) -> None:
    """Alpha-composite a source, optionally restricting it with an L mask."""
    if mask is not None:
        source = source.copy()
        source.putalpha(ImageChops.multiply(source.getchannel("A"), mask))
    destination.alpha_composite(source, position)


def _ambient_glow(image: Image.Image, bounds: tuple[int, int, int, int], radius: int, colour: tuple[int, int, int, int]) -> None:
    """A restrained local glow; avoids costly full-card blur layers."""
    x0, y0, x1, y1 = bounds
    padding = 12
    local_size = (x1 - x0 + padding * 2, y1 - y0 + padding * 2)
    layer = Image.new("RGBA", local_size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(
        (padding, padding, local_size[0] - padding - 1, local_size[1] - padding - 1),
        radius=radius, outline=colour, width=2,
    )
    _alpha_composite_masked(image, layer.filter(ImageFilter.GaussianBlur(7)), (x0 - padding, y0 - padding))


def draw_card_frame(draw: ImageDraw.ImageDraw, size: tuple[int, int]) -> None:
    """A thin, illuminated frame rather than a heavy outline."""
    width, height = size
    draw.rounded_rectangle((7, 7, width - 8, height - 8), radius=BorderRadius.CARD,
                           outline=(154, 190, 255, 78), width=1)
    draw.rounded_rectangle((10, 10, width - 11, height - 11), radius=BorderRadius.PANEL,
                           outline=_INNER_BORDER, width=1)


def draw_glass_panel(
    image: Image.Image,
    bounds: tuple[int, int, int, int],
    radius: int = BorderRadius.PANEL,
    glow: tuple[int, int, int, int] | None = None,
    fill: tuple[int, int, int, int] = Colors.GLASS,
) -> None:
    """Composite a layered acrylic panel from the wallpaper directly beneath it."""
    x0, y0, x1, y1 = bounds
    size = (x1 - x0, y1 - y0)
    if size[0] <= 1 or size[1] <= 1:
        return
    mask = _rounded_mask(size, radius)
    glow_colour = glow or Glow.BLUE
    _ambient_glow(image, bounds, radius, glow_colour)

    # The blurred wallpaper is the base; colour, light, texture and edges are then
    # layered inside the same mask rather than painting an opaque dark rectangle.
    panel = image.crop(bounds).filter(ImageFilter.GaussianBlur(20)).convert("RGBA")
    cool_tint = Image.new("RGBA", size, (85, 145, 255, 20))
    panel.alpha_composite(cool_tint)
    panel.alpha_composite(Image.new("RGBA", size, fill))
    panel.alpha_composite(_vertical_gradient(size, (
        (0.00, (255, 255, 255, 30)),
        (0.24, (255, 255, 255, 9)),
        (0.34, (255, 255, 255, 0)),
        (0.70, (0, 0, 0, 0)),
        (1.00, (1, 7, 20, 26)),
    )))

    # Fine low-alpha monochrome grain makes the acrylic feel physical, not flat.
    noise = Image.effect_noise(size, 18).convert("L")
    noise_alpha = noise.point(lambda value: 3 + value // 85)  # 1.2–2.4% opacity
    grain = Image.merge("RGBA", (noise, noise, noise, noise_alpha))
    panel.alpha_composite(grain)

    # Gentle corner specular light; visible on bright wallpapers but never a white wash.
    specular = Image.new("RGBA", size, (0, 0, 0, 0))
    spec_draw = ImageDraw.Draw(specular)
    corner_width, corner_height = max(28, size[0] // 4), max(18, size[1] // 3)
    spec_draw.ellipse((-corner_width // 2, -corner_height // 2, corner_width, corner_height), fill=(255, 255, 255, 18))
    spec_draw.ellipse((size[0] - corner_width, -corner_height // 2, size[0] + corner_width // 2, corner_height), fill=(190, 220, 255, 10))
    panel.alpha_composite(specular.filter(ImageFilter.GaussianBlur(8)))

    panel_draw = ImageDraw.Draw(panel)
    panel_draw.rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, outline=_OUTER_BORDER, width=1)
    panel_draw.rounded_rectangle((1, 1, size[0] - 2, size[1] - 2), radius=max(0, radius - 1), outline=_INNER_BORDER, width=1)
    _alpha_composite_masked(image, panel, (x0, y0), mask)


def draw_stat_card(image: Image.Image, bounds: tuple[int, int, int, int], label: str, value: str,
                   accent: tuple[int, int, int, int], load_font, fit_text) -> None:
    draw_glass_panel(image, bounds, BorderRadius.BADGE, Glow.BLUE)
    draw = ImageDraw.Draw(image)
    x0, y0, x1, _ = bounds
    draw.text((x0 + Spacing.SM + 2, y0 + 13), label.upper(), font=load_font(14), fill=Typography.MUTED)
    draw.text((x0 + Spacing.SM + 2, y0 + 42), value,
              font=fit_text(draw, value, x1 - x0 - (Spacing.SM + 2) * 2, 25, 16), fill=accent)


def draw_badge(image: Image.Image, bounds: tuple[int, int, int, int], text: str, accent, font) -> None:
    draw_glass_panel(image, bounds, BorderRadius.BADGE, (*accent[:3], 48))
    x0, y0, x1, y1 = bounds
    ImageDraw.Draw(image).text(((x0 + x1) // 2, (y0 + y1) // 2), text, font=font, fill=accent, anchor="mm")


def draw_progress_bar(image: Image.Image, bounds: tuple[int, int, int, int], ratio: float, kind: str = "xp") -> None:
    """Render a clipped, glossy gradient fill inside an inset glass track."""
    x0, y0, x1, y1 = bounds
    width, height = x1 - x0, y1 - y0
    if width <= 1 or height <= 1:
        return
    radius = height // 2
    ratio = min(max(ratio, 0.0), 1.0)
    track_mask = _rounded_mask((width, height), radius)

    track = Image.new("RGBA", (width, height), (8, 13, 28, 124))
    track.alpha_composite(_vertical_gradient((width, height), (
        (0.0, (255, 255, 255, 10)), (0.36, (255, 255, 255, 0)), (1.0, (0, 0, 0, 20)),
    )))
    track_draw = ImageDraw.Draw(track)
    track_draw.rounded_rectangle((0, 1, width - 1, height - 1), radius=radius, outline=(0, 0, 0, 72), width=1)
    track_draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=radius, outline=_INNER_BORDER, width=1)
    _alpha_composite_masked(image, track, (x0, y0), track_mask)

    fill_width = round(width * ratio)
    if fill_width <= 0:
        return
    start, end = ((49, 125, 246, 255), (118, 211, 252, 255)) if kind == "xp" else ((27, 184, 89, 255), (126, 236, 161, 255))
    fill = _vertical_gradient((width, height), ((0.0, start), (1.0, end)))
    fill_mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(fill_mask).rounded_rectangle((0, 0, fill_width - 1, height - 1), radius=radius, fill=255)
    fill_mask = ImageChops.multiply(fill_mask, track_mask)
    fill.putalpha(fill_mask)

    # A soft coloured halo stays inside the track; the fill and gloss cannot overflow.
    inner_glow = Image.new("RGBA", (width, height), (*start[:3], 0))
    inner_glow.putalpha(fill_mask.filter(ImageFilter.GaussianBlur(3)).point(lambda value: value // 5))
    _alpha_composite_masked(image, inner_glow, (x0, y0), track_mask)
    _alpha_composite_masked(image, fill, (x0, y0), track_mask)

    gloss = _vertical_gradient((width, height), (
        (0.0, (255, 255, 255, 62)), (0.34, (255, 255, 255, 16)), (0.52, (255, 255, 255, 0)), (1.0, (255, 255, 255, 0)),
    ))
    gloss.putalpha(ImageChops.multiply(gloss.getchannel("A"), fill_mask))
    _alpha_composite_masked(image, gloss, (x0, y0), track_mask)
    # Re-establish the delicate glass track edge above every fill state.
    edge = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(edge).rounded_rectangle((0, 0, width - 1, height - 1), radius=radius, outline=_OUTER_BORDER, width=1)
    _alpha_composite_masked(image, edge, (x0, y0), track_mask)
