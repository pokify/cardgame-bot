from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from bot.config import CARDS, CARDS_DIR


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ):
        if Path(name).exists():
            return ImageFont.truetype(name, size)
    return ImageFont.load_default()


def _placeholder_card(label: str, score: int, size: tuple[int, int] = (280, 400)) -> Image.Image:
    img = Image.new("RGB", size, (28, 28, 36))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((8, 8, size[0] - 8, size[1] - 8), radius=24, outline=(230, 230, 230), width=4)
    title = _font(48)
    sub = _font(28)
    tw = draw.textlength(label, font=title)
    draw.text(((size[0] - tw) / 2, size[1] / 2 - 50), label, fill=(255, 255, 255), font=title)
    sw = draw.textlength(str(score), font=sub)
    draw.text(((size[0] - sw) / 2, size[1] / 2 + 16), str(score), fill=(200, 200, 200), font=sub)
    return img


def load_card(card_key: str) -> Image.Image:
    meta = CARDS[card_key]
    path = CARDS_DIR / meta["file"]
    if path.exists():
        img = Image.open(path).convert("RGBA")
        return img
    return _placeholder_card(meta["label"], meta["score"]).convert("RGBA")


def display_name(username: str | None, first_name: str | None, user_id: int) -> str:
    if username:
        return username[:18]
    if first_name:
        return first_name[:18]
    return str(user_id)


def render_deal(players: list[dict]) -> BytesIO:
    """players: {username, first_name, user_id, card_key, score}"""
    card_target_h = 420
    gap = 24
    label_h = 88
    score_h = 88
    pad = 28

    cards: list[tuple[str, str, Image.Image]] = []
    for p in players:
        raw = load_card(p["card_key"])
        ratio = card_target_h / raw.height
        w = max(1, int(raw.width * ratio))
        name = display_name(p.get("username"), p.get("first_name"), p["user_id"])
        if "display_score" in p:
            score = p["display_score"]
        elif p.get("card_key") == "joker":
            score = -5
        else:
            score = p.get("score", CARDS[p["card_key"]]["score"])
        cards.append((name, f"Score: {score}", raw.resize((w, card_target_h))))

    total_w = pad * 2 + sum(im.width for _, _, im in cards) + gap * (len(cards) - 1)
    total_h = pad * 2 + label_h + card_target_h + score_h
    canvas = Image.new("RGB", (total_w, total_h), (248, 248, 248))
    draw = ImageDraw.Draw(canvas)

    min_card_w = min(im.width for _, _, im in cards)
    font_size = max(36, min(56, int(min_card_w * 0.16)))
    font = _font(font_size)

    x = pad
    for name, score_text, im in cards:
        tw = draw.textlength(name, font=font)
        draw.text(
            (x + (im.width - tw) / 2, pad + (label_h - font_size) / 2),
            name,
            fill=(20, 20, 20),
            font=font,
        )
        y_card = pad + label_h
        if im.mode == "RGBA":
            canvas.paste(im, (x, y_card), im)
        else:
            canvas.paste(im, (x, y_card))
        sw = draw.textlength(score_text, font=font)
        draw.text(
            (x + (im.width - sw) / 2, y_card + im.height + (score_h - font_size) / 2),
            score_text,
            fill=(20, 20, 20),
            font=font,
        )
        x += im.width + gap

    buf = BytesIO()
    canvas.save(buf, format="PNG")
    buf.seek(0)
    return buf
