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
    _clean_price_text,
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


class _FakeBrowserEngine:
    def __init__(self, html: str, *, binary: bytes | None = None, fail_binary: bool = False):
        self.html = html
        self.binary = binary or b""
        self.fail_binary = fail_binary
        self.last_proxy = "http://proxy.local:8080"

    def fetch_html(self, request):
        return self.html

    def fetch_binary(self, url: str, proxy: str | None = None):
        if self.fail_binary:
            raise RuntimeError("binary failed")
        return self.binary, "image/webp"


class _RecordingImageSaver:
    def __init__(self) -> None:
        self.saved_from_content: list[tuple[str, str | None]] = []
        self.saved_http: list[tuple[str, str | None]] = []

    def save_from_content(self, url: str, title: str | None, fallback_id: str, content: bytes, content_type: str | None):
        self.saved_from_content.append((url, title))
        return "/tmp/content.webp"

    def save(self, url: str, title: str | None, fallback_id: str, proxy: str | None = None):
        self.saved_http.append((url, title))
        return "/tmp/http.webp"

    def close(self) -> None:  # pragma: no cover - совместимость интерфейса
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


def test_clean_price_text_extracts_amount_and_currency():
    assert _clean_price_text("Цена: 1 290 ₽ / шт.") == "1 290 ₽"
    assert _clean_price_text("Всего 990 руб.") == "990 руб."
    assert _clean_price_text("~ 5 500,50  рубля за набор") == "5 500,50 руб."


def test_fetch_uses_browser_engine_for_content(tmp_path):
    network = NetworkConfig(user_agents=["UA"], proxy_pool=[])
    fetcher = ProductContentFetcher(network, Path(tmp_path))
    html = """
    <html>
        <div class=\"price\">Цена: 1 250 руб.</div>
        <img data-nuxt-img=\"https://example.com/image.webp\" />
    </html>
    """
    fetcher._browser = _FakeBrowserEngine(html, binary=b"bytes")  # type: ignore[attr-defined]
    fetcher._owns_browser = False  # type: ignore[attr-defined]
    recorder = _RecordingImageSaver()
    fetcher.image_saver = recorder  # type: ignore[assignment]

    result = fetcher.fetch(
        "https://example.com/product",
        image_selector="img",
        price_with_discount_selector=".price",
        behavior_context=None,
    )

    assert result.price_with_discount == "1 250 руб."
    assert recorder.saved_from_content
    assert not recorder.saved_http


def test_fetch_falls_back_to_http_when_browser_binary_fails(tmp_path):
    network = NetworkConfig(user_agents=["UA"], proxy_pool=[])
    fetcher = ProductContentFetcher(network, Path(tmp_path))
    html = """
    <html>
        <div class=\"price\">~ 5 500,50 рубля</div>
        <img data-nuxt-img=\"https://example.com/image.webp\" />
    </html>
    """
    fetcher._browser = _FakeBrowserEngine(html, fail_binary=True)  # type: ignore[attr-defined]
    fetcher._owns_browser = False  # type: ignore[attr-defined]
    recorder = _RecordingImageSaver()
    fetcher.image_saver = recorder  # type: ignore[assignment]

    result = fetcher.fetch(
        "https://example.com/product",
        image_selector="img",
        price_with_discount_selector=".price",
        behavior_context=None,
    )

    assert result.price_with_discount == "5 500,50 руб."
    assert recorder.saved_http
