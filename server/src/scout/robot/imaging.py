"""Picture helpers: sizes, previews, labelled grids, contact sheets and boxes (Pillow)."""

from __future__ import annotations

import io
import math
from collections.abc import Sequence
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

FontType = ImageFont.FreeTypeFont | ImageFont.ImageFont


def is_jpeg(data: bytes) -> bool:
    return len(data) > 4 and data[:2] == b"\xff\xd8"


def jpeg_size(data: bytes) -> tuple[int, int]:
    """(width, height) of a JPEG; raises ``ValueError`` if it isn't one."""
    if not is_jpeg(data):
        raise ValueError("not a JPEG")
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.format != "JPEG":
                raise ValueError("not a JPEG")
            return img.size
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError("unreadable JPEG") from exc


def _encode(img: Image.Image, quality: int) -> bytes:
    out = io.BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=quality, optimize=True)
    return out.getvalue()


def _open_rgb(data: bytes) -> Image.Image | None:
    try:
        with Image.open(io.BytesIO(data)) as img:
            return img.convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


def preview(jpeg: bytes, *, max_width: int = 480, quality: int = 75) -> bytes:
    """A small copy of a picture, for showing a capture without sending it full size."""
    img = _open_rgb(jpeg)
    if img is None:
        img = _placeholder(max_width, max_width * 3 // 4)
    if img.width > max_width:
        img = img.resize(
            (max_width, max(1, round(img.height * max_width / img.width))), Image.Resampling.LANCZOS
        )
    return _encode(img, quality)


@lru_cache(maxsize=16)
def _font(size: int) -> FontType:
    try:
        return ImageFont.load_default(size=size)
    except (OSError, TypeError, ValueError):  # pragma: no cover - Pillow without FreeType
        return ImageFont.load_default()


def _placeholder(width: int, height: int) -> Image.Image:
    img = Image.new("RGB", (width, height), (70, 70, 70))
    ImageDraw.Draw(img).text((8, 8), "picture unreadable", fill=(230, 230, 230), font=_font(14))
    return img


def labelled_grid(
    tiles: Sequence[tuple[bytes, str]],
    *,
    columns: int,
    max_width: int = 1280,
    tile_width: int = 640,
    quality: int = 80,
) -> bytes:
    """Lay pictures out left to right, top to bottom, each with a caption strip.

    Tiles keep a 4:3 shape and are at most ``tile_width`` wide; the whole grid is at
    most ``max_width`` wide.
    """
    if not tiles:
        raise ValueError("no tiles")
    columns = max(1, min(columns, len(tiles)))
    rows = math.ceil(len(tiles) / columns)
    gap = 4
    tw = max(40, min(tile_width, (max_width - gap * (columns + 1)) // columns))
    th = tw * 3 // 4
    font_size = max(11, tw // 20)
    font = _font(font_size)
    strip = font_size + 10
    sheet = Image.new("RGB", (gap + columns * (tw + gap), gap + rows * (th + strip + gap)), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    for i, (data, label) in enumerate(tiles):
        img = _open_rgb(data) or _placeholder(tw, th)
        img = img.resize((tw, th), Image.Resampling.LANCZOS)
        x = gap + (i % columns) * (tw + gap)
        y = gap + (i // columns) * (th + strip + gap)
        sheet.paste(img, (x, y))
        draw.rectangle((x, y + th, x + tw - 1, y + th + strip - 1), fill=(40, 40, 40))
        draw.text((x + 6, y + th + 4), label, fill=(240, 240, 240), font=font)
    return _encode(sheet, quality)


Box = tuple[float, float, float, float]  # x0, y0, x1, y1 in 0..1 of the picture
GREEN = (40, 220, 90)
YELLOW = (250, 200, 40)


def annotate(
    jpeg: bytes,
    boxes: Sequence[tuple[Box, str, tuple[int, int, int]]],
    *,
    min_width: int = 640,
    quality: int = 85,
) -> bytes:
    """The picture with each box outlined and labelled, scaled up to at least ``min_width``
    so the labels stay legible on a small stream frame."""
    img = _open_rgb(jpeg) or _placeholder(min_width, min_width * 3 // 4)
    if img.width < min_width:
        img = img.resize((min_width, round(img.height * min_width / img.width)), Image.Resampling.LANCZOS)
    draw = ImageDraw.Draw(img)
    font_size = max(14, img.width // 32)
    font = _font(font_size)
    line = max(2, img.width // 213)
    for (x0, y0, x1, y1), label, colour in boxes:
        left, top = round(x0 * img.width), round(y0 * img.height)
        right, bottom = round(x1 * img.width) - 1, round(y1 * img.height) - 1
        draw.rectangle((left, top, right, bottom), outline=colour, width=line)
        if not label:
            continue
        text_box = draw.textbbox((0, 0), label, font=font)
        tw, th = text_box[2] - text_box[0] + 8, text_box[3] - text_box[1] + 8
        ty = top - th if top - th >= 0 else top + line  # above the box, or just inside it
        tx = min(max(0, left), img.width - tw)
        draw.rectangle((tx, ty, tx + tw, ty + th), fill=colour)
        draw.text((tx + 4, ty + 4 - text_box[1]), label, fill=(0, 0, 0), font=font)
    return _encode(img, quality)


def crop(jpeg: bytes, box: Box, *, pad: float = 0.15, height: int = 320, quality: int = 85) -> bytes:
    """A close-up of ``box`` with some margin, scaled to ``height`` pixels tall."""
    img = _open_rgb(jpeg)
    if img is None:
        return _encode(_placeholder(height * 3 // 4, height), quality)
    x0, y0, x1, y1 = box
    px, py = (x1 - x0) * pad, (y1 - y0) * pad
    left, top = max(0, round((x0 - px) * img.width)), max(0, round((y0 - py) * img.height))
    right = min(img.width, max(left + 1, round((x1 + px) * img.width)))
    bottom = min(img.height, max(top + 1, round((y1 + py) * img.height)))
    part = img.crop((left, top, right, bottom))
    scale = height / part.height
    part = part.resize((max(1, round(part.width * scale)), height), Image.Resampling.LANCZOS)
    return _encode(part, quality)
