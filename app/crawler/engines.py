from __future__ import annotations

import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol

import httpx
from urllib.parse import urlsplit

from app.config.models import HumanBehaviorConfig, NetworkConfig, PaginationConfig, WaitCondition
from app.crawler.behavior import BehaviorContext, HumanBehaviorController
from app.logger import get_logger
from app.network.http_client_factory import HttpClientFactory

logger = get_logger(__name__)


@dataclass(slots=True)
class EngineRequest:
    url: str
    wait_conditions: Iterable[WaitCondition]
    pagination: PaginationConfig
    scroll_limit: int | None = None
    behavior_context: BehaviorContext | None = None


class CrawlerEngine(Protocol):
    def fetch_html(self, request: EngineRequest) -> str: ...

    def shutdown(self) -> None: ...


class ProxyPool:
    def __init__(self, proxies: list[str], override: str | None = None, allow_direct: bool = False):
        self.override = override
        self._proxies = proxies or []
        self._bad: set[str] = set()
        self._has_pool = bool(self._proxies)
        self._allow_direct = allow_direct

    def pick(self) -> str | None:
        if self.override:
            return self.override
        if not self._has_pool and not self._allow_direct:
            return None
        candidates = [proxy for proxy in self._proxies if proxy not in self._bad]
        if self._allow_direct:
            candidates.append(None)
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
        self._proxy_pool = ProxyPool(network.proxy_pool, proxy_override, allow_direct=network.proxy_allow_direct)
        self.timeout = network.request_timeout_sec
        self._client_factory = HttpClientFactory(
            base_kwargs={
                "timeout": self.timeout,
                "follow_redirects": True,
            }
        )

    def _pick_proxy(self) -> str | None:
        return self._proxy_pool.pick()

    def fetch_html(self, request: EngineRequest) -> str:
        for condition in request.wait_conditions:
            if condition.type == "delay":
                time.sleep(float(condition.value))
        headers = {"User-Agent": random.choice(self.network.user_agents)}
        if self.network.accept_language:
            headers["Accept-Language"] = self.network.accept_language
        attempts = self.network.retry.max_attempts
        backoff = self.network.retry.backoff_sec
        for attempt in range(attempts):
            try:
                proxy = self._pick_proxy()
            except ProxyExhaustedError as exc:
                logger.error("Прокси-пул исчерпан", extra={"url": request.url})
                raise RuntimeError(str(exc)) from exc
            try:
                client = self._client_factory.get(proxy)
                response = client.get(request.url, headers=headers)
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
        self._client_factory.close()


