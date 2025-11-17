from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Iterable, Protocol

import httpx

from app.config.models import NetworkConfig, PaginationConfig, WaitCondition
from app.logger import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class EngineRequest:
    url: str
    wait_conditions: Iterable[WaitCondition]
    pagination: PaginationConfig
    scroll_limit: int | None = None


class CrawlerEngine(Protocol):
    def fetch_html(self, request: EngineRequest) -> str: ...

    def shutdown(self) -> None: ...


class HttpEngine:
    """HTTP-клиент для статичных страниц."""

    def __init__(self, network: NetworkConfig, proxy_override: str | None = None):
        self.network = network
        self.proxy_override = proxy_override
        self.timeout = network.request_timeout_sec
        self.client = httpx.Client(
            timeout=self.timeout,
            follow_redirects=True,
        )

    def fetch_html(self, request: EngineRequest) -> str:
        for condition in request.wait_conditions:
            if condition.type == "delay":
                time.sleep(float(condition.value))
        headers = {"User-Agent": random.choice(self.network.user_agents)}
        proxy = self.proxy_override or random.choice(self.network.proxy_pool) if self.network.proxy_pool else None
        attempts = self.network.retry.max_attempts
        backoff = self.network.retry.backoff_sec
        for attempt in range(attempts):
            try:
                response = self.client.get(request.url, headers=headers, proxy=proxy)
                response.raise_for_status()
                return response.text
            except httpx.HTTPError as exc:
                wait = backoff[min(attempt, len(backoff) - 1)]
                logger.warning("Ошибка HTTP, повтор", extra={"url": request.url, "error": str(exc), "wait": wait})
                time.sleep(wait)
        raise RuntimeError(f"Не удалось загрузить {request.url}")

    def shutdown(self) -> None:
        self.client.close()


class BrowserEngine:
    """Headless-браузер на базе Playwright (используется для динамики)."""

    def __init__(self, network: NetworkConfig):
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
        except ImportError as exc:  # pragma: no cover - зависит от опциональной либы
            raise RuntimeError(
                "Для режима engine=browser требуется playwright. "
                "Убедитесь, что выполнена команда `playwright install`."
            ) from exc

        self.network = network
        self._timeout_error = PlaywrightTimeoutError
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
        self._context = self._browser.new_context(user_agent=random.choice(network.user_agents))

    def fetch_html(self, request: EngineRequest) -> str:
        page = self._context.new_page()
        try:
            page.set_default_timeout(self.network.request_timeout_sec * 1000)
            self._goto_with_retry(page, request.url)
            self._apply_wait_conditions(page, request.wait_conditions)
            if request.pagination.mode == "infinite_scroll":
                self._perform_infinite_scroll(page, request.scroll_limit or request.pagination.max_scrolls or 30)
            html = page.content()
            return html
        finally:
            page.close()

    def _goto_with_retry(self, page, url: str) -> None:
        attempts = max(1, self.network.retry.max_attempts)
        backoff = self.network.retry.backoff_sec or [1]
        for attempt in range(attempts):
            try:
                page.goto(url, wait_until="domcontentloaded")
                return
            except self._timeout_error as exc:  # pragma: no cover — зависит от внешнего сайта
                wait = backoff[min(attempt, len(backoff) - 1)]
                logger.warning(
                    "Timeout при загрузке страницы браузером, повтор",
                    extra={
                        "url": url,
                        "attempt": attempt + 1,
                        "max_attempts": attempts,
                        "wait": wait,
                    },
                )
                if attempt == attempts - 1:
                    raise RuntimeError(f"Не удалось загрузить {url}") from exc
                page.wait_for_timeout(wait * 1000)

    def _apply_wait_conditions(self, page, conditions: Iterable[WaitCondition]) -> None:
        for condition in conditions:
            if condition.type == "delay":
                time.sleep(float(condition.value))
            elif condition.type == "selector":
                page.wait_for_selector(condition.value, timeout=condition.timeout_sec * 1000)

    def _perform_infinite_scroll(self, page, limit: int) -> None:
        for _ in range(limit):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
            page.wait_for_timeout(1000)

    def shutdown(self) -> None:
        self._context.close()
        self._browser.close()
        self._playwright.stop()


def create_engine(engine_type: str, network: NetworkConfig) -> CrawlerEngine:
    if engine_type == "browser":
        return BrowserEngine(network)
    return HttpEngine(network)
