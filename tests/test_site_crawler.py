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

    def fetch(self, url: str, image_selector: str | None = None) -> ProductContent:
        return ProductContent(
            text_content=f"content-{url}",
            image_url="https://demo.example/img.jpg",
            image_path="/tmp/img.jpg",
        )

    def close(self) -> None:
        pass


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
        flush_page_interval=1,
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

    crawler = SiteCrawler(context, site, flush_pages=1, flush_callback=flush_chunk)
    result = crawler.crawl()

    assert len(result.records) == 3
    assert store.get(site.name, str(site.category_urls[0])).last_page == 2
    assert result.records[0].content_text.startswith("content-")
    assert result.records[0].image_path == "/tmp/img.jpg"
    assert flushed and flushed[0][0] == "https://demo.example/p/1"
    store.close()
