from __future__ import annotations

import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol

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


class ProxyPool:
    def __init__(self, proxies: list[str], override: str | None = None):
        self.override = override
        self._proxies = proxies or []
        self._bad: set[str] = set()
        self._has_pool = bool(self._proxies)

    def pick(self) -> str | None:
        if self.override:
            return self.override
        if not self._has_pool:
            return None
        candidates = [proxy for proxy in self._proxies if proxy not in self._bad]
        if not candidates:
            raise ProxyExhaustedError("Все прокси из пула помечены как недоступные")
        return random.choice(candidates)

    def mark_bad(self, proxy: str | None) -> None:
        if proxy and not self.override and self._has_pool:
            self._bad.add(proxy)


class HttpEngine:
    """HTTP-клиент для статичных страниц."""

    def __init__(self, network: NetworkConfig, proxy_override: str | None = None):
        self.network = network
        self._proxy_pool = ProxyPool(network.proxy_pool, proxy_override)
        self.timeout = network.request_timeout_sec
        self.client = httpx.Client(
            timeout=self.timeout,
            follow_redirects=True,
        )

    def _pick_proxy(self) -> str | None:
        return self._proxy_pool.pick()

    def fetch_html(self, request: EngineRequest) -> str:
        for condition in request.wait_conditions:
            if condition.type == "delay":
                time.sleep(float(condition.value))
        headers = {"User-Agent": random.choice(self.network.user_agents)}
        attempts = self.network.retry.max_attempts
        backoff = self.network.retry.backoff_sec
        for attempt in range(attempts):
            try:
                proxy = self._pick_proxy()
            except ProxyExhaustedError as exc:
                logger.error("Прокси-пул исчерпан", extra={"url": request.url})
                raise RuntimeError(str(exc)) from exc
            try:
                response = self.client.get(request.url, headers=headers, proxy=proxy)
                response.raise_for_status()
                return response.text
            except httpx.HTTPError as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {403, 407}:
                    self._proxy_pool.mark_bad(proxy)
                    logger.warning(
                        "Ошибка HTTP, повтор с другим прокси",
                        extra={"url": request.url, "error": str(exc), "proxy": proxy},
                    )
                wait = backoff[min(attempt, len(backoff) - 1)]
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
        self._proxy_pool = ProxyPool(network.proxy_pool)
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
        storage_state_arg = None
        storage_path = network.browser_storage_state_path
        if storage_path:
            path_obj = Path(storage_path)
            if path_obj.exists():
                storage_state_arg = str(path_obj)
                logger.info(
                    "Загружены cookies Playwright",
                    extra={"path": str(path_obj)},
                )
            else:
                logger.warning(
                    "Файл storage_state не найден",
                    extra={"path": str(path_obj)},
                )
        self._storage_state = storage_state_arg
        self._contexts: dict[str | None, Any] = {}

    def fetch_html(self, request: EngineRequest) -> str:
        attempts = max(1, self.network.retry.max_attempts)
        backoff = self.network.retry.backoff_sec or [1]
        for attempt in range(attempts):
            try:
                proxy = self._proxy_pool.pick()
            except ProxyExhaustedError as exc:
                logger.error("Прокси-пул исчерпан в браузерном движке", extra={"url": request.url})
                raise RuntimeError(str(exc)) from exc
            context = self._get_or_create_context(proxy)
            page = context.new_page()
            try:
                page.set_default_timeout(self.network.request_timeout_sec * 1000)
                response = page.goto(request.url, wait_until="domcontentloaded")
                if response and response.status == 403:
                    raise ProxyBannedError
                self._apply_wait_conditions(page, request.wait_conditions)
                if request.pagination.mode == "infinite_scroll":
                    self._perform_infinite_scroll(
                        page, request.scroll_limit or request.pagination.max_scrolls or 30
                    )
                html = page.content()
                return html
            except ProxyBannedError:
                logger.warning(
                    "Браузер получил 403, смена прокси",
                    extra={"url": request.url, "proxy": proxy},
                )
                self._proxy_pool.mark_bad(proxy)
                self._dispose_context(proxy)
            except self._timeout_error as exc:
                wait = backoff[min(attempt, len(backoff) - 1)]
                logger.warning(
                    "Timeout при загрузке страницы браузером, повтор",
                    extra={
                        "url": request.url,
                        "attempt": attempt + 1,
                        "max_attempts": attempts,
                        "wait": wait,
                        "proxy": proxy,
                    },
                )
                if attempt == attempts - 1:
                    raise RuntimeError(f"Не удалось загрузить {request.url}") from exc
                time.sleep(wait)
            finally:
                page.close()
        raise RuntimeError(f"Не удалось загрузить {request.url}")

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
        for context in self._contexts.values():
            context.close()
        self._browser.close()
        self._playwright.stop()

    def _get_or_create_context(self, proxy: str | None):
        key = proxy or "__direct__"
        context = self._contexts.get(key)
        if context is None:
            proxy_arg = {"server": proxy} if proxy else None
            context = self._browser.new_context(
                user_agent=random.choice(self.network.user_agents),
                storage_state=self._storage_state,
                proxy=proxy_arg,
            )
            self._contexts[key] = context
        return context

    def _dispose_context(self, proxy: str | None) -> None:
        key = proxy or "__direct__"
        context = self._contexts.pop(key, None)
        if context:
            context.close()


class ProxyBannedError(Exception):
    pass


class ProxyExhaustedError(Exception):
    pass


def create_engine(engine_type: str, network: NetworkConfig) -> CrawlerEngine:
    if engine_type == "browser":
        return BrowserEngine(network)
    return HttpEngine(network)