class BrowserEngine:
    """Headless-браузер на базе Playwright (используется для динамики)."""

    def __init__(self, network: NetworkConfig, behavior: HumanBehaviorConfig | None = None):
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
        except ImportError as exc:  # pragma: no cover - зависит от опциональной либы
            raise RuntimeError(
                "Для режима engine=browser требуется playwright. "
                "Убедитесь, что выполнена команда `playwright install`."
            ) from exc

        self.network = network
        self._timeout_error = PlaywrightTimeoutError
        self._proxy_pool = ProxyPool(network.proxy_pool, allow_direct=network.proxy_allow_direct)
        self._playwright = sync_playwright().start()
        slow_mo_ms = int(network.browser_slow_mo_ms or 0)
        if not network.browser_headless:
            logger.warning("Playwright запущен в визуальном режиме (headless=False)")
        if slow_mo_ms > 0:
            logger.info("Playwright slow-mo активирован", extra={"slow_mo_ms": slow_mo_ms})
        self._browser = self._playwright.chromium.launch(
            headless=network.browser_headless,
            slow_mo=slow_mo_ms or None,
        )
        self._behavior = HumanBehaviorController(
            behavior,
            default_timeout_sec=network.request_timeout_sec,
            extra_page_preview_sec=network.browser_extra_page_preview_sec,
        )
        self._preview_delay_sec = max(0.0, float(network.browser_preview_delay_sec or 0.0))
        self._preview_before_sec = max(
            0.0, float(network.browser_preview_before_behavior_sec or 0.0)
        )
        if self._preview_delay_sec > 0:
            logger.info(
                "Режим визуального предпросмотра включён",
                extra={"hold_sec": self._preview_delay_sec},
            )
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
        self._last_proxy: str | None = None

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
                logger.debug(
                    "Page navigation url=%s proxy=%s user_agent=%s cookies_loaded=%s headers=%s",
                    request.url,
                    proxy,
                    context._options.get("user_agent") if hasattr(context, "_options") else None,  # type: ignore[attr-defined]
                    bool(self._storage_state),
                    self._build_default_headers(),
                )
                response = page.goto(request.url, wait_until="domcontentloaded")
                if response and response.status == 403:
                    raise ProxyBannedError
                self._apply_wait_conditions(page, request.wait_conditions)
                if request.pagination.mode == "infinite_scroll":
                    self._perform_infinite_scroll(
                        page, request.scroll_limit or request.pagination.max_scrolls or 30
                    )
                if self._preview_before_sec > 0:
                    logger.debug(
                        "Задержка перед поведенческим слоем для визуализации",
                        extra={
                            "url": request.url,
                            "delay_sec": self._preview_before_sec,
                        },
                    )
                    page.wait_for_timeout(self._preview_before_sec * 1000)
                behavior_meta = {"url": request.url, "proxy": proxy}
                self._behavior.apply(
                    page,
                    context=request.behavior_context,
                    meta=behavior_meta,
                )
                html = page.content()
                self._last_proxy = proxy
                if self._preview_delay_sec > 0:
                    logger.debug(
                        "Пауза перед закрытием страницы для предпросмотра",
                        extra={
                            "url": request.url,
                            "delay_sec": self._preview_delay_sec,
                        },
                    )
                    page.wait_for_timeout(self._preview_delay_sec * 1000)
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
            except Exception as exc:
                wait = backoff[min(attempt, len(backoff) - 1)]
                logger.warning(
                    "Ошибка Playwright при загрузке страницы, смена прокси",
                    extra={
                        "url": request.url,
                        "proxy": proxy,
                        "attempt": attempt + 1,
                        "max_attempts": attempts,
                    },
                    exc_info=True,
                )
                self._proxy_pool.mark_bad(proxy)
                self._dispose_context(proxy)
                if attempt == attempts - 1:
                    raise RuntimeError(f"Не удалось загрузить {request.url}") from exc
                time.sleep(wait)
            finally:
                page.close()
        raise RuntimeError(f"Не удалось загрузить {request.url}")

    def fetch_binary(self, url: str, proxy: str | None = None) -> tuple[bytes, str | None]:
        context = self._get_or_create_context(proxy)
        timeout_ms = int(self.network.request_timeout_sec * 1000)
        response = context.request.get(url, timeout=timeout_ms)
        if response.status != 200:
            raise RuntimeError(f"Не удалось загрузить ресурс {url}, статус {response.status}")
        logger.debug("Binary fetch via browser url=%s proxy=%s status=%s", url, proxy, response.status)
        return response.body(), response.headers.get("content-type")

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
            proxy_arg = self._build_proxy_settings(proxy)
            headers = self._build_default_headers()
            context_kwargs: dict[str, Any] = {
                "user_agent": random.choice(self.network.user_agents),
                "storage_state": self._storage_state,
                "proxy": proxy_arg,
            }
            if self.network.accept_language:
                context_kwargs["locale"] = self.network.accept_language
            if headers:
                context_kwargs["extra_http_headers"] = headers
            context = self._browser.new_context(**context_kwargs)
            self._contexts[key] = context
        return context

    @property
    def last_proxy(self) -> str | None:
        return self._last_proxy

    def _build_proxy_settings(self, proxy: str | None) -> dict[str, Any] | None:
        if not proxy:
            return None
        try:
            parsed = urlsplit(proxy)
        except ValueError:
            return {"server": proxy}
        scheme = parsed.scheme or "http"
        host = parsed.hostname
        port = parsed.port
        if not host:
            return {"server": proxy}
        server = f"{scheme}://{host}"
        if port:
            server = f"{server}:{port}"
        proxy_kwargs: dict[str, Any] = {"server": server}
        if parsed.username:
            proxy_kwargs["username"] = parsed.username
        if parsed.password:
            proxy_kwargs["password"] = parsed.password
        return proxy_kwargs

    def _dispose_context(self, proxy: str | None) -> None:
        key = proxy or "__direct__"
        context = self._contexts.pop(key, None)
        if context:
            context.close()

    def _build_default_headers(self) -> dict[str, str]:
        if not self.network.accept_language:
            return {}
        return {"Accept-Language": self.network.accept_language}


class ProxyBannedError(Exception):
    pass


class ProxyExhaustedError(Exception):
    pass


def create_engine(
    engine_type: str,
    network: NetworkConfig,
    behavior: HumanBehaviorConfig | None = None,
) -> CrawlerEngine:
    if engine_type == "browser":
        return BrowserEngine(network, behavior=behavior)
    return HttpEngine(network)
