"""
Hall of Fame Generator (Single File) - matches the rank-card glass design
Requires:
    pip install pillow

Replace USERS with your real data. Each entry is (name, xp, level).
Drop background images (.webp/.png/.jpg) into BG_FOLDER - one is picked at
random and used as the blurred, colourful backdrop behind the glass panels,
same as the rank card screenshot.
"""
from __future__ import annotations

import io
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ---------------------------------------------------------------------------
# CONFIG - edit these
# ---------------------------------------------------------------------------
W, H = 1920, 1080
BG_FOLDER = Path(__file__).resolve().parent.parent / "assets" / "halloffame_bg"
FONT_PATH = Path(__file__).resolve().parent.parent / "assets" / "rankcard" / "DejaVuSans.ttf"
BG_SUFFIXES = {".webp", ".png", ".jpg", ".jpeg", ".bmp", ".gif"}

LOGO_PATH = Path(__file__).resolve().parent.parent / "assets" / "logo.png"             # put your logo file next to the script (any size/aspect ratio)

# ---------------------------------------------------------------------------
# THEME (same tokens as card_design.py: Spacing / BorderRadius / colours)
# ---------------------------------------------------------------------------
SP_SM, SP_MD, SP_LG = 8, 16, 24
RADIUS_CARD, RADIUS_PANEL, RADIUS_BADGE = 32, 20, 999

TEXT_HEADING = (255, 255, 255, 255)
TEXT_PRIMARY = (235, 240, 248, 255)
TEXT_MUTED = (150, 165, 190, 255)
ACCENT_CYAN = (0, 212, 255, 255)

RANK_COLORS = [
    (255, 205, 60, 255),   # gold   - #1
    (200, 210, 220, 255),  # silver - #2
    (205, 140, 80, 255),   # bronze - #3
    (90, 170, 255, 255),   # blue   - #4
    (90, 170, 255, 255),   # blue   - #5
]
GLOW_CYAN = (0, 212, 255, 70)

GLASS_BLUR = 30
GLASS_TINT = (18, 22, 38, 95)      # translucent frost - low alpha so the bg actually shows through
GLASS_WASH_ALPHA = 14               # how much of the glow colour tints the glass
GLASS_SHEEN_ALPHA = 22               # top-edge glass highlight strength


# ---------------------------------------------------------------------------
# FONT HELPERS
# ---------------------------------------------------------------------------
def _find_font_file() -> str | None:
    if FONT_PATH and Path(FONT_PATH).exists():
        return str(FONT_PATH)
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "arial.ttf",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return None


_FONT_FILE = _find_font_file()
_FONT_CACHE: dict[int, ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}


def font(size: int):
    if size not in _FONT_CACHE:
        try:
            _FONT_CACHE[size] = ImageFont.truetype(_FONT_FILE, size) if _FONT_FILE else ImageFont.load_default()
        except OSError:
            _FONT_CACHE[size] = ImageFont.load_default()
    return _FONT_CACHE[size]


def fit_text(draw: ImageDraw.ImageDraw, text: str, max_width: int, start_size: int, min_size: int):
    for size in range(start_size, min_size - 1, -2):
        f = font(size)
        if draw.textbbox((0, 0), text, font=f)[2] <= max_width:
            return f
    return font(min_size)


# ---------------------------------------------------------------------------
# BACKGROUND
# ---------------------------------------------------------------------------
def load_random_background(folder: str | Path, size: tuple[int, int]) -> Image.Image:
    w, h = size
    directory = Path(folder)
    if not directory.is_absolute():
        directory = Path(__file__).resolve().parent.parent / directory
    files = [p for p in directory.rglob("*") if p.is_file() and p.suffix.lower() in BG_SUFFIXES] if directory.exists() else []
    if files:
        src = Image.open(random.choice(files)).convert("RGB")
        sw, sh = src.size
        scale = max(w / sw, h / sh)
        src = src.resize((int(sw * scale) + 1, int(sh * scale) + 1), Image.LANCZOS)
        left = (src.width - w) // 2
        top = (src.height - h) // 2
        return src.crop((left, top, left + w, top + h))

    # fallback gradient so the script never crashes without assets
    bg = Image.new("RGB", size, (20, 24, 40))
    d = ImageDraw.Draw(bg)
    top_c, bot_c = (20, 24, 40), (70, 20, 120)
    for y in range(h):
        t = y / h
        d.line((0, y, w, y), fill=tuple(int(top_c[i] * (1 - t) + bot_c[i] * t) for i in range(3)))
    return bg


