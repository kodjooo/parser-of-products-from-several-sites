from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from app.config.models import GlobalConfig, SiteConfig
from app.crawler.content_fetcher import ProductContent
from app.crawler.models import CategoryMetrics, ProductRecord
from app.crawler.site_crawler import SiteCrawler
from app.runtime import RuntimeContext
from app.state.storage import CategoryState, StateStore


class FakeEngine:
    def __init__(self, responses: dict[str, str]):
        self.responses = responses
        self.calls: list[str] = []

    def fetch_html(self, request) -> str:
        self.calls.append(request.url)
        return self.responses.get(request.url, "<div></div>")

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
        exclude_selectors: list[str] | None = None,
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
    # в state сохраняется следующая страница для старта
    assert store.get(site.name, str(site.category_urls[0])).last_page == 3
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


def test_site_crawler_retries_empty_category(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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

    pages = [
        "<div class='empty'></div>",
        '<div class="product"><a href="https://demo.example/p/1">1</a></div>',
    ]

    def fake_fetch(self, url: str, scroll_limit=None) -> str:  # type: ignore[override]
        if pages:
            return pages.pop(0)
        return "<div></div>"

    wait_delays: list[int] = []

    def fake_wait(self, delay: int) -> None:
        wait_delays.append(delay)

    class StubEngine:
        def __init__(self) -> None:
            self.mark_calls = 0

        def fetch_html(self, request) -> str:  # pragma: no cover - не должен вызываться
            raise AssertionError("fetch_html should be patched")

        def shutdown(self) -> None:
            pass

        def mark_last_proxy_bad(self, reason=None) -> None:
            self.mark_calls += 1

    stub_engine = StubEngine()
    monkeypatch.setattr("app.crawler.site_crawler.create_engine", lambda *args, **kwargs: stub_engine)
    monkeypatch.setattr("app.crawler.site_crawler.ProductContentFetcher", lambda *args, **kwargs: DummyContentFetcher())
    monkeypatch.setattr("app.crawler.site_crawler.SiteCrawler._fetch_page_html", fake_fetch)
    monkeypatch.setattr("app.crawler.site_crawler.SiteCrawler._wait_before_retry", fake_wait)

    crawler = SiteCrawler(context, site, flush_products=1)
    result = crawler.crawl()

    assert len(result.records) == 1
    assert wait_delays == [60]
    assert stub_engine.mark_calls == 1
    store.close()


def test_site_crawler_retries_empty_category_twice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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

    pages = [
        "<div class='empty'></div>",
        "<section></section>",
        '<div class="product"><a href="https://demo.example/p/1">1</a></div>',
    ]

    def fake_fetch(self, url: str, scroll_limit=None) -> str:  # type: ignore[override]
        if pages:
            return pages.pop(0)
        return "<div></div>"

    wait_delays: list[int] = []

    def fake_wait(self, delay: int) -> None:
        wait_delays.append(delay)

    class StubEngine:
        def __init__(self) -> None:
            self.mark_calls = 0

        def fetch_html(self, request) -> str:  # pragma: no cover
            raise AssertionError("fetch_html should be patched")

        def shutdown(self) -> None:
            pass

        def mark_last_proxy_bad(self, reason=None) -> None:
            self.mark_calls += 1

    stub_engine = StubEngine()
    monkeypatch.setattr("app.crawler.site_crawler.create_engine", lambda *args, **kwargs: stub_engine)
    monkeypatch.setattr("app.crawler.site_crawler.ProductContentFetcher", lambda *args, **kwargs: DummyContentFetcher())
    monkeypatch.setattr("app.crawler.site_crawler.SiteCrawler._fetch_page_html", fake_fetch)
    monkeypatch.setattr("app.crawler.site_crawler.SiteCrawler._wait_before_retry", fake_wait)

    crawler = SiteCrawler(context, site, flush_products=1)
    result = crawler.crawl()

    assert len(result.records) == 1
    assert wait_delays[:2] == [60, 600]
    assert stub_engine.mark_calls == 2
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
    assert store.get(site.name, str(site.category_urls[0])).last_page == 5
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
    assert store.get(site.name, str(site.category_urls[0])).last_page == 3
    store.close()


def test_site_crawler_continues_after_empty_pages(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    site = _site_config()
    config = _global_config(tmp_path)
    store = StateStore(Path(config.state.database))
    context = RuntimeContext(
        run_id="run-empty-continue",
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
    <div class='product'><a href='https://demo.example/p/1'>1</a></div>
    <div class='product'><a href='https://demo.example/p/2'>2</a></div>
    """
    page4_html = """
    <div class='product'><a href='https://demo.example/p/4'>4</a></div>
    """
    responses = {
        "https://demo.example/catalog/": page1_html,
        "https://demo.example/catalog/?page=2": "<div></div>",
        "https://demo.example/catalog/?page=3": "<div></div>",
        "https://demo.example/catalog/?page=4": page4_html,
        "https://demo.example/catalog/?page=5": "<div></div>",
    }
    fake_engine = FakeEngine(responses)
    monkeypatch.setattr("app.crawler.site_crawler.create_engine", lambda *args, **kwargs: fake_engine)
    monkeypatch.setattr("app.crawler.site_crawler.ProductContentFetcher", lambda *args, **kwargs: DummyContentFetcher())

    crawler = SiteCrawler(context, site, flush_products=1)
    result = crawler.crawl()

    assert len(result.records) == 3
    assert any("page=4" in call for call in fake_engine.calls)
    store.close()


def test_site_crawler_stops_after_three_empty_pages(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    site = _site_config()
    site.pagination.max_pages = 10
    config = _global_config(tmp_path)
    store = StateStore(Path(config.state.database))
    context = RuntimeContext(
        run_id="run-empty-stop",
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
    <div class='product'><a href='https://demo.example/p/1'>1</a></div>
    """
    responses = {
        "https://demo.example/catalog/": page1_html,
        "https://demo.example/catalog/?page=2": "<div></div>",
        "https://demo.example/catalog/?page=3": "<div></div>",
        "https://demo.example/catalog/?page=4": "<div></div>",
        "https://demo.example/catalog/?page=5": """
        <div class='product'><a href='https://demo.example/p/5'>5</a></div>
        """,
    }
    fake_engine = FakeEngine(responses)
    monkeypatch.setattr("app.crawler.site_crawler.create_engine", lambda *args, **kwargs: fake_engine)
    monkeypatch.setattr("app.crawler.site_crawler.ProductContentFetcher", lambda *args, **kwargs: DummyContentFetcher())

    crawler = SiteCrawler(context, site, flush_products=1)
    result = crawler.crawl()

    assert len(result.records) == 1
    assert not any("page=5" in call for call in fake_engine.calls)
    store.close()


def test_category_cooldown_flushes_buffer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    site = _site_config()
    config = _global_config(tmp_path)
    config.runtime.fail_cooldown_threshold = 2
    config.runtime.fail_cooldown_seconds = 7
    store = StateStore(Path(config.state.database))
    context = RuntimeContext(
        run_id="run-cooldown",
        started_at=datetime.now(timezone.utc),
        config=config,
        sites=[site],
        state_store=store,
        dry_run=True,
        resume=True,
        assets_dir=tmp_path / "assets",
        flush_product_interval=1,
    )
    flushed: list[list[str]] = []

    def fake_flush(chunk: list[str]) -> None:
        flushed.append(list(chunk))

    sleep_calls: list[int] = []
    crawler = SiteCrawler(context, site, flush_products=1, flush_callback=fake_flush)
    monkeypatch.setattr("app.crawler.site_crawler.time.sleep", lambda seconds: sleep_calls.append(seconds))
    crawler._pending_chunk = ["p1"]  # type: ignore[assignment]

    crawler._register_category_fetch_failure()
    assert not sleep_calls
    assert crawler._category_fail_streak == 1

    crawler._register_category_fetch_failure()
    assert sleep_calls == [7]
    assert crawler._category_fail_streak == 0
    assert flushed == [["p1"]]
    store.close()


def test_site_crawler_resumes_from_last_product(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    site = _site_config()
    config = _global_config(tmp_path)
    store = StateStore(Path(config.state.database))
    category = str(site.category_urls[0])
    # предыдущий запуск обработал страницу 2 до первой ссылки
    store.upsert(
        CategoryState(
            site_name=site.name,
            category_url=category,
            last_page=2,
            last_offset=1,
            last_product_count=1,
        )
    )
    context = RuntimeContext(
        run_id="run-resume",
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
    <div class='product'><a href='https://demo.example/p/1'>1</a></div>
    """
    html_page2 = """
    <div class='product'><a href='https://demo.example/p/21'>21</a></div>
    <div class='product'><a href='https://demo.example/p/22'>22</a></div>
    <div class='product'><a href='https://demo.example/p/23'>23</a></div>
    """
    responses = {
        "https://demo.example/catalog/": html_page1,
        "https://demo.example/catalog/?page=2": html_page2,
        "https://demo.example/catalog/?page=3": "<div></div>",
    }
    fake_engine = FakeEngine(responses)
    fetcher = CountingContentFetcher()
    monkeypatch.setattr("app.crawler.site_crawler.create_engine", lambda *args, **kwargs: fake_engine)
    monkeypatch.setattr("app.crawler.site_crawler.ProductContentFetcher", lambda *args, **kwargs: fetcher)

    crawler = SiteCrawler(context, site, flush_products=1)
    result = crawler.crawl()

    urls = [record.product_url for record in result.records]
    assert urls[0].endswith("/p/22")
    # после завершения страницы 2 нужно перейти к 3
    saved_state = store.get(site.name, category)
    assert saved_state.last_page == 3
    assert saved_state.last_offset == 0
    store.close()


def test_site_crawler_logs_failed_category(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    site = _site_config()
    site.pagination.max_pages = 1
    config = _global_config(tmp_path)
    store = StateStore(Path(config.state.database))
    context = RuntimeContext(
        run_id="run-failed-category",
        started_at=datetime.now(timezone.utc),
        config=config,
        sites=[site],
        state_store=store,
        dry_run=True,
        resume=True,
        assets_dir=tmp_path / "assets",
        flush_product_interval=1,
    )

    class ErrorEngine:
        def fetch_html(self, request) -> str:
            raise RuntimeError("boom")

        def shutdown(self) -> None:  # pragma: no cover - ничего не делает
            pass

        def mark_last_proxy_bad(self, reason=None) -> None:  # pragma: no cover
            pass

    monkeypatch.setattr("app.crawler.site_crawler.create_engine", lambda *args, **kwargs: ErrorEngine())
    monkeypatch.setattr(
        "app.crawler.site_crawler.ProductContentFetcher",
        lambda *args, **kwargs: DummyContentFetcher(),
    )

    crawler = SiteCrawler(context, site, flush_products=1)
    result = crawler.crawl()

    assert not result.records
    log_path = Path(config.state.database).with_name("skipped_categories.log")
    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "reason=exception:RuntimeError" in content
    assert str(site.category_urls[0]) in content
    store.close()


def test_site_crawler_logs_empty_category_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    site = _site_config()
    config = _global_config(tmp_path)
    store = StateStore(Path(config.state.database))
    context = RuntimeContext(
        run_id="run-empty-category",
        started_at=datetime.now(timezone.utc),
        config=config,
        sites=[site],
        state_store=store,
        dry_run=True,
        resume=True,
        assets_dir=tmp_path / "assets",
        flush_product_interval=1,
    )
    crawler = SiteCrawler(context, site, flush_products=1)

    monkeypatch.setattr(
        "app.crawler.site_crawler.SiteCrawler._EMPTY_CATEGORY_RETRY_DELAYS",
        tuple(),
    )

    metrics = CategoryMetrics(site_name=site.name, category_url=str(site.category_urls[0]))
    crawler._retry_empty_category_page(  # type: ignore[arg-type]
        page_url=str(site.category_urls[0]),
        category_url=str(site.category_urls[0]),
        page_num=1,
        metrics=metrics,
        start_offset=0,
        save_progress=False,
        scroll_limit=None,
    )

    log_path = Path(config.state.database).with_name("skipped_categories.log")
    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "reason=empty_after_retries" in content
    store.close()
