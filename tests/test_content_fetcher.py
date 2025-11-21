from __future__ import annotations

from bs4 import BeautifulSoup

from app.crawler.content_fetcher import (
    _extract_main_image_url,
    _extract_text_content,
    _extract_text_by_selector,
)


def test_extract_text_content_drops_after_selector():
    html = """
    <div class='desc'>Описание товара <b>с выделением</b></div>
    <div class='container ratings-block'>Рейтинг и отзывы</div>
    <div>Хвост, который нужно убрать</div>
    """
    soup = BeautifulSoup(html, "lxml")
    text = _extract_text_content(soup, ["div.container.ratings-block"])
    assert text == "Описание товара с выделением"


def test_extract_text_by_selector_accepts_single_string():
    soup = BeautifulSoup("<div class='price'><span>990</span></div>", "lxml")
    value = _extract_text_by_selector(soup, "div.price span")
    assert value == "990"


def test_extract_text_by_selector_tries_fallbacks():
    html = """
    <div class='price'>
        <span class='primary'>950</span>
    </div>
    """
    soup = BeautifulSoup(html, "lxml")
    value = _extract_text_by_selector(
        soup, ["", ".missing-price", ".price .primary"]
    )
    assert value == "950"


def test_extract_main_image_prefers_highest_descriptor():
    html = """
    <picture>
        <img src="https://example.com/a-1x.webp" srcset="https://example.com/a-1x.webp 1x, https://example.com/a-2x.webp 2x">
    </picture>
    """
    soup = BeautifulSoup(html, "lxml")
    url = _extract_main_image_url(soup, "https://example.com/product")
    assert url == "https://example.com/a-2x.webp"