def load_logo(logo_path: str | Path | None = None) -> Image.Image | None:
    """Loads a logo file and auto-trims any transparent margin."""
    path = Path(logo_path) if logo_path else Path(LOGO_PATH)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    if not path.exists():
        print(f"Logo not found at '{path}' - using placeholder instead.")
        return None
    try:
        logo = Image.open(path).convert("RGBA")
        bbox = logo.getbbox()  # bounding box of all non-fully-transparent pixels
        if bbox:
            logo = logo.crop(bbox)
        return logo
    except Exception as e:
        print(f"Could not open logo ('{e}'); using placeholder instead.")
        return None


# ---------------------------------------------------------------------------
# GLASS DRAWING PRIMITIVES
# ---------------------------------------------------------------------------
def glass_panel(card: Image.Image, bounds, radius: int, glow=GLOW_CYAN):
    """Frosted-glass panel: blurs the background currently under `bounds`,
    tints it dark + with a hint of the glow colour, pastes it back with
    rounded corners, then adds a soft glow border + a crisp hairline."""
    x1, y1, x2, y2 = (int(v) for v in bounds)
    w, h = x2 - x1, y2 - y1
    if w <= 0 or h <= 0:
        return

    # oversample the crop area so the blur doesn't pull in hard edges from
    # neighbouring panels / the crop boundary itself
    pad = GLASS_BLUR
    ext = card.crop((x1 - pad, y1 - pad, x2 + pad, y2 + pad)).convert("RGBA")
    ext = ext.filter(ImageFilter.GaussianBlur(GLASS_BLUR))
    blurred = ext.crop((pad, pad, pad + w, pad + h))

    tint = Image.new("RGBA", (w, h), GLASS_TINT)
    glass = Image.alpha_composite(blurred, tint)
    wash = Image.new("RGBA", (w, h), (*glow[:3], GLASS_WASH_ALPHA))
    glass = Image.alpha_composite(glass, wash)

    # glass "sheen" - a soft diagonal highlight fading from top-left, the
    # bit that actually reads as "glass" rather than just a blurred tile
    sheen = Image.new("L", (w, h), 0)
    sd = ImageDraw.Draw(sheen)
    for yy in range(h):
        t = 1 - min(yy / (h * 0.6), 1.0)
        sd.line((0, yy, w, yy), fill=int(GLASS_SHEEN_ALPHA * t))
    sheen_layer = Image.new("RGBA", (w, h), (255, 255, 255, 0))
    sheen_layer.putalpha(sheen)
    glass = Image.alpha_composite(glass, sheen_layer)

    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w, h), radius=radius, fill=255)
    card.paste(glass, (x1, y1), mask)

    # soft outer glow
    glow_layer = Image.new("RGBA", card.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow_layer).rounded_rectangle((x1, y1, x2, y2), radius=radius, outline=glow, width=5)
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(5))
    card.paste(glow_layer, (0, 0), glow_layer)

    # crisp hairline
    ImageDraw.Draw(card).rounded_rectangle((x1, y1, x2, y2), radius=radius, outline=(255, 255, 255, 55), width=1)


def draw_card_frame(card: Image.Image, glow=GLOW_CYAN):
    w, h = card.size
    d = ImageDraw.Draw(card)
    d.rounded_rectangle((3, 3, w - 3, h - 3), radius=RADIUS_CARD, outline=(255, 255, 255, 90), width=2)
    d.rounded_rectangle((9, 9, w - 9, h - 9), radius=RADIUS_CARD - 6, outline=glow, width=1)


def section_label(draw: ImageDraw.ImageDraw, pos, text: str, size=15, tracking=1, color=ACCENT_CYAN):
    x, y = pos
    f = font(size)
    for ch in text.upper():
        draw.text((x, y), ch, font=f, fill=color)
        x += int(draw.textlength(ch, font=f)) + tracking


def draw_avatar_circle(card: Image.Image, center, radius: int, fill, ring_color):
    """Placeholder avatar: a filled circle + glow ring."""
    cx, cy = center
    d = ImageDraw.Draw(card)
    ring_layer = Image.new("RGBA", card.size, (0, 0, 0, 0))
    ImageDraw.Draw(ring_layer).ellipse((cx - radius - 6, cy - radius - 6, cx + radius + 6, cy + radius + 6),
                                        outline=ring_color, width=6)
    ring_layer = ring_layer.filter(ImageFilter.GaussianBlur(4))
    card.paste(ring_layer, (0, 0), ring_layer)
    d.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=fill)
    d.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline=ring_color, width=3)


