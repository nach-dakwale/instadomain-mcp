#!/usr/bin/env python3
"""Generate the InstaDomain product image used by the Stripe catalog feed.

Produces a 1024x1024 PNG at instadomain/static/og.png. Re-runnable; output
is deterministic given the same Pillow version.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


SIZE = 1024
BG = (10, 10, 12)
INK = (240, 240, 240)
ACCENT = (88, 200, 130)


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    candidates = [
        "/System/Library/Fonts/Supplemental/Helvetica.ttc",
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/SFNS.ttf",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def main() -> None:
    img = Image.new("RGB", (SIZE, SIZE), BG)
    draw = ImageDraw.Draw(img)

    cx = cy = SIZE // 2
    # Concentric rings echoing the homepage motif.
    for radius, width, color in [
        (360, 4, (40, 40, 44)),
        (260, 3, (60, 60, 66)),
        (160, 2, ACCENT),
    ]:
        draw.ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius),
            outline=color,
            width=width,
        )

    wordmark_font = _load_font(96)
    tag_font = _load_font(36)

    wordmark = "instadomain"
    bbox = draw.textbbox((0, 0), wordmark, font=wordmark_font)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    draw.text(
        ((SIZE - w) // 2, cy - h // 2 - 12),
        wordmark,
        fill=INK,
        font=wordmark_font,
    )

    tagline = "domains for AI agents"
    bbox = draw.textbbox((0, 0), tagline, font=tag_font)
    w = bbox[2] - bbox[0]
    draw.text(
        ((SIZE - w) // 2, cy + 70),
        tagline,
        fill=ACCENT,
        font=tag_font,
    )

    out = Path(__file__).resolve().parent.parent / "instadomain" / "static" / "og.png"
    img.save(out, "PNG", optimize=True)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
