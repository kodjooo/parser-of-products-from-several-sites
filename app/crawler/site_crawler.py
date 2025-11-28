from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from app.config.models import DelayConfig, SiteConfig
from app.crawler.behavior import BehaviorContext
from app.crawler.content_fetcher import ProductContent, ProductContentFetcher
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

    _EMPTY_CATEGORY_RETRY_DELAYS = (60, 600, 1200, 3600)
    _MAX_EMPTY_PAGES_STREAK = 3

    def __init__(
        self,
        context: RuntimeContext,
        site: SiteConfig,
        flush_products: int = 0,
        flush_callback: Optional[Callable[[list[ProductRecord]], None]] = None,
        existing_product_urls: Optional[set[str]] = None,
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
        self._existing_product_urls: set[str] = set(existing_product_urls or set())
        self.flush_products = max(1, flush_products) if flush_products else 0
        self.flush_callback = flush_callback
        self._pending_chunk: list[ProductRecord] = []
        self._page_delay: DelayConfig = context.config.runtime.page_delay
        self._product_delay: DelayConfig = context.config.runtime.product_delay
        state_path = getattr(self.context.state_store, "path", None)
        if state_path:
            self._skipped_log_path = state_path.with_name("skipped_products.log")
        else:
            self._skipped_log_path = Path("state/skipped_products.log")
        logger.debug(
            "SiteCrawler инициализирован: flush_callback=%s, flush_products=%s",
            bool(self.flush_callback),
            self.flush_products,
            extra={"site": self.site.name},
        )

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
        pagination = self.site.pagination
        configured_start = max(1, pagination.start_page or 1)
        start_page = configured_start
        start_offset = 0
        if self.context.resume and state:
            if state.last_offset is not None:
                resume_page = state.last_page or configured_start
                start_page = max(configured_start, resume_page)
                start_offset = max(0, state.last_offset or 0)
            else:
                resume_page = (state.last_page or (configured_start - 1)) + 1
                start_page = max(configured_start, resume_page)
        max_pages_limit = self.site.limits.max_pages or pagination.max_pages or 100
        if pagination.end_page is not None:
            max_pages = min(max_pages_limit, pagination.end_page)
        else:
            max_pages = max_pages_limit
        metrics = CategoryMetrics(site_name=self.site.name, category_url=category_url)
        records: list[ProductRecord] = []
        page = start_page
        empty_pages_streak = 0
        while page <= max_pages and not self._global_stop_reached():
            url = self._build_page_url(category_url, page)
            html = self._fetch_page_html(url)
            page_records, has_data, limit_hit = self._process_html(
                html,
                category_url,
                page,
                metrics,
                start_offset=start_offset if page == start_page else 0,
                save_progress=True,
            )
            records.extend(page_records)
            start_offset_value = start_offset if page == start_page else 0
            should_retry = (
                not has_data
                and metrics.total_found == 0
                and page == start_page
            )
            if should_retry:
                retry = self._retry_empty_category_page(
                    url,
                    category_url,
                    page,
                    metrics,
                    start_offset=start_offset_value,
                    save_progress=True,
                )
                if retry:
                    retry_records, has_data, limit_hit = retry
                    records.extend(retry_records)
            if has_data:
                empty_pages_streak = 0
                metrics.last_page = page
            else:
                empty_pages_streak += 1
                if empty_pages_streak >= self._MAX_EMPTY_PAGES_STREAK:
                    logger.info(
                        "Достигнут лимит пустых страниц подряд, прерываем обход категории",
                        extra={"site": self.site.name, "category_url": category_url, "page": page},
                    )
                    break
            start_offset = 0
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
        empty_pages_streak = 0
        while next_url and page <= max_pages and not self._global_stop_reached():
            html = self._fetch_page_html(next_url)
            page_records, has_data, limit_hit = self._process_html(
                html, category_url, page, metrics
            )
            records.extend(page_records)
            self._persist_state(
                category_url,
                next_page=page + 1,
                page_offset=0,
                total=len(records),
            )
            should_retry = not has_data and metrics.total_found == 0 and page == 1
            if should_retry:
                retry = self._retry_empty_category_page(
                    next_url,
                    category_url,
                    page,
                    metrics,
                    start_offset=0,
                    save_progress=False,
                )
                if retry:
                    retry_records, has_data, limit_hit = retry
                    records.extend(retry_records)
            if has_data:
                empty_pages_streak = 0
                metrics.last_page = page
            else:
                empty_pages_streak += 1
                if empty_pages_streak >= self._MAX_EMPTY_PAGES_STREAK:
                    logger.info(
                        "Достигнут лимит пустых страниц подряд, прерываем обход категории",
                        extra={"site": self.site.name, "category_url": category_url, "page": page},
                    )
                    break
            if limit_hit or self._should_stop(metrics):
                break
            soup = BeautifulSoup(html, "lxml")
            next_url = self._extract_next_link(soup, current_url=next_url)
            page += 1
        return CategoryResult(records=records, metrics=metrics)

    def _crawl_infinite_scroll(self, category_url: str) -> CategoryResult:
        scroll_limit = self.site.limits.max_scrolls
        html = self._fetch_page_html(category_url, scroll_limit=scroll_limit)
        metrics = CategoryMetrics(site_name=self.site.name, category_url=category_url)
        records, has_data, limit_hit = self._process_html(html, category_url, 1, metrics)
        should_retry = not has_data and metrics.total_found == 0
        if should_retry:
            retry = self._retry_empty_category_page(
                category_url,
                category_url,
                1,
                metrics,
                start_offset=0,
                save_progress=False,
                scroll_limit=scroll_limit,
            )
            if retry:
                retry_records, has_data, limit_hit = retry
                records.extend(retry_records)
        self._emit_pending(force=True)
        if has_data:
            metrics.last_page = 1
            self._persist_state(
                category_url,
                next_page=1,
                page_offset=0,
                total=len(records),
            )
        if limit_hit:
            metrics.last_page = 1
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
            scroll_min_percent=self.site.pagination.scroll_min_percent,
            scroll_max_percent=self.site.pagination.scroll_max_percent,
        )

    def _build_product_behavior_context(self, product_url: str) -> BehaviorContext | None:
        behavior = self.context.config.runtime.behavior
        if not behavior.enabled:
            return None
        product_hover = self.site.selectors.product_hover_targets
        if product_hover is None:
            return None
        root_url = self.site.base_url
        if not root_url:
            parsed = urlparse(product_url)
            root_url = f"{parsed.scheme}://{parsed.netloc}"
        return BehaviorContext(
            hover_selectors=list(product_hover) if product_hover else [],
            category_url=product_url,
            base_url=self.site.base_url or product_url,
            root_url=root_url,
        )

    def _process_html(
        self,
        html: str,
        category_url: str,
        page_num: int,
        metrics: CategoryMetrics,
        *,
        start_offset: int = 0,
        save_progress: bool = False,
    ) -> tuple[list[ProductRecord], bool, bool]:
        soup = BeautifulSoup(html, "lxml")
        if self._should_stop_on_missing_selector(soup):
            return [], False, False
        links = self._extract_product_links(soup)
        logger.debug(
            "Парсер категории нашёл %s ссылок",
            len(links),
            extra={"site": self.site.name, "page": page_num, "count": len(links)},
        )
        metrics.total_found += len(links)
        records: list[ProductRecord] = []
        if not links:
            return records, False, False
        limit_hit = False
        total_links = len(links)
        handled_offset = start_offset
        for idx, link in enumerate(links):
            if idx < start_offset:
                continue
            current_offset = idx + 1
            normalized, product_hash = normalize_url(
                link,
                self.site.base_url,
                self.dedupe_strip,
            )
            if normalized in self._seen_urls:
                metrics.total_duplicates += 1
                if save_progress:
                    self._persist_state(
                        category_url,
                        next_page=page_num,
                        page_offset=current_offset,
                        total=metrics.total_written,
                    )
                continue
            if normalized in self._existing_product_urls:
                metrics.total_duplicates += 1
                if save_progress:
                    self._persist_state(
                        category_url,
                        next_page=page_num,
                        page_offset=current_offset,
                        total=metrics.total_written,
                    )
                continue
            if self.site.selectors.allowed_domains:
                domain = urlparse(normalized).netloc
                if domain not in self.site.selectors.allowed_domains:
                    if save_progress:
                        self._persist_state(
                            category_url,
                            next_page=page_num,
                            page_offset=current_offset,
                            total=metrics.total_written,
                        )
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
            try:
                content = self.content_fetcher.fetch(
                    normalized,
                    image_selector=self.site.selectors.main_image_selector,
                    drop_after_selectors=self.site.selectors.content_drop_after,
                    download_image=True,
                    name_en_selector=self.site.selectors.name_en_selector,
                    name_ru_selector=self.site.selectors.name_ru_selector,
                    price_without_discount_selector=self.site.selectors.price_without_discount_selector,
                    price_with_discount_selector=self.site.selectors.price_with_discount_selector,
                    behavior_context=self._build_product_behavior_context(normalized),
                )
            except Exception as exc:
                logger.error(
                    "Ошибка при обработке товара, запись пропущена",
                    extra={"url": normalized, "error": str(exc)},
                )
                metrics.total_failed += 1
                self._log_skipped_product(normalized, exc)
                if save_progress:
                    self._persist_state(
                        category_url,
                        next_page=page_num,
                        page_offset=current_offset,
                        total=metrics.total_written,
                    )
                continue
            record.content_text = content.text_content
            record.image_url = content.image_url
            record.image_path = content.image_path
            record.image_name_hint = content.title
            record.name_en = content.name_en
            record.name_ru = content.name_ru
            record.price_without_discount = content.price_without_discount
            record.price_with_discount = content.price_with_discount
            if not self._content_loaded(content):
                logger.warning(
                    "Страница товара не загружена, запись пропущена",
                    extra={"url": normalized},
                )
                metrics.total_failed += 1
                self._log_skipped_product(normalized, None)
                if save_progress:
                    self._persist_state(
                        category_url,
                        next_page=page_num,
                        page_offset=current_offset,
                        total=metrics.total_written,
                    )
                continue
            self._seen_urls.add(normalized)
            self._existing_product_urls.add(normalized)
            records.append(record)
            self._queue_for_flush([record])
            metrics.total_written += 1
            if save_progress:
                self._persist_state(
                    category_url,
                    next_page=page_num,
                    page_offset=current_offset,
                    total=metrics.total_written,
                )
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
            handled_offset = current_offset
        if save_progress and not limit_hit and total_links > 0:
            if handled_offset >= total_links:
                self._persist_state(
                    category_url,
                    next_page=page_num + 1,
                    page_offset=0,
                    total=metrics.total_written,
                )
        return records, bool(records), limit_hit

    def _retry_empty_category_page(
        self,
        page_url: str,
        category_url: str,
        page_num: int,
        metrics: CategoryMetrics,
        *,
        start_offset: int,
        save_progress: bool,
        scroll_limit: int | None = None,
    ) -> tuple[list[ProductRecord], bool, bool] | None:
        for delay in self._EMPTY_CATEGORY_RETRY_DELAYS:
            if self._global_stop_reached():
                break
            self._mark_last_proxy_for_retry()
            logger.warning(
                "Категория вернула 0 ссылок, попытка повторной загрузки через другой прокси",
                extra={
                    "site": self.site.name,
                    "category_url": category_url,
                    "page": page_num,
                    "retry_delay_sec": delay,
                },
            )
            self._wait_before_retry(delay)
            html = self._fetch_page_html(page_url, scroll_limit=scroll_limit)
            page_records, has_data, limit_hit = self._process_html(
                html,
                category_url,
                page_num,
                metrics,
                start_offset=start_offset,
                save_progress=save_progress,
            )
            if has_data:
                return page_records, has_data, limit_hit
        if self._global_stop_reached():
            return None
        logger.error(
            "Категория осталась пустой после всех повторов",
            extra={
                "site": self.site.name,
                "category_url": category_url,
                "page": page_num,
            },
        )
        return None

    def _mark_last_proxy_for_retry(self) -> None:
        marker = getattr(self.engine, "mark_last_proxy_bad", None)
        if not callable(marker):
            return
        try:
            marker(reason="empty_category_page")
        except Exception:
            logger.debug(
                "Не удалось пометить последний прокси перед повторной попыткой",
                extra={"site": self.site.name},
                exc_info=True,
            )

    def _wait_before_retry(self, delay_sec: int) -> None:
        logger.info(
            "Ожидание перед повторной загрузкой категории",
            extra={
                "site": self.site.name,
                "delay_sec": delay_sec,
                "delay_min": round(delay_sec / 60, 2),
            },
        )
        time.sleep(delay_sec)

    def _log_skipped_product(self, url: str, error: Exception | None) -> None:
        try:
            self._skipped_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._skipped_log_path.open("a", encoding="utf-8") as dump:
                line = f"{datetime.now(timezone.utc).isoformat()} {url}"
                if error:
                    line = f"{line} | {error}"
                dump.write(f"{line}\n")
        except Exception as exc:
            logger.error(
                "Не удалось записать лог пропущенного товара",
                extra={"url": url, "error": str(exc)},
            )

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

    def _persist_state(
        self,
        category_url: str,
        *,
        next_page: int,
        page_offset: int,
        total: int,
    ) -> None:
        self.context.state_store.upsert(
            CategoryState(
                site_name=self.site.name,
                category_url=category_url,
                last_page=next_page,
                last_offset=page_offset,
                last_product_count=total,
                last_run_ts=datetime.now(timezone.utc),
            )
        )

    def _reached_product_limit(self, metrics: CategoryMetrics) -> bool:
        max_products = self.site.limits.max_products
        if max_products and metrics.total_written >= max_products:
            return True
        return False

    @staticmethod
    def _content_loaded(content: ProductContent) -> bool:
        return any(
            [
                content.text_content,
                content.image_url,
                content.name_en,
                content.name_ru,
                content.price_without_discount,
                content.price_with_discount,
            ]
        )

    def _should_stop(self, metrics: CategoryMetrics) -> bool:
        for condition in self.site.stop_conditions:
            if condition.type == "no_new_products" and metrics.total_written == 0:
                return True
        return False

    def _queue_for_flush(self, new_records: list[ProductRecord]) -> None:
        logger.debug(
            "Очередь на запись: size=%s, flush_callback=%s",
            len(new_records) if new_records else 0,
            bool(self.flush_callback),
            extra={"site": self.site.name},
        )
        if not self.flush_callback or not new_records:
            return
        self._pending_chunk.extend(new_records)
        logger.debug(
            "Добавлены записи в буфер перед отправкой",
            extra={"site": self.site.name, "chunk_size": len(self._pending_chunk)},
        )
        if self.flush_products > 0 and len(self._pending_chunk) >= self.flush_products:
            self._emit_pending()

    def _emit_pending(self, force: bool = False) -> None:
        if not self.flush_callback:
            return
        if not self._pending_chunk:
            return
        should_flush = force or (
            self.flush_products > 0 and len(self._pending_chunk) >= self.flush_products
        )
        if not should_flush:
            return
        logger.debug(
            "Отправляем буфер в Google Sheets",
            extra={"site": self.site.name, "size": len(self._pending_chunk), "force": force},
        )
        chunk = self._pending_chunk
        self._pending_chunk = []
        self.flush_callback(chunk)
        # после записи начинаем отсчёт заново

    def _global_stop_reached(self) -> bool:
        return self.context.product_limit_reached()