def draw_avatar_image(card: Image.Image, center, radius: int, avatar_bytes, ring_color, bg_fill):
    cx, cy = center
    d = ImageDraw.Draw(card)
    ring_layer = Image.new("RGBA", card.size, (0, 0, 0, 0))
    ImageDraw.Draw(ring_layer).ellipse((cx - radius - 6, cy - radius - 6, cx + radius + 6, cy + radius + 6),
                                        outline=ring_color, width=6)
    ring_layer = ring_layer.filter(ImageFilter.GaussianBlur(4))
    card.paste(ring_layer, (0, 0), ring_layer)

    if isinstance(avatar_bytes, Image.Image):
        image = avatar_bytes.convert("RGBA")
    else:
        image = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")

    w, h = image.size
    side = min(w, h)
    image = image.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
    image = image.resize((radius * 2, radius * 2), Image.LANCZOS)

    avatar_bg = Image.new("RGBA", (radius * 2, radius * 2), bg_fill)
    avatar_bg.paste(image, (0, 0), image)

    mask = Image.new("L", (radius * 2, radius * 2), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, radius * 2, radius * 2), fill=255)
    clipped = Image.new("RGBA", (radius * 2, radius * 2), (0, 0, 0, 0))
    clipped.paste(avatar_bg, (0, 0), mask)
    card.paste(clipped, (cx - radius, cy - radius), clipped)
    d.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline=ring_color, width=3)


def draw_logo_circle(card: Image.Image, center, radius: int, logo: Image.Image, ring_color, bg_fill):
    """Same glow-ring treatment as draw_avatar_circle, with the logo fitted
    inside a circular disc. Anything outside the circle - including logo
    corners/points that would otherwise poke past the edge - is clipped
    away with a hard circular mask, so the result is a clean circle."""
    cx, cy = center
    diameter = radius * 2
    d = ImageDraw.Draw(card)

    ring_layer = Image.new("RGBA", card.size, (0, 0, 0, 0))
    ImageDraw.Draw(ring_layer).ellipse((cx - radius - 6, cy - radius - 6, cx + radius + 6, cy + radius + 6),
                                        outline=ring_color, width=6)
    ring_layer = ring_layer.filter(ImageFilter.GaussianBlur(4))
    card.paste(ring_layer, (0, 0), ring_layer)

    # build the disc (fill + fitted logo) at 4x for a smooth anti-aliased
    # circular edge, then downsample back to the real size
    ss = 4
    big_d = diameter * ss
    disc = Image.new("RGBA", (big_d, big_d), (0, 0, 0, 0))
    ImageDraw.Draw(disc).ellipse((0, 0, big_d, big_d), fill=bg_fill)

    pad = int(big_d * 0.0)
    inner = big_d - pad * 2
    fitted = logo.copy()
    fitted.thumbnail((inner, inner), Image.LANCZOS)
    lx = (big_d - fitted.width) // 2
    ly = (big_d - fitted.height) // 2
    disc.paste(fitted, (lx, ly), fitted)

    # hard circular clip - removes every pixel (fill AND logo) outside the circle
    clip_mask = Image.new("L", (big_d, big_d), 0)
    ImageDraw.Draw(clip_mask).ellipse((0, 0, big_d, big_d), fill=255)
    clipped = Image.new("RGBA", (big_d, big_d), (0, 0, 0, 0))
    clipped.paste(disc, (0, 0), clip_mask)
    clipped = clipped.resize((diameter, diameter), Image.LANCZOS)

    card.paste(clipped, (cx - radius, cy - radius), clipped)
    d.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline=ring_color, width=3)


def draw_progress_bar(card: Image.Image, bounds, ratio: float, c1, c2):
    x1, y1, x2, y2 = bounds
    d = ImageDraw.Draw(card)
    radius = (y2 - y1) // 2
    d.rounded_rectangle(bounds, radius=radius, fill=(255, 255, 255, 20), outline=(255, 255, 255, 40), width=1)
    ratio = max(0.0, min(1.0, ratio))
    fw = int((x2 - x1) * ratio)
    if fw > radius:
        grad = Image.new("RGBA", (fw, y2 - y1), (0, 0, 0, 0))
        gd = ImageDraw.Draw(grad)
        for i in range(fw):
            t = i / max(fw - 1, 1)
            gd.line((i, 0, i, y2 - y1), fill=tuple(int(c1[k] * (1 - t) + c2[k] * t) for k in range(3)) + (255,))
        mask = Image.new("L", (fw, y2 - y1), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, fw, y2 - y1), radius=radius, fill=255)
        card.paste(grad, (x1, y1), mask)


