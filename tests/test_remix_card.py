from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

from meme_arsenal.remix_card import render_remix_card


def test_card_renders_inputs_output_and_original_tweak_scales(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    Image.new("RGB", (400, 300), "#d8b0c8").save(source)
    output = tmp_path / "output.png"
    Image.new("RGB", (600, 450), "#b4dbca").save(output)
    texts = []
    original_text = ImageDraw.ImageDraw.text

    def capture(draw, xy, text, *args, **kwargs):
        texts.append(text)
        return original_text(draw, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    png = render_remix_card(output, [("First input", source), ("Second input", source)], {
        "schizo_level": 2, "chronically_online_level": 1,
        "controls": [{"id": "chaos", "label": "Chaos", "default_value": 50, "low_label": "Calm", "high_label": "Wild"}],
        "values": {"chaos": 68}, "no_text": True, "anti_slop": False, "comic_strip": True,
    })
    image = Image.open(BytesIO(png))
    assert image.size == (1000, 1460)
    assert image.getpixel((200, 200)) == (216, 176, 200)
    assert image.getpixel((500, 700)) == (180, 219, 202)
    for x, color in ((260, (197, 61, 85)), (740, (35, 124, 87))):
        assert image.getpixel((x, 350)) == color
        assert image.getpixel((x, 370)) == color
        assert image.getpixel((x, 394)) == color
    assert {"68", "2", "1", "off (0)", "high (3)", "Calm (0)", "Wild (100)"} <= set(texts)
    assert "Text free: on / Anti-slop: off / Comic strip: on" in texts
    screenshot = Path(__file__).resolve().parents[1] / "tmp/remix-card.png"
    screenshot.parent.mkdir(exist_ok=True)
    screenshot.write_bytes(png)


def test_old_card_missing_sources_and_snapshot_still_renders(tmp_path):
    output = tmp_path / "output.png"
    Image.new("RGB", (40, 60), "white").save(output)
    image = Image.open(BytesIO(render_remix_card(output, [], None)))
    assert image.size == (1000, 1460)
