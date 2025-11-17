from __future__ import annotations

from bs4 import BeautifulSoup

from app.crawler.content_fetcher import _extract_text_content


def test_extract_text_content_drops_after_selector():
    html = """
    <div class='desc'>Описание товара <b>с выделением</b></div>
    <div class='container ratings-block'>Рейтинг и отзывы</div>
    <div>Хвост, который нужно убрать</div>
    """
    soup = BeautifulSoup(html, "lxml")
    text = _extract_text_content(soup, ["div.container.ratings-block"])
    assert text == "Описание товара с выделением"
