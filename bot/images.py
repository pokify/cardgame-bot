from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from bot.config import CARDS, CARDS_DIR


BASE_DIR = Path(__file__).resolve().parent.parent
BUNDLED_FONT = BASE_DIR / "assets" / "fonts" / "DejaVuSans-Bold.ttf"

# Render the source card artwork at 50% of its native 418x579 size.
CARD_SCALE = 0.50

# Typography is deliberately sized for the smaller composite image so Telegram
# does not have to downscale a very wide 3-4 player image as aggressively.
NAME_FONT_SIZE = 40
NAME_MIN_FONT_SIZE = 35
SCORE_FONT_SIZE = 40
SCORE_MIN_FONT_SIZE = 35


def _font(size: int) -> ImageFont.FreeTypeFont:
    """Load the bundled TTF, with known system-font fallbacks."""
    candidates = (
        BUNDLED_FONT,
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"),
    )

    for path in candidates:
        if not path.exists():
            continue
        try:
            return ImageFont.truetype(str(path), size)
        except (OSError, ValueError):
            continue

    raise RuntimeError(
        "No usable TrueType font found. Expected a valid font at "
        f"{BUNDLED_FONT} or a standard Linux font location."
    )


def _fit_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_width: int,
    start_size: int,
    min_size: int,
) -> tuple[ImageFont.FreeTypeFont, float]:
    """Fit text to a width without allowing the font to collapse below a floor."""
    size = start_size
    font = _font(size)
    width = draw.textlength(text, font=font)

    while width > max_width and size > min_size:
        size = max(min_size, size - 4)
        font = _font(size)
        width = draw.textlength(text, font=font)

    return font, width


def _placeholder_card(label: str, score: int, size: tuple[int, int] = (280, 400)) -> Image.Image:
    img = Image.new("RGB", size, (28, 28, 36))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        (8, 8, size[0] - 8, size[1] - 8),
        radius=24,
        outline=(230, 230, 230),
        width=4,
    )
    title = _font(48)
    sub = _font(28)
    tw = draw.textlength(label, font=title)
    draw.text(
        ((size[0] - tw) / 2, size[1] / 2 - 50),
        label,
        fill=(255, 255, 255),
        font=title,
    )
    sw = draw.textlength(str(score), font=sub)
    draw.text(
        ((size[0] - sw) / 2, size[1] / 2 + 16),
        str(score),
        fill=(200, 200, 200),
        font=sub,
    )
    return img


def load_card(card_key: str) -> Image.Image:
    meta = CARDS[card_key]
    path = CARDS_DIR / meta["file"]
    if path.exists():
        return Image.open(path).convert("RGBA")
    return _placeholder_card(meta["label"], meta["score"]).convert("RGBA")


def display_name(username: str | None, first_name: str | None, user_id: int) -> str:
    """Return the full display name; width-based truncation happens at render time."""
    if username:
        return str(username)
    if first_name:
        return str(first_name)
    return str(user_id)


def _clean_display_name(text: str) -> str:
    """Remove Unicode replacement characters without stripping normal Unicode."""
    return text.replace("\ufffd", "").strip()


def _truncate_to_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> tuple[str, float]:
    """Truncate by rendered pixel width and append ASCII dots for maximum font/encoding compatibility."""
    if draw.textlength(text, font=font) <= max_width:
        return text, draw.textlength(text, font=font)

    ellipsis = "..."
    ellipsis_w = draw.textlength(ellipsis, font=font)
    if ellipsis_w > max_width:
        return ellipsis, ellipsis_w

    truncated = text
    while truncated:
        candidate = truncated[:-1] + ellipsis
        width = draw.textlength(candidate, font=font)
        if width <= max_width:
            return candidate, width
        truncated = truncated[:-1]

    return ellipsis, ellipsis_w


