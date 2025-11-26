from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from app.config.models import GlobalConfig, SiteConfig
from app.crawler.content_fetcher import ProductContent
from app.crawler.models import ProductRecord
from app.crawler.site_crawler import SiteCrawler
from app.runtime import RuntimeContext
from app.state.storage import StateStore


class FakeEngine:
    def __init__(self, responses: dict[str, str]):
        self.responses = responses

    def fetch_html(self, request) -> str:
        return self.responses[request.url]

    def shutdown(self) -> None:
        pass


class DummyContentFetcher:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def fetch(
        self,
        url: str,
        image_selector: str | None = None,
        drop_after_selectors: list[str] | None = None,
        **kwargs,
    ) -> ProductContent:
        return ProductContent(
            text_content=f"content-{url}",
            image_url="https://demo.example/img.jpg",
            image_path=None,
            title="Dummy",
            name_en="Test EN",
            name_ru="Тест",
            price_without_discount="100",
            price_with_discount="90",
        )

    def close(self) -> None:
        pass


class CountingContentFetcher(DummyContentFetcher):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[str] = []

    def fetch(self, url: str, *args, **kwargs) -> ProductContent:
        self.calls.append(url)
        return super().fetch(url, *args, **kwargs)


def _site_config() -> SiteConfig:
    payload: dict[str, Any] = {
        "site": {
            "name": "demo",
            "domain": "demo.example",
            "engine": "http",
            "base_url": "https://demo.example",
        },
        "selectors": {
            "product_link_selector": ".product a",
            "allowed_domains": ["demo.example"],
        },
        "pagination": {
            "mode": "numbered_pages",
            "param_name": "page",
            "max_pages": 5,
        },
        "limits": {"max_products": 10},
        "category_urls": ["https://demo.example/catalog/"],
    }
    return SiteConfig.model_validate(payload)


def _global_config(tmp_path: Path) -> GlobalConfig:
    return GlobalConfig.model_validate(
        {
            "sheet": {
                "spreadsheet_id": "TEST",
                "write_batch_size": 200,
                "sheet_state_tab": "_state",
                "sheet_runs_tab": "_runs",
            },
            "runtime": {"max_concurrency_per_site": 1, "global_stop": {}},
            "network": {
                "user_agents": ["test-agent"],
                "proxy_pool": [],
                "request_timeout_sec": 5,
                "retry": {"max_attempts": 1, "backoff_sec": [1, 2]},
            },
            "dedupe": {"strip_params_blacklist": ["utm_*"]},
            "state": {"driver": "sqlite", "database": str(tmp_path / "state.db")},
        }
    )


