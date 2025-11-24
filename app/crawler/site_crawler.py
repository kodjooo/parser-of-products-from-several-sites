from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from app.config.models import DelayConfig, SiteConfig
from app.crawler.behavior import BehaviorContext
from app.crawler.content_fetcher import ProductContentFetcher
from app.crawler.engines import BrowserEngine, EngineRequest, create_engine
from app.crawler.models import CategoryMetrics, ProductRecord, SiteCrawlResult
from app.crawler.utils import jitter_sleep, normalize_url
from app.logger import get_logger
from app.runtime import RuntimeContext
from app.state.storage import CategoryState

logger = get_logger(__name__)


@dataclass(slots=True)
class CategoryResult:
    records: list[ProductRecord]
    metrics: CategoryMetrics


class SiteCrawler:
    """Обходит все категории сайта и готовит результаты для записи."""

    def __init__(
        self,
        context: RuntimeContext,
        site: SiteConfig,
        flush_products: int = 0,
        flush_callback: Optional[Callable[[list[ProductRecord]], None]] = None,
    ):
        self.context = context
        self.site = site
        self._behavior_config = self._prepare_behavior_config(context.config.runtime.behavior)
        self.engine = create_engine(site.engine, context.config.network, self._behavior_config)
        assets_dir = context.assets_dir if context.assets_dir else Path("/app/assets/images")
        shared_browser_engine = (
            self.engine
            if context.config.runtime.product_fetch_engine == "browser"
            and isinstance(self.engine, BrowserEngine)
            else None
        )
        self.content_fetcher = ProductContentFetcher(
            context.config.network,
            assets_dir,
            fetch_engine=context.config.runtime.product_fetch_engine,
            behavior_config=self._behavior_config,
            shared_browser_engine=shared_browser_engine,
        )
        self.dedupe_strip = context.config.dedupe.strip_params_blacklist
        self._seen_urls: set[str] = set()
        self.flush_products = max(1, flush_products) if flush_products else 0
        self.flush_callback = flush_callback
        self._pending_chunk: list[ProductRecord] = []
        self._page_delay: DelayConfig = context.config.runtime.page_delay
        self._product_delay: DelayConfig = context.config.runtime.product_delay

    def crawl(self) -> SiteCrawlResult:
        logger.info("Старт обхода сайта", extra={"site": self.site.name})
        records: list[ProductRecord] = []
        metrics: list[CategoryMetrics] = []
        try:
            for category_url in self.site.category_urls:
                category_result = self._crawl_category(category_url)
                records.extend(category_result.records)
                metrics.append(category_result.metrics)
                if self._global_stop_reached():
                    break
        finally:
            self.engine.shutdown()
            self.content_fetcher.close()
            self._emit_pending(force=True)
        return SiteCrawlResult(
            site_name=self.site.name,
            sheet_tab=self.site.domain,
            records=records,
            metrics=metrics,
        )

    def _crawl_category(self, category_url: str) -> CategoryResult:
        category_url = str(category_url)
        pagination_mode = self.site.pagination.mode
        if pagination_mode == "numbered_pages":
            return self._crawl_numbered_pages(category_url)
        if pagination_mode == "next_button":
            return self._crawl_next_button(category_url)
        return self._crawl_infinite_scroll(category_url)

    def _crawl_numbered_pages(self, category_url: str) -> CategoryResult:
        state = (
            self.context.state_store.get(self.site.name, category_url)
            if self.context.resume
            else None
        )
        start_page = (state.last_page or 0) + 1 if state else 1
        max_pages = self.site.limits.max_pages or self.site.pagination.max_pages or 100
        metrics = CategoryMetrics(site_name=self.site.name, category_url=category_url)
        records: list[ProductRecord] = []
        page = start_page
        while page <= max_pages and not self._global_stop_reached():
            url = self._build_page_url(category_url, page)
            html = self._fetch_page_html(url)
            page_records, has_data, limit_hit = self._process_html(
                html, category_url, page, metrics
            )
            records.extend(page_records)
            self._queue_for_flush(page_records)
            if not has_data:
                break
            metrics.last_page = page
            self._persist_state(category_url, last_page=page, total=len(records))
            if limit_hit or self._should_stop(metrics):
                break
            page += 1
        return CategoryResult(records=records, metrics=metrics)

    def _crawl_next_button(self, category_url: str) -> CategoryResult:
        next_url = category_url
        metrics = CategoryMetrics(site_name=self.site.name, category_url=category_url)
        records: list[ProductRecord] = []
        max_pages = self.site.limits.max_pages or self.site.pagination.max_pages or 100
        page = 1
        while next_url and page <= max_pages and not self._global_stop_reached():
            html = self._fetch_page_html(next_url)
            page_records, has_data, limit_hit = self._process_html(
                html, category_url, page, metrics
            )
            records.extend(page_records)
            self._queue_for_flush(page_records)
            metrics.last_page = page
            self._persist_state(category_url, last_page=page, total=len(records))
            if not has_data or limit_hit or self._should_stop(metrics):
                break
            soup = BeautifulSoup(html, "lxml")
            next_url = self._extract_next_link(soup, current_url=next_url)
            page += 1
        return CategoryResult(records=records, metrics=metrics)

    def _crawl_infinite_scroll(self, category_url: str) -> CategoryResult:
        html = self._fetch_page_html(category_url, scroll_limit=self.site.limits.max_scrolls)
        metrics = CategoryMetrics(site_name=self.site.name, category_url=category_url)
        records, has_data, _ = self._process_html(html, category_url, 1, metrics)
        self._queue_for_flush(records)
        self._emit_pending(force=True)
        if has_data:
            metrics.last_page = 1
            self._persist_state(category_url, last_page=1, total=len(records))
        return CategoryResult(records=records, metrics=metrics)

    def _fetch_page_html(self, url: str, scroll_limit: int | None = None) -> str:
        request = EngineRequest(
            url=url,
            wait_conditions=self.site.wait_conditions,
            pagination=self.site.pagination,
            scroll_limit=scroll_limit,
            behavior_context=self._build_behavior_context(category_url=url),
        )
        html = self.engine.fetch_html(request)
        retries = 0
        while not self._wait_conditions_met(html) and retries < 2:
            html = self.engine.fetch_html(request)
            retries += 1
        self._sleep_between_pages()
        return html

    def _wait_conditions_met(self, html: str) -> bool:
        soup = BeautifulSoup(html, "lxml")
        for condition in self.site.wait_conditions:
            if condition.type == "selector" and not soup.select(condition.value):
                return False
        return True

    def _prepare_behavior_config(self, base_behavior):
        behavior = base_behavior.model_copy()
        hover_targets = self.site.selectors.hover_targets
        if hover_targets:
            mouse_cfg = behavior.mouse.model_copy(update={"hover_selectors": hover_targets})
            behavior = behavior.model_copy(update={"mouse": mouse_cfg})
        return behavior

    def _build_behavior_context(self, category_url: str) -> BehaviorContext | None:
        behavior = self.context.config.runtime.behavior
        if not behavior.enabled:
            return None
        root_url = self.site.base_url
        if not root_url:
            parsed = urlparse(category_url)
            root_url = f"{parsed.scheme}://{parsed.netloc}"
        return BehaviorContext(
            product_link_selector=self.site.selectors.product_link_selector,
            category_url=category_url,
            base_url=self.site.base_url or category_url,
            root_url=root_url,
        )

    def _process_html(
        self,
        html: str,
        category_url: str,
        page_num: int,
        metrics: CategoryMetrics,
    ) -> tuple[list[ProductRecord], bool, bool]:
        soup = BeautifulSoup(html, "lxml")
        if self._should_stop_on_missing_selector(soup):
            return [], False, False
        links = self._extract_product_links(soup)
        metrics.total_found += len(links)
        records: list[ProductRecord] = []
        if not links:
            return records, False, False
        limit_hit = False
        for link in links:
            normalized, product_hash = normalize_url(
                link,
                self.site.base_url,
                self.dedupe_strip,
            )
            if normalized in self._seen_urls:
                metrics.total_duplicates += 1
                continue
            if self.site.selectors.allowed_domains:
                domain = urlparse(normalized).netloc
                if domain not in self.site.selectors.allowed_domains:
                    continue
            record = ProductRecord(
                source_site=self.site.domain,
                category_url=category_url,
                product_url=normalized,
                page_num=page_num,
                run_id=self.context.run_id,
                product_id_hash=product_hash,
                category=self._map_category_slug(category_url),
            )
            content = self.content_fetcher.fetch(
                normalized,
                image_selector=self.site.selectors.main_image_selector,
                drop_after_selectors=self.site.selectors.content_drop_after,
                download_image=False,
                name_en_selector=self.site.selectors.name_en_selector,
                name_ru_selector=self.site.selectors.name_ru_selector,
                price_without_discount_selector=self.site.selectors.price_without_discount_selector,
                price_with_discount_selector=self.site.selectors.price_with_discount_selector,
            )
            record.content_text = content.text_content
            record.image_url = content.image_url
            record.image_path = content.image_path
            record.image_name_hint = content.title
            record.name_en = content.name_en
            record.name_ru = content.name_ru
            record.price_without_discount = content.price_without_discount
            record.price_with_discount = content.price_with_discount
            self._seen_urls.add(normalized)
            records.append(record)
            metrics.total_written += 1
            if self.context.register_product():
                limit_hit = True
                break
            if self._reached_product_limit(metrics):
                limit_hit = True
                break
            if self._global_stop_reached():
                limit_hit = True
                break
            self._sleep_between_products()
        return records, bool(records), limit_hit

    def _should_stop_on_missing_selector(self, soup: BeautifulSoup) -> bool:
        for condition in self.site.stop_conditions:
            if condition.type == "missing_selector" and condition.value:
                if not soup.select(condition.value):
                    return True
        return False

    def _sleep_between_pages(self) -> None:
        if self._page_delay.max_sec <= 0:
            return
        jitter_sleep(self._page_delay.min_sec, self._page_delay.max_sec)

    def _sleep_between_products(self) -> None:
        if self._product_delay.max_sec <= 0:
            return
        jitter_sleep(self._product_delay.min_sec, self._product_delay.max_sec)

    def _extract_product_links(self, soup: BeautifulSoup) -> list[str]:
        nodes = soup.select(self.site.selectors.product_link_selector)
        return [node.get("href") for node in nodes if node.get("href")]

    def _extract_next_link(self, soup: BeautifulSoup, current_url: str) -> str | None:
        selector = self.site.pagination.next_button_selector
        if not selector:
            return None
        node = soup.select_one(selector)
        if node and node.get("href"):
            base = self.site.base_url or current_url
            return urljoin(base, node["href"])
        return None

    def _build_page_url(self, category_url: str, page_num: int) -> str:
        param = self.site.pagination.param_name or "page"
        if page_num <= 1:
            return category_url
        parsed = urlparse(category_url)
        query = dict(parse_qsl(parsed.query)) if parsed.query else {}
        query[param] = str(page_num)
        new_query = urlencode(query)
        return urlunparse(
            (parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, "")
        )

    def _extract_category_slug(self, category_url: str) -> str | None:
        parsed = urlparse(category_url)
        path = parsed.path or ""
        marker = "/items/"
        if marker in path:
            slug = path.split(marker, 1)[1]
            return slug.strip("/")
        return path.strip("/") or None

    def _map_category_slug(self, category_url: str) -> str | None:
        slug = self._extract_category_slug(category_url)
        if not slug:
            return None
        labels = self.site.selectors.category_labels
        return labels.get(slug, slug)

    def _persist_state(self, category_url: str, *, last_page: int, total: int) -> None:
        self.context.state_store.upsert(
            CategoryState(
                site_name=self.site.name,
                category_url=category_url,
                last_page=last_page,
                last_product_count=total,
                last_run_ts=datetime.now(timezone.utc),
            )
        )

    def _reached_product_limit(self, metrics: CategoryMetrics) -> bool:
        max_products = self.site.limits.max_products
        if max_products and metrics.total_written >= max_products:
            return True
        return False

    def _should_stop(self, metrics: CategoryMetrics) -> bool:
        for condition in self.site.stop_conditions:
            if condition.type == "no_new_products" and metrics.total_written == 0:
                return True
        return False

    def _queue_for_flush(self, new_records: list[ProductRecord]) -> None:
        if not self.flush_callback or not new_records:
            return
        self._pending_chunk.extend(new_records)
        if self.flush_products > 0 and len(self._pending_chunk) >= self.flush_products:
            self._emit_pending()

    def _emit_pending(self, force: bool = False) -> None:
        if not self.flush_callback:
            return
        if not self._pending_chunk:
            return
        if force or (self.flush_products > 0 and len(self._pending_chunk) >= self.flush_products):
            chunk = self._pending_chunk
            self._pending_chunk = []
            self.flush_callback(chunk)
            # после записи начинаем отсчёт заново

    def _global_stop_reached(self) -> bool:
        return self.context.product_limit_reached()
