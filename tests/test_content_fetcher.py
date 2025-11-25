from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup

from app.config.models import NetworkConfig
from app.crawler.content_fetcher import (
    ProductContentFetcher,
    _extract_image_from_node,
    _extract_main_image_url,
    _extract_text_content,
    _extract_text_by_selector,
)
from app.crawler.engines import ProxyPool


class _FakeResponse:
    def __init__(self, text: str = "<html></html>") -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


class _RecordingHttpClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def get(self, url: str, *, headers: dict[str, str]):
        self.calls.append({"url": url, "headers": headers})
        return _FakeResponse()


class _FakeClientFactory:
    def __init__(self, client: _RecordingHttpClient) -> None:
        self.client = client
        self.requested_proxies: list[str | None] = []

    def get(self, proxy: str | None) -> _RecordingHttpClient:
        self.requested_proxies.append(proxy)
        return self.client

    def close(self) -> None:
        return None


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


def test_extract_image_from_node_understands_picture_sources():
    html = """
    <picture class="image">
        <source srcset="https://example.com/a-1x.webp 1x, https://example.com/a-2x.webp 2x">
        <img data-nuxt-img="https://example.com/a-1x.webp"/>
    </picture>
    """
    soup = BeautifulSoup(html, "lxml")
    node = soup.select_one("picture.image")
    url = _extract_image_from_node(node, "https://example.com/product")
    assert url == "https://example.com/a-2x.webp"


def test_fetch_html_http_passes_proxy_into_httpx(tmp_path):
    network = NetworkConfig(
        user_agents=["UA"],
        proxy_pool=["http://proxy.local:8080"],
    )
    fetcher = ProductContentFetcher(network, Path(tmp_path))
    fake_client = _RecordingHttpClient()
    factory = _FakeClientFactory(fake_client)
    fetcher._http_client_factory = factory  # type: ignore[assignment]
    fetcher._proxy_pool = ProxyPool(["http://proxy.local:8080"])
    html, proxy = fetcher._fetch_html_http("https://example.com/product")
    assert html == "<html></html>"
    assert proxy == "http://proxy.local:8080"
    assert factory.requested_proxies[-1] == "http://proxy.local:8080"