def test_site_crawler_numbered_pages(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    site = _site_config()
    config = _global_config(tmp_path)
    store = StateStore(Path(config.state.database))
    context = RuntimeContext(
        run_id="run-1",
        started_at=datetime.now(timezone.utc),
        config=config,
        sites=[site],
        state_store=store,
        dry_run=True,
        resume=True,
        assets_dir=tmp_path / "assets",
        flush_product_interval=1,
    )
    html_page1 = """
    <div class="product"><a href="https://demo.example/p/1">1</a></div>
    <div class="product"><a href="https://demo.example/p/2?utm_source=test">2</a></div>
    """
    html_page2 = """
    <div class="product"><a href="/p/3">3</a></div>
    """
    responses = {
        "https://demo.example/catalog/": html_page1,
        "https://demo.example/catalog/?page=2": html_page2,
        "https://demo.example/catalog/?page=3": "<div></div>",
    }
    fake_engine = FakeEngine(responses)
    monkeypatch.setattr("app.crawler.site_crawler.create_engine", lambda *args, **kwargs: fake_engine)
    monkeypatch.setattr("app.crawler.site_crawler.ProductContentFetcher", lambda *args, **kwargs: DummyContentFetcher())

    flushed: list[list[str]] = []

    def flush_chunk(chunk: list[ProductRecord]) -> None:
        flushed.append([item.product_url for item in chunk])

    crawler = SiteCrawler(context, site, flush_products=1, flush_callback=flush_chunk)
    result = crawler.crawl()

    assert len(result.records) == 3
    assert store.get(site.name, str(site.category_urls[0])).last_page == 2
    assert result.records[0].content_text.startswith("content-")
    assert result.records[0].image_path is None
    assert result.records[0].category == "catalog"
    assert flushed and flushed[0][0] == "https://demo.example/p/1"
    store.close()


def test_site_crawler_respects_global_stop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    site = _site_config()
    config = _global_config(tmp_path)
    config.runtime.global_stop.stop_after_products = 2
    store = StateStore(Path(config.state.database))
    context = RuntimeContext(
        run_id="run-1",
        started_at=datetime.now(timezone.utc),
        config=config,
        sites=[site],
        state_store=store,
        dry_run=True,
        resume=True,
        assets_dir=tmp_path / "assets",
        flush_product_interval=10,
    )
    html = """
    <div class="product"><a href="https://demo.example/p/1">1</a></div>
    <div class="product"><a href="https://demo.example/p/2">2</a></div>
    <div class="product"><a href="https://demo.example/p/3">3</a></div>
    """
    responses = {
        "https://demo.example/catalog/": html,
        "https://demo.example/catalog/?page=2": html,
    }
    fake_engine = FakeEngine(responses)
    monkeypatch.setattr("app.crawler.site_crawler.create_engine", lambda *args, **kwargs: fake_engine)
    monkeypatch.setattr("app.crawler.site_crawler.ProductContentFetcher", lambda *args, **kwargs: DummyContentFetcher())

    crawler = SiteCrawler(context, site, flush_products=10)
    result = crawler.crawl()

    assert len(result.records) == 2
    assert context.products_written == 2
    store.close()


def test_site_crawler_skips_existing_products(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    site = _site_config()
    config = _global_config(tmp_path)
    store = StateStore(Path(config.state.database))
    context = RuntimeContext(
        run_id="run-1",
        started_at=datetime.now(timezone.utc),
        config=config,
        sites=[site],
        state_store=store,
        dry_run=True,
        resume=True,
        assets_dir=tmp_path / "assets",
        flush_product_interval=1,
    )
    html = """
    <div class="product"><a href="https://demo.example/p/1">1</a></div>
    <div class="product"><a href="https://demo.example/p/2">2</a></div>
    """
    responses = {
        "https://demo.example/catalog/": html,
        "https://demo.example/catalog/?page=2": "<div></div>",
    }
    fake_engine = FakeEngine(responses)
    fetcher = CountingContentFetcher()
    monkeypatch.setattr("app.crawler.site_crawler.create_engine", lambda *args, **kwargs: fake_engine)
    monkeypatch.setattr("app.crawler.site_crawler.ProductContentFetcher", lambda *args, **kwargs: fetcher)

    crawler = SiteCrawler(
        context,
        site,
        flush_products=1,
        existing_product_urls={"https://demo.example/p/1"},
    )
    result = crawler.crawl()

    assert len(result.records) == 1
    assert result.records[0].product_url == "https://demo.example/p/2"
    assert fetcher.calls == ["https://demo.example/p/2"]
    store.close()


def test_site_crawler_respects_start_page(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    site = _site_config()
    site.pagination.start_page = 3
    config = _global_config(tmp_path)
    store = StateStore(Path(config.state.database))
    context = RuntimeContext(
        run_id="run-start",
        started_at=datetime.now(timezone.utc),
        config=config,
        sites=[site],
        state_store=store,
        dry_run=True,
        resume=True,
        assets_dir=tmp_path / "assets",
        flush_product_interval=1,
    )
    html_page3 = """
    <div class='product'><a href='https://demo.example/p/31'>31</a></div>
    <div class='product'><a href='https://demo.example/p/32'>32</a></div>
    """
    html_page4 = """
    <div class='product'><a href='/p/41'>41</a></div>
    """
    responses = {
        "https://demo.example/catalog/?page=3": html_page3,
        "https://demo.example/catalog/?page=4": html_page4,
        "https://demo.example/catalog/?page=5": "<div></div>",
    }
    fake_engine = FakeEngine(responses)
    fetcher = CountingContentFetcher()
    monkeypatch.setattr("app.crawler.site_crawler.create_engine", lambda *args, **kwargs: fake_engine)
    monkeypatch.setattr("app.crawler.site_crawler.ProductContentFetcher", lambda *args, **kwargs: fetcher)

    crawler = SiteCrawler(context, site, flush_products=1)
    result = crawler.crawl()

    urls = [record.product_url for record in result.records]
    assert urls[0].endswith("/p/31")
    assert store.get(site.name, str(site.category_urls[0])).last_page == 4
    store.close()


def test_site_crawler_respects_end_page(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    site = _site_config()
    site.pagination.end_page = 2
    config = _global_config(tmp_path)
    store = StateStore(Path(config.state.database))
    context = RuntimeContext(
        run_id="run-end",
        started_at=datetime.now(timezone.utc),
        config=config,
        sites=[site],
        state_store=store,
        dry_run=True,
        resume=True,
        assets_dir=tmp_path / "assets",
        flush_product_interval=1,
    )
    page1_html = """
    <div class='product'><a href='https://demo.example/p/A1'>A1</a></div>
    """
    page2_html = """
    <div class='product'><a href='https://demo.example/p/A2'>A2</a></div>
    """
    responses = {
        "https://demo.example/catalog/": page1_html,
        "https://demo.example/catalog/?page=2": page2_html,
        "https://demo.example/catalog/?page=3": page2_html,
    }
    fake_engine = FakeEngine(responses)
    monkeypatch.setattr("app.crawler.site_crawler.create_engine", lambda *args, **kwargs: fake_engine)
    monkeypatch.setattr("app.crawler.site_crawler.ProductContentFetcher", lambda *args, **kwargs: DummyContentFetcher())

    crawler = SiteCrawler(context, site, flush_products=1)
    result = crawler.crawl()

    assert len(result.records) == 2
    assert store.get(site.name, str(site.category_urls[0])).last_page == 2
    store.close()
