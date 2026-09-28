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


RANK_ICON_SIZE = 30
RANK_ROW_GAP = 22
RANK_TEXT_SIZE = 34
RANK_META_SIZE = 26
RANK_TITLE_SIZE = 42
RANK_ICON_GAP = 12
RANK_LEFT_PAD = 28
RANK_RIGHT_PAD = 28
RANK_TOP_PAD = 26
RANK_BOTTOM_PAD = 28
RANK_ASSET_DIR = BASE_DIR / "assets" / "ranking"


def _load_rank_icon(kind: str, size: int = RANK_ICON_SIZE) -> Image.Image:
    """Load and proportionally scale a supplied ranking PNG."""
    path = RANK_ASSET_DIR / f"{kind}.png"
    if not path.exists():
        return Image.new("RGBA", (size, size), (0, 0, 0, 0))
    raw = Image.open(path).convert("RGBA")
    scale = min(size / raw.width, size / raw.height)
    w = max(1, round(raw.width * scale))
    h = max(1, round(raw.height * scale))
    return raw.resize((w, h), Image.Resampling.LANCZOS)


def render_leaderboard(rows) -> BytesIO:
    """Render the leaderboard with the supplied PNG rank markers.

    Telegram text messages cannot embed arbitrary PNGs in place of emoji, so
    the leaderboard is rendered as a compact PNG when these custom markers
    are used. Text sizing and icon placement are kept proportional and
    consistent across all three markers.
    """
    title_font = _font(RANK_TITLE_SIZE)
    name_font = _font(RANK_TEXT_SIZE)
    meta_font = _font(RANK_META_SIZE)
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    title = "Tomochi Card Leaderboard"
    prepared = []
    max_text_w = 0
    top_score = int(rows[0]["score"]) if rows else 0
    sole_leader = len(rows) == 1 or top_score > int(rows[1]["score"])

    for i, row in enumerate(rows, start=1):
        prev_rank = row["prev_rank"]
        prev_score = row["prev_score"]
        score = int(row["score"])

        if i == 1 and sole_leader:
            marker = "crown"
        elif prev_rank is None:
            marker = None
        elif i < prev_rank and prev_score is not None and score > int(prev_score):
            marker = "up"
        elif i > prev_rank and prev_score is not None:
            dropped_points = score < int(prev_score)
            passed_from_below = any(
                other["prev_score"] is not None
                and int(other["prev_score"]) < int(prev_score)
                for j, other in enumerate(rows, start=1)
                if j < i
            )
            marker = "down" if (dropped_points or passed_from_below) else None
        else:
            marker = None

        if row.get("username"):
            who = f"@{row['username']}"
        else:
            who = row.get("first_name") or str(row["user_id"])

        meta = f"Score: {row['score']} | Played: {row['played']} | Wins: {row['wins']}"
        prepared.append((marker, who, meta))
        max_text_w = max(max_text_w, int(measure.textlength(who, font=name_font)), int(measure.textlength(meta, font=meta_font)))

    title_w = int(measure.textlength(title, font=title_font))
    width = max(
        700,
        RANK_LEFT_PAD + title_w + RANK_RIGHT_PAD,
        RANK_LEFT_PAD + RANK_ICON_SIZE + RANK_ICON_GAP + max_text_w + RANK_RIGHT_PAD,
    )

    row_heights = []
    for marker, who, meta in prepared:
        who_box = name_font.getbbox(who)
        meta_box = meta_font.getbbox(meta)
        row_heights.append((who_box[3] - who_box[1]) + 8 + (meta_box[3] - meta_box[1]))

    title_box = title_font.getbbox(title)
    height = (
        RANK_TOP_PAD
        + (title_box[3] - title_box[1])
        + 30
        + sum(row_heights)
        + RANK_ROW_GAP * max(0, len(prepared) - 1)
        + RANK_BOTTOM_PAD
    )
    canvas = Image.new("RGB", (width, height), (248, 248, 248))
    draw = ImageDraw.Draw(canvas)

    y = RANK_TOP_PAD
    draw.text(((width - title_w) / 2, y - title_box[1]), title, fill=(20, 20, 20), font=title_font)
    y += title_box[3] - title_box[1] + 30

    for row_index, (marker, who, meta) in enumerate(prepared):
        row_h = row_heights[row_index]
        icon = _load_rank_icon(marker) if marker else None
        content_x = RANK_LEFT_PAD
        if icon is not None:
            icon_y = y + max(0, (row_h - icon.height) // 2)
            canvas.paste(icon, (content_x, icon_y), icon)
            content_x += RANK_ICON_SIZE + RANK_ICON_GAP

        who_box = draw.textbbox((0, 0), who, font=name_font)
        draw.text((content_x, y - who_box[1]), who, fill=(20, 20, 20), font=name_font)
        meta_y = y + (who_box[3] - who_box[1]) + 8
        meta_box = draw.textbbox((0, 0), meta, font=meta_font)
        draw.text((content_x, meta_y - meta_box[1]), meta, fill=(80, 80, 80), font=meta_font)
        y += row_h + RANK_ROW_GAP

    buf = BytesIO()
    canvas.save(buf, format="PNG")
    buf.seek(0)
    return buf
