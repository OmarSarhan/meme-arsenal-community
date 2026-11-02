from meme_arsenal.remix_page import render_remix_page


def test_recipe_page_keeps_full_text_and_escapes_html():
    text = "A detailed but readable description. " * 40 + "END OF DESCRIPTION"
    response = render_remix_page(
        image_url="/ABCDE/media",
        sources=[(text, "/ABCDE/card/image?source=A"), ('<script>alert("bad")</script>', None)],
        summary=text,
        tweaks={
            "summary": text,
            "controls": [{"id": "tone", "label": "A long label", "description": text,
                          "low_label": "A very long low endpoint", "high_label": "A very long high endpoint"}],
            "values": {"tone": 73}, "anti_slop": True,
        },
        public_url="https://forward-deployed-meme.example/ABCDE",
    )
    body = response.body.decode()
    assert body.count(text) == 5  # Four visible text blocks plus the social description.
    assert '<script>alert("bad")</script>' not in body
    assert "&lt;script&gt;" in body
    assert '<dd class="value">73</dd>' in body
    assert 'class="on">On' in body
    assert "A very long low endpoint" in body
    assert "text-overflow" not in body
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_private_recipe_preview_does_not_publish_or_offer_public_share():
    response = render_remix_page(image_url="/api/remix/jobs/example/image", sources=[], summary="", tweaks=None)
    assert 'id="share"' not in response.body.decode()
    assert "frame-ancestors 'self'" in response.headers["content-security-policy"]
    assert "no-store" in response.headers["cache-control"]


def test_recipe_video_sources_use_inline_playback_with_sound_and_posters():
    response = render_remix_page(
        image_url="/ABCDE/media", summary="Remix", tweaks=None,
        sources=[("Video ingredient", "/ABCDE/card/image?source=A"), ("Still image", "/ABCDE/card/image?source=B")],
        source_videos={"A": "/ABCDE/card/image?source=A&playback=true"},
    )
    body = response.body.decode()
    assert '<video src="/ABCDE/card/image?source=A&amp;playback=true"' in body
    assert 'poster="/ABCDE/card/image?source=A"' in body
    assert 'controls playsinline preload="none"' in body
    assert "disablepictureinpicture" in body
    assert " autoplay" not in body and " muted" not in body
    assert '<img src="/ABCDE/card/image?source=B"' in body
    assert "media-src 'self'" in response.headers["content-security-policy"]
