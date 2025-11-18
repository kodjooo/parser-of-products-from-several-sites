from __future__ import annotations

from app.media.image_saver import _guess_extension


def test_guess_extension_prefers_content_type():
    url = "https://cdn.example.com/file"
    assert _guess_extension(url, "image/webp") == "webp"
    assert _guess_extension(url, "image/avif; charset=utf-8") == "avif"
    assert _guess_extension(url, "image/svg+xml") == "svg"


def test_guess_extension_falls_back_to_url():
    url = "https://cdn.example.com/promo/shot.webp?size=large"
    assert _guess_extension(url, None) == "webp"
    assert _guess_extension("https://cdn.example.com/logo.svg", None) == "svg"