# ---------------------------------------------------------------------------
# BUILD THE HALL OF FAME
# ---------------------------------------------------------------------------
def render(
    users: list[tuple[str, int, int]],
    out_path: str | None = None,
    avatars: list[bytes] | None = None,
    logo_path: str | Path | None = None,
):
    background = load_random_background(BG_FOLDER, (W, H)).convert("RGBA")
    avatars = avatars or [None] * len(users)
    if len(avatars) != len(users):
        avatars = [None] * len(users)

    # slight overall darken + a few colour glows, same trick as the rank card bg
    shade = Image.new("RGBA", (W, H), (10, 10, 20, 80))
    card = Image.alpha_composite(background, shade)
    glow_overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    god = ImageDraw.Draw(glow_overlay)
    for x, y, c in [(300, 250, (0, 180, 255, 60)), (1600, 300, (255, 0, 255, 45)), (900, 950, (0, 255, 180, 45))]:
        god.ellipse((x - 200, y - 200, x + 200, y + 200), fill=c)
    glow_overlay = glow_overlay.filter(ImageFilter.GaussianBlur(90))
    card = Image.alpha_composite(card, glow_overlay)

    draw_card_frame(card)
    draw = ImageDraw.Draw(card)

    max_xp = max(u[1] for u in users) or 1
    total_xp = sum(u[1] for u in users)

    # ---- left summary panel -------------------------------------------------
    left_bounds = (40, 40, 470, H - 40)
    glass_panel(card, left_bounds, RADIUS_CARD)
    logo = load_logo(logo_path)
    if logo is not None:
        draw_logo_circle(card, (255, 245), 130, logo, ACCENT_CYAN, (18, 22, 38, 255))
    else:
        draw_avatar_circle(card, (255, 245), 130, (40, 55, 90, 255), ACCENT_CYAN)
        draw.text((255, 245), "RS", font=font(70), fill="white", anchor="mm")
    section_label(draw, (80, 470), "TOTAL XP AWARDED")
    draw.text((80, 500), f"{total_xp:,}", font=font(52), fill=TEXT_HEADING)
    section_label(draw, (80, 620), "LEGENDS")
    draw.text((80, 650), str(len(users)), font=font(52), fill=(120, 255, 170, 255))
    section_label(draw, (80, 770), "TOP SCORE")
    draw.text((80, 800), f"{users[0][1]:,}", font=font(52), fill=ACCENT_CYAN)

    # ---- header banner --------------------------------------------------
    header_bounds = (510, 40, W - 40, 180)
    glass_panel(card, header_bounds, RADIUS_PANEL)
    section_label(draw, (560, 68), "RYNEX SECURITY")
    draw.text((558, 100), "Hall of Fame", font=font(58), fill=TEXT_HEADING)

    # ---- table container --------------------------------------------------
    table_bounds = (510, 220, W - 40, H - 40)
    glass_panel(card, table_bounds, RADIUS_CARD)
    section_label(draw, (560, 250), "RANK", 15)
    section_label(draw, (760, 250), "USER", 15)
    section_label(draw, (1440, 250), "LEVEL", 15)
    section_label(draw, (1620, 250), "TOTAL XP", 15)

    row_top = 310
    row_h = (table_bounds[3] - row_top - 40) / len(users)
    for i, (name, xp, level) in enumerate(users):
        y1 = row_top + i * row_h
        y2 = y1 + row_h - 16
        row_bounds = (540, y1, W - 70, y2)
        rank_color = RANK_COLORS[min(i, len(RANK_COLORS) - 1)]
        glass_panel(card, row_bounds, RADIUS_PANEL, glow=(*rank_color[:3], 55))

        cy = int((y1 + y2) / 2)
        # rank number + coloured ring
        draw_avatar_circle(card, (600, cy), 26, (*rank_color[:3], 60), rank_color)
        draw.text((600, cy), f"#{i + 1}", font=fit_text(draw, f"#{i+1}", 44, 24, 14), fill=TEXT_HEADING, anchor="mm")

        # avatar placeholder + name
        if avatars[i] is not None:
            draw_avatar_image(card, (720, cy), 32, avatars[i], rank_color, (70 + i * 15, 110 + i * 10, 220 - i * 15, 255))
        else:
            draw_avatar_circle(card, (720, cy), 32, (70 + i * 15, 110 + i * 10, 220 - i * 15, 255), rank_color)
        draw.text((770, cy), name, font=fit_text(draw, name, 600, 32, 18), fill=TEXT_HEADING, anchor="lm")

        # level
        draw.text((1440, cy), f"Lv. {level}", font=fit_text(draw, f"Lv. {level}", 140, 30, 18),
                   fill=TEXT_MUTED, anchor="lm")

        # xp total
        draw.text((1620, cy), f"{xp:,} XP", font=fit_text(draw, f"{xp:,} XP", 190, 30, 18),
                   fill=(120, 255, 170, 255), anchor="lm")

    if out_path is None:
        output = io.BytesIO()
        card.convert("RGB").save(output, "PNG")
        output.seek(0)
        return output

    card.convert("RGB").save(out_path)
    print("Generated", out_path)
    return out_path


if __name__ == "__main__":
    render(USERS)
