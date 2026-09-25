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
        return username[:16]
    if first_name:
        return first_name[:16]
    return str(user_id)


def render_deal(players: list[dict]) -> BytesIO:
    """players: {username, first_name, user_id, card_key}"""
    card_target_h = 420
    gap = 24
    label_h = 48
    pad = 28

    cards: list[tuple[str, Image.Image]] = []
    for p in players:
        raw = load_card(p["card_key"])
        ratio = card_target_h / raw.height
        w = max(1, int(raw.width * ratio))
        cards.append((display_name(p.get("username"), p.get("first_name"), p["user_id"]), raw.resize((w, card_target_h))))

    total_w = pad * 2 + sum(im.width for _, im in cards) + gap * (len(cards) - 1)
    total_h = pad * 2 + label_h + card_target_h
    canvas = Image.new("RGB", (total_w, total_h), (248, 248, 248))
    draw = ImageDraw.Draw(canvas)
    font = _font(26)

    x = pad
    for name, im in cards:
        tw = draw.textlength(name, font=font)
        draw.text((x + (im.width - tw) / 2, pad + 8), name, fill=(20, 20, 20), font=font)
        if im.mode == "RGBA":
            canvas.paste(im, (x, pad + label_h), im)
        else:
            canvas.paste(im, (x, pad + label_h))
        x += im.width + gap

    buf = BytesIO()
    canvas.save(buf, format="PNG")
    buf.seek(0)
    return buf
