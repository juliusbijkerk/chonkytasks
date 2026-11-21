#!/usr/bin/env python3
"""Render the chonkytasks SVG brand mark into the macOS icon sizes."""
import sys
from pathlib import Path
import xml.etree.ElementTree as ET

from PIL import Image, ImageDraw

SIZE = 1024
BG = (28, 20, 10, 255)
MARK = Path(__file__).resolve().parent.parent / 'static/brand/mark-mistral.svg'


def draw(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = size / SIZE  # scale unit
    # Inset rounded tile: macOS does not mask a legacy .icns automatically.
    d.rounded_rectangle([80 * u, 80 * u, 944 * u, 944 * u], radius=194 * u, fill=BG)
    # The upstream mark consists entirely of rect pixels; render those exact
    # shapes without a network dependency or a separate SVG renderer.
    pixel = 62 * u
    left, top = (size - 10 * pixel) / 2, (size - 8 * pixel) / 2
    for rect in ET.parse(MARK).getroot():
        if rect.tag != '{http://www.w3.org/2000/svg}rect':
            raise ValueError('The icon renderer expects the original rectangular brand pixels.')
        x = left + float(rect.attrib['x']) * pixel
        y = top + float(rect.attrib['y']) * pixel
        width = float(rect.attrib['width']) * pixel
        height = float(rect.attrib['height']) * pixel
        d.rectangle([round(x), round(y), round(x + width) - 1, round(y + height) - 1], fill=rect.attrib['fill'])
    return img


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "chonkytasks.iconset"
    out.mkdir(parents=True, exist_ok=True)
    base = draw(SIZE)
    names = {
        "icon_16x16.png": 16, "icon_16x16@2x.png": 32,
        "icon_32x32.png": 32, "icon_32x32@2x.png": 64,
        "icon_128x128.png": 128, "icon_128x128@2x.png": 256,
        "icon_256x256.png": 256, "icon_256x256@2x.png": 512,
        "icon_512x512.png": 512, "icon_512x512@2x.png": 1024,
    }
    for name, size in names.items():
        base.resize((size, size), Image.LANCZOS).save(out / name)
    print(f"wrote {len(names)} icons to {out}")


if __name__ == "__main__":
    main()