def _scale_card(raw: Image.Image) -> Image.Image:
    """Scale card artwork to 50% while preserving its native aspect ratio."""
    w = max(1, round(raw.width * CARD_SCALE))
    h = max(1, round(raw.height * CARD_SCALE))

    if (w, h) == raw.size:
        return raw

    # LANCZOS gives a clean reduction without upscaling the card artwork.
    return raw.resize((w, h), Image.Resampling.LANCZOS)


def render_deal(players: list[dict]) -> BytesIO:
    """Render the existing deal as a compact Telegram-friendly composite.

    players: {username, first_name, user_id, card_key, score}
    """
    gap = 18
    pad_x = 18
    pad_y = 18
    name_area_h = 120
    score_area_h = 150

    cards: list[tuple[str, int, Image.Image]] = []
    for p in players:
        raw = load_card(p["card_key"])
        im = _scale_card(raw)
        name = display_name(p.get("username"), p.get("first_name"), p["user_id"])

        if "display_score" in p:
            score = p["display_score"]
        elif p.get("card_key") == "joker":
            score = -5
        else:
            score = p.get("score", CARDS[p["card_key"]]["score"])

        cards.append((name, score, im))

    card_h = max(im.height for _, _, im in cards)
    total_w = (
        pad_x * 2
        + sum(im.width for _, _, im in cards)
        + gap * (len(cards) - 1)
    )
    total_h = pad_y * 2 + name_area_h + card_h + score_area_h

    canvas = Image.new("RGB", (total_w, total_h), (248, 248, 248))
    draw = ImageDraw.Draw(canvas)

    x = pad_x
    for name, score, im in cards:
        # Keep the preferred username size. If it is too wide, truncate by
        # rendered pixel width and append ASCII dots instead of
        # shrinking the username to an unreadable size.
        name = _clean_display_name(name)
        name_font = _font(NAME_FONT_SIZE)
        name, name_w = _truncate_to_width(
            draw,
            name,
            name_font,
            max_width=max(1, im.width - 12),
        )

        # Fixed username zone: every name is centred at exactly the same
        # vertical position above its card, regardless of glyph shape.
        name_center_y = pad_y + name_area_h / 2
        draw.text(
            (x + im.width / 2, name_center_y),
            name,
            fill=(20, 20, 20),
            font=name_font,
            anchor="mm",
        )

        y_card = pad_y + name_area_h
        if im.mode == "RGBA":
            canvas.paste(im, (x, y_card), im)
        else:
            canvas.paste(im, (x, y_card))

        # The score gets its own large line below "Score:". This avoids
        # shrinking a 70-90px font just to fit "Score: 11" into 209px.
        score_label = "Score:"
        score_value = str(score)
        score_label_font = _font(40)
        score_value_font, score_w = _fit_font(
            draw,
            score_value,
            max_width=max(1, im.width - 12),
            start_size=SCORE_FONT_SIZE,
            min_size=SCORE_MIN_FONT_SIZE,
        )

        score_label_bbox = draw.textbbox((0, 0), score_label, font=score_label_font)
        score_value_bbox = draw.textbbox((0, 0), score_value, font=score_value_font)
        label_h = score_label_bbox[3] - score_label_bbox[1]
        value_h = score_value_bbox[3] - score_value_bbox[1]
        spacing = 10
        block_h = label_h + spacing + value_h
        score_top = y_card + im.height + max(0, (score_area_h - block_h) // 2)

        label_w = draw.textlength(score_label, font=score_label_font)
        draw.text(
            (x + (im.width - label_w) / 2, score_top - score_label_bbox[1]),
            score_label,
            fill=(20, 20, 20),
            font=score_label_font,
        )

        value_y = score_top + label_h + spacing - score_value_bbox[1]
        draw.text(
            (x + (im.width - score_w) / 2, value_y),
            score_value,
            fill=(20, 20, 20),
            font=score_value_font,
        )

        x += im.width + gap

    buf = BytesIO()
    canvas.save(buf, format="PNG")
    buf.seek(0)
    return buf