"""AutoCopy's shared icon artwork and Windows icon export."""

from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageTk


@lru_cache(maxsize=16)
def make_icon(active=True, size=64):
    # Draw at 4x the largest exported size so small icons have smooth edges.
    scale = 4
    extent = 256 * scale
    image = Image.new("RGBA", (extent, extent))
    mask = Image.new("L", image.size)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((8 * scale, 8 * scale, 248 * scale, 248 * scale),
                           radius=54 * scale, fill=255)
    top, bottom = ((65, 141, 239), (32, 83, 183)) if active else ((148, 155, 165), (91, 100, 114))
    gradient = ImageDraw.Draw(image)
    for y in range(extent):
        fraction = y / (extent - 1)
        color = tuple(round(a + (b - a) * fraction) for a, b in zip(top, bottom))
        gradient.line((0, y, extent, y), fill=(*color, 255))
    image.putalpha(mask)
    draw = ImageDraw.Draw(image)

    def rect(box, radius, **kwargs):
        draw.rounded_rectangle(tuple(round(value * scale) for value in box),
                               radius=radius * scale, **kwargs)

    # A separate outline behind a solid front sheet reads as "copy" at 16 px.
    rect((90, 53, 184, 167), 14, outline="#e1eeff", width=9 * scale)
    rect((57, 85, 153, 203), 15, fill="#173f86" if active else "#46515f")
    rect((51, 79, 147, 197), 15, fill="#ffffff")
    ink = "#437bd0" if active else "#87929f"
    for y, right in ((111, 121), (135, 121), (159, 106)):
        draw.line((76 * scale, y * scale, right * scale, y * scale), fill=ink, width=8 * scale)

    # A single four-point sparkle signifies the automatic action.
    cx, cy = 194, 67
    points = [(cx, cy - 29), (cx + 8, cy - 8), (cx + 29, cy), (cx + 8, cy + 8),
              (cx, cy + 29), (cx - 8, cy + 8), (cx - 29, cy), (cx - 8, cy - 8)]
    draw.polygon([(x * scale, y * scale) for x, y in points],
                 fill="#a5f3d2" if active else "#d5dce4")
    return image.resize((size, size), Image.Resampling.LANCZOS)


def set_window_icon(window):
    # Keeping the PhotoImages alive also keeps the Windows title-bar icons alive.
    window._autocopy_icons = tuple(ImageTk.PhotoImage(make_icon(size=size), master=window)
                                   for size in (16, 32, 48, 256))
    window.iconphoto(False, *window._autocopy_icons)


def export_icons():
    destination = Path(__file__).resolve().parent / "assets"
    destination.mkdir(exist_ok=True)
    make_icon(size=256).save(destination / "autocopy.png")
    make_icon(False, 256).save(destination / "autocopy-paused.png")
    make_icon(size=256).save(destination / "autocopy.ico", format="ICO",
                              sizes=[(size, size) for size in (16, 20, 24, 32, 40, 48, 64, 128, 256)])
    print(f"Icons saved to {destination}")


if __name__ == "__main__":
    export_icons()
