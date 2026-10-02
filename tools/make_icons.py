"""Generate the favicon and touch icon from the design tokens. Run once; commit the PNGs.

    python tools/make_icons.py

Pillow is a build-time-only dependency (requirements-dev.txt) — the app never
imports it. The mark is the nav logo: a yellow tile, a fat black stroke, and a
heavy "B", which is what the home-screen icon should look like next to the app.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Straight from tokens.css
YELLOW = "#F4D738"
INK = "#111111"

ICONS = Path(__file__).resolve().parent.parent / "static" / "icons"
FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def draw_icon(size: int, *, maskable: bool = False) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # Maskable icons get cropped to a circle by the OS, so keep the art inside
    # the safe zone (the middle 80%) and let the yellow bleed to the edges.
    pad = 0 if maskable else round(size * 0.06)
    radius = 0 if maskable else round(size * 0.22)
    stroke = max(2, round(size * 0.045))

    draw.rounded_rectangle(
        [pad, pad, size - pad - 1, size - pad - 1],
        radius=radius, fill=YELLOW, outline=INK, width=stroke,
    )

    glyph_size = round(size * (0.46 if maskable else 0.58))
    font = _font(glyph_size)
    box = draw.textbbox((0, 0), "B", font=font)
    draw.text(
        ((size - (box[2] - box[0])) / 2 - box[0],
         (size - (box[3] - box[1])) / 2 - box[1]),
        "B", font=font, fill=INK,
    )
    return image


def main() -> None:
    ICONS.mkdir(parents=True, exist_ok=True)
    # Only the two files base.html links to. The PWA manifest set was dropped
    # with the PWA; regenerate it here if a manifest ever comes back.
    outputs = {
        # iOS has no transparency handling for the home-screen icon, so it
        # gets its own flattened file.
        "apple-touch-icon.png": draw_icon(180).convert("RGB"),
        "favicon-32.png": draw_icon(32),
    }
    for name, image in outputs.items():
        image.save(ICONS / name)
        print(f"wrote {name} ({image.size[0]}px)")


if __name__ == "__main__":
    main()
