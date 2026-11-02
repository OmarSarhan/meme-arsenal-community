"""Render the saved remix recipe as a portable image, without an AI request."""

from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps


def render_remix_card(
    output: Path,
    sources: list[tuple[str, Path | None]],
    tweaks: dict[str, Any] | None,
) -> bytes:
    canvas = Image.new("RGB", (1000, 1460), "#f8faf9")
    draw = ImageDraw.Draw(canvas)

    def font(size: int):
        try:
            return ImageFont.truetype("DejaVuSans.ttf", size)
        except OSError:
            return ImageFont.load_default(size=size)

    def text(value: str, x: int, y: int, width: int, size: int = 23, color: str = "#24312e"):
        face = font(size)
        value = " ".join(str(value).split())[:300]
        while value and draw.textlength(value, font=face) > width:
            value = value[:-2].rstrip() + "~"
        draw.text((x, y), value, font=face, fill=color)

    def picture(path: Path | None, box: tuple[int, int, int, int]):
        x, y, width, height = box
        draw.rounded_rectangle((x, y, x + width, y + height), radius=8, fill="#e8eeec")
        if path is None:
            text("Source unavailable", x + 18, y + height // 2, width - 36)
            return
        try:
            with Image.open(path) as original:
                image = ImageOps.contain(ImageOps.exif_transpose(original).convert("RGBA"), (width, height))
                canvas.paste(image, (x + (width - image.width) // 2, y + (height - image.height) // 2), image)
        except (OSError, ValueError):
            text("Source unavailable", x + 18, y + height // 2, width - 36)

    text("MEME ARSENAL / REMIX CARD", 40, 24, 920, 30)
    for index, label in enumerate(("A", "B")):
        x = 40 + index * 480
        title, path = sources[index] if index < len(sources) else ("Unavailable", None)
        picture(path, (x, 106, 440, 230))
        color = "#c53d55" if index == 0 else "#237c57"
        corner = x if index == 0 else x + 440
        draw.polygon(((corner, 106), (corner + (54 if index == 0 else -54), 106), (corner, 160)), fill=color)
        text(label, corner + (8 if index == 0 else -26), 109, 35, 23, "#ffffff")
        text(title, x, 70, 440, 26)
        # Straight arrows point down from each input into the output.
        start = (x + 220, 348)
        end = (x + 220, 394)
        draw.line((start, end), fill=color, width=4)
        draw.polygon((end, (end[0] - 9, end[1] - 12), (end[0] + 9, end[1] - 12)), fill=color)
    picture(output, (40, 400, 920, 570))
    text("GENERATION TWEAKS", 40, 995, 920, 30)
    if tweaks is None:
        text("No tweak snapshot available for this remix", 40, 1050, 920)
    else:
        rows = [
            ("Schizophrenic", tweaks.get("schizo_level", 0), "off (0)", "high (3)"),
            ("Chronically online", tweaks.get("chronically_online_level", 0), "off (0)", "high (3)"),
        ]
        values = tweaks.get("values") or {}
        for control in (tweaks.get("controls") or [])[:4]:
            rows.append((control.get("label", "Tweak"), values.get(control.get("id"), control.get("default_value", 50)),
                         f'{control.get("low_label", "Low")} (0)', f'{control.get("high_label", "High")} (100)'))
        for index, (label, value, low, high) in enumerate(rows):
            x = 40 + (index % 2) * 480
            y = 1040 + (index // 2) * 98
            draw.rounded_rectangle((x, y, x + 440, y + 86), radius=6, fill="#e6f0ec" if index % 2 else "#f7e7ec")
            text(label, x + 12, y + 6, 325, 29)
            text(str(value), x + 350, y + 2, 80, 34)
            text(low, x + 12, y + 50, 205, 22)
            text(high, x + 232, y + 50, 196, 22)
        flags = [f'{label}: {"on" if tweaks.get(key) else "off"}' for key, label in (
            ("no_text", "Text free"), ("anti_slop", "Anti-slop"), ("comic_strip", "Comic strip"))]
        text("  /  ".join(flags), 40, 1352, 920, 27)
    buffer = BytesIO()
    canvas.save(buffer, format="PNG")
    return buffer.getvalue()
