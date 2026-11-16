#!/usr/bin/env python3
"""Generate the add-on icon and logo (run once, commit the PNGs).

    python tools/make_branding.py

Outputs:
  grok2api/icon.png  128x128 rounded gradient tile with the "G2" mark
  grok2api/logo.png  250x100 wordmark for the add-on store
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

APP = Path(__file__).resolve().parents[1] / "grok2api"
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\segoeuib.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in FONT_CANDIDATES:
        path = Path(candidate)
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size)


def gradient(size: tuple[int, int], start: tuple[int, int, int],
             end: tuple[int, int, int]) -> Image.Image:
    width, height = size
    image = Image.new("RGB", size)
    drawer = ImageDraw.Draw(image)
    for y in range(height):
        ratio = y / max(height - 1, 1)
        color = tuple(round(start[i] + (end[i] - start[i]) * ratio) for i in range(3))
        drawer.line([(0, y), (width, y)], fill=color)
    return image


def rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius=radius, fill=255)
    return mask


def make_icon(path: Path) -> None:
    size = 128
    base = gradient((size, size), (37, 99, 235), (124, 58, 237))
    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    tile.paste(base, (0, 0), rounded_mask((size, size), 26))

    drawer = ImageDraw.Draw(tile)
    # chat bubble outline
    drawer.rounded_rectangle([22, 26, 106, 88], radius=18, outline=(255, 255, 255, 235), width=5)
    drawer.polygon([(44, 88), (44, 106), (66, 88)], fill=(255, 255, 255, 235))

    font = load_font(34)
    text = "G2"
    left, top, right, bottom = drawer.textbbox((0, 0), text, font=font)
    drawer.text(((size - (right - left)) / 2 - left, (78 - (bottom - top)) / 2 - top + 16),
                text, font=font, fill=(255, 255, 255, 255))
    tile.save(path)


def make_logo(path: Path) -> None:
    width, height = 250, 100
    logo = Image.new("RGBA", (width, height), (0, 0, 0, 0))

    tile_size = 76
    base = gradient((tile_size, tile_size), (37, 99, 235), (124, 58, 237))
    tile = Image.new("RGBA", (tile_size, tile_size), (0, 0, 0, 0))
    tile.paste(base, (0, 0), rounded_mask((tile_size, tile_size), 18))
    drawer = ImageDraw.Draw(tile)
    drawer.rounded_rectangle([14, 17, 62, 52], radius=11, outline=(255, 255, 255, 235), width=4)
    drawer.polygon([(27, 52), (27, 63), (40, 52)], fill=(255, 255, 255, 235))
    font_small = load_font(21)
    left, top, right, bottom = drawer.textbbox((0, 0), "G2", font=font_small)
    drawer.text(((tile_size - (right - left)) / 2 - left, (46 - (bottom - top)) / 2 - top + 9),
                "G2", font=font_small, fill=(255, 255, 255, 255))
    logo.alpha_composite(tile, (4, 12))

    drawer = ImageDraw.Draw(logo)
    font = load_font(30)
    drawer.text((92, 22), "Grok2API", font=font, fill=(23, 23, 33, 255))
    font_sub = load_font(15)
    drawer.text((93, 58), "for Home Assistant", font=font_sub, fill=(90, 90, 105, 255))
    logo.save(path)


def main() -> int:
    make_icon(APP / "icon.png")
    make_logo(APP / "logo.png")
    for name in ("icon.png", "logo.png"):
        image = Image.open(APP / name)
        print(f"{name}: {image.size} {image.mode} {Path(APP / name).stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
