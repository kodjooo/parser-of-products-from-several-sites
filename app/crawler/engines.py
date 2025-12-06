from __future__ import annotations

import random
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

import httpx
from urllib.parse import urlsplit

from app.config.models import HumanBehaviorConfig, NetworkConfig, PaginationConfig, WaitCondition
from app.crawler.behavior import BehaviorContext, HumanBehaviorController
from app.logger import get_logger
from app.monitoring import build_error_event
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
    def __init__(
        self,
        proxies: list[str],
        override: str | None = None,
        *,
        allow_direct: bool = False,
        bad_log_path: Path | None = None,
        forbidden_threshold: int = 2,
        revive_after_sec: float = 1800.0,
        time_provider: Callable[[], float] | None = None,
    ):
        self.override = override
        self._proxies = proxies or []
        self._has_pool = bool(self._proxies)
        self._allow_direct = allow_direct
        self._bad_log_path = bad_log_path
        self._forbidden_threshold = max(1, forbidden_threshold)
        self._forbidden_counts: dict[str, int] = {}
        self._issue_counts: dict[str, int] = {}
        self._consecutive_errors: dict[tuple[str, str], int] = {}
        self._recent_issue_ts: deque[float] = deque()
        self._issue_window_sec = 300
        self._revive_after_sec = max(0.0, revive_after_sec)
        self._time_provider = time_provider or time.time
        self._blocked_until: dict[str, float | None] = {}
        self._direct_blocked = False
        self._direct_blocked_until: float | None = None

    def pick(self, exclude: Iterable[str | None] | None = None) -> str | None:
        if self.override:
            return self.override
        if not self._has_pool and not self._allow_direct:
            return None
        excluded = set(exclude or [])
        candidates = self._collect_candidates(excluded)
        if not candidates and excluded:
            candidates = self._collect_candidates(set())
        if not candidates:
            raise ProxyExhaustedError("Все прокси из пула помечены как недоступные")
        return random.choice(candidates)

    def _collect_candidates(self, excluded: set[str | None]) -> list[str | None]:
        self._prune_expired_blocks()
        candidates = [
            proxy
            for proxy in self._proxies
            if proxy not in excluded and not self._is_proxy_blocked(proxy)
        ]
        if self._allow_direct and not self._is_direct_blocked() and (None not in excluded):
            candidates.append(None)
        return candidates

    def mark_bad(self, proxy: str | None, *, reason: str | None = None, log: bool = False) -> None:
        key = self._make_key(proxy)
        if proxy and not self.override and self._has_pool:
            self._blocked_until[proxy] = self._compute_block_expiration()
        elif proxy is None and self._allow_direct:
            self._direct_blocked = True
            self._direct_blocked_until = self._compute_block_expiration()
        else:
            return
        self._note_issue_timestamp()
        self._clear_consecutive_for_proxy(proxy)
        if log:
            self._write_bad_entry(key, reason)

    def mark_forbidden(self, proxy: str | None) -> None:
        key = self._make_key(proxy)
        current = self._forbidden_counts.get(key, 0) + 1
        self._forbidden_counts[key] = current
        if current >= self._forbidden_threshold:
            self.mark_bad(proxy, reason="HTTP 403", log=True)

    def _make_key(self, proxy: str | None) -> str:
        return proxy or "__direct__"

    def _write_bad_entry(self, key: str, reason: str | None) -> None:
        if not self._bad_log_path:
            return
        try:
            self._bad_log_path.parent.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            with self._bad_log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{timestamp}\t{key}\t{reason or ''}\n")
        except Exception:  # pragma: no cover - запись логов не должна ронять процесс
            logger.warning("Не удалось записать файл испорченных прокси", extra={"path": str(self._bad_log_path)})

    @property
    def issue_threshold(self) -> int:
        return self._forbidden_threshold

    def register_issue(self, proxy: str | None, *, reason: str) -> bool:
        key = self._make_key(proxy)
        current = self._issue_counts.get(key, 0) + 1
        self._issue_counts[key] = current
        self._note_issue_timestamp()
        if current >= self._forbidden_threshold:
            self.mark_bad(proxy, reason=reason, log=True)
            self._issue_counts[key] = 0
            return True
        return False

    def reset_issue_counter(self, proxy: str | None) -> None:
        key = self._make_key(proxy)
        if key in self._issue_counts:
            del self._issue_counts[key]
        self._clear_consecutive_for_proxy(proxy)
        self._recover_source(proxy)

    def increment_consecutive_error(self, proxy: str | None, error_code: str) -> int:
        key = (self._make_key(proxy), error_code)
        current = self._consecutive_errors.get(key, 0) + 1
        self._consecutive_errors[key] = current
        return current

    def pool_snapshot(self) -> dict[str, Any]:
        self._prune_expired_blocks()
        active = max(0, len(self._proxies) - len(self._blocked_until))
        if self._allow_direct and not self._is_direct_blocked():
            active += 1
        direct_available = self._allow_direct and not self._is_direct_blocked()
        return {
            "total_sources": len(self._proxies) + (1 if self._allow_direct else 0),
            "configured_proxies": len(self._proxies),
            "active_proxies": active,
            "bad_proxies": len(self._blocked_until),
            "allow_direct": self._allow_direct,
            "direct_blocked": self._is_direct_blocked(),
            "recent_issue_count_5m": self._recent_issue_count(self._issue_window_sec),
            "has_direct_slot": direct_available,
            "proxy_revive_after_sec": self._revive_after_sec,
        }

    def _clear_consecutive_for_proxy(self, proxy: str | None) -> None:
        key = self._make_key(proxy)
        for record_key in list(self._consecutive_errors.keys()):
            if record_key[0] == key:
                del self._consecutive_errors[record_key]

    def _note_issue_timestamp(self) -> None:
        now = self._now()
        self._recent_issue_ts.append(now)
        self._trim_recent_issues(self._issue_window_sec)

    def _recent_issue_count(self, window_sec: int) -> int:
        self._trim_recent_issues(window_sec)
        return len(self._recent_issue_ts)

    def _trim_recent_issues(self, window_sec: int) -> None:
        threshold = self._now() - window_sec
        while self._recent_issue_ts and self._recent_issue_ts[0] < threshold:
            self._recent_issue_ts.popleft()

    def _compute_block_expiration(self) -> float | None:
        if self._revive_after_sec <= 0:
            return None
        return self._now() + self._revive_after_sec

    def _prune_expired_blocks(self) -> None:
        if not self._blocked_until:
            return
        now = self._now()
        for proxy, expires_at in list(self._blocked_until.items()):
            if expires_at is not None and expires_at <= now:
                del self._blocked_until[proxy]

    def _is_proxy_blocked(self, proxy: str) -> bool:
        expires_at = self._blocked_until.get(proxy)
        if expires_at is None:
            return proxy in self._blocked_until
        if expires_at <= self._now():
            del self._blocked_until[proxy]
            return False
        return True

    def _is_direct_blocked(self) -> bool:
        if not self._direct_blocked:
            return False
        if self._direct_blocked_until is None:
            return True
        if self._direct_blocked_until <= self._now():
            self._direct_blocked = False
            self._direct_blocked_until = None
            return False
        return True

    def _recover_source(self, proxy: str | None) -> None:
        if proxy is None:
            if self._allow_direct:
                self._direct_blocked = False
                self._direct_blocked_until = None
            return
        self._blocked_until.pop(proxy, None)

    def _now(self) -> float:
        return float(self._time_provider())


class HttpEngine:
    """HTTP-клиент для статичных страниц."""

    def __init__(self, network: NetworkConfig, proxy_override: str | None = None):
        self.network = network
        self._proxy_pool = ProxyPool(
            network.proxy_pool,
            proxy_override,
            allow_direct=network.proxy_allow_direct,
            bad_log_path=network.bad_proxy_log_path,
            revive_after_sec=network.proxy_revive_after_sec,
        )
        self.timeout = network.request_timeout_sec
        self._client_factory = HttpClientFactory(
            base_kwargs={
                "timeout": self.timeout,
                "follow_redirects": True,
            }
        )
        self._last_proxy: str | None = None
        self._url_timeout_counts: dict[str, int] = {}

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
                self._last_proxy = proxy
            except ProxyExhaustedError as exc:
                event = build_error_event(
                    error_type="proxy_pool_exhausted",
                    error_source="app.crawler.engines.HttpEngine",
                    url=request.url,
                    action_required=["refresh_proxy_pool", "add_delay"],
                    metadata=self._proxy_pool.pool_snapshot(),
                )
                logger.error("Прокси-пул исчерпан", extra={"url": request.url, "error_event": event})
                raise RuntimeError(str(exc)) from exc
            try:
                client = self._client_factory.get(proxy)
                response = client.get(request.url, headers=headers)
                response.raise_for_status()
                self._proxy_pool.reset_issue_counter(proxy)
                return response.text
            except httpx.HTTPError as exc:
                if isinstance(exc, httpx.HTTPStatusError):
                    status = exc.response.status_code
                    if status == 403:
                        self._proxy_pool.mark_forbidden(proxy)
                    elif status == 407:
                        self._proxy_pool.mark_bad(proxy)
                    logger.warning(
                        "Ошибка HTTP, повтор с другим прокси",
                        extra={"url": request.url, "error": str(exc), "proxy": proxy},
                    )
                else:
                    self._handle_http_transport_error(
                        exc, request.url, proxy, attempt + 1, attempts
                    )
                wait = backoff[min(attempt, len(backoff) - 1)]
                time.sleep(wait)
        raise RuntimeError(f"Не удалось загрузить {request.url}")

    def shutdown(self) -> None:
        self._client_factory.close()

    def mark_last_proxy_bad(self, reason: str | None = None) -> None:
        if self._last_proxy is None:
            return
        marked = self._proxy_pool.register_issue(
            self._last_proxy,
            reason=reason or "empty_category_page",
        )
        if marked:
            self._last_proxy = None

    def _handle_http_transport_error(
        self,
        exc: httpx.HTTPError,
        url: str,
        proxy: str | None,
        attempt: int,
        total_attempts: int,
    ) -> None:
        extra = {
            "url": url,
            "proxy": proxy,
            "attempt": attempt,
            "max_attempts": total_attempts,
        }
        event: dict[str, Any] | None = None
        if isinstance(exc, httpx.ConnectTimeout):
            streak = self._proxy_pool.increment_consecutive_error(proxy, "CONNECT_TIMEOUT")
            event = build_error_event(
                error_type="ConnectTimeout",
                error_source="httpcore.connection",
                url=url,
                proxy=proxy,
                retry_index=attempt,
                action_required=["change_proxy", "retry"],
                metadata={
                    "timeout_sec": self.timeout,
                    "consecutive_errors_with_proxy": streak,
                },
            )
            self._proxy_pool.mark_bad(proxy, reason="connect_timeout", log=True)
        else:
            cause = exc.__cause__
            cause_text = str(cause or "")
            if isinstance(exc, httpx.ProxyError) or "connect_tcp" in cause_text.lower() or "connectionrefusederror" in cause_text.lower():
                streak = self._proxy_pool.increment_consecutive_error(proxy, "CONNECTION_REFUSED")
                event = build_error_event(
                    error_type="ConnectionRefusedError",
                    error_source="httpcore.proxy_connection",
                    url=url,
                    proxy=proxy,
                    retry_index=attempt,
                    action_required=["change_proxy", "add_delay"],
                    metadata={
                        "failure_streak": streak,
                        "raw_error": cause_text or str(exc),
                    },
                )
                self._proxy_pool.mark_bad(proxy, reason="connection_refused", log=True)
        if event:
            logger.error(
                "HTTP-соединение через прокси не установлено, повторяем с новым источником",
                extra={**extra, "error_event": event},
            )
        else:
            logger.warning(
                "HTTP транспортная ошибка, повтор",
                extra=extra,
                exc_info=True,
            )


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
        self._proxy_pool = ProxyPool(
            network.proxy_pool,
            allow_direct=network.proxy_allow_direct,
            bad_log_path=network.bad_proxy_log_path,
            revive_after_sec=network.proxy_revive_after_sec,
        )
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
        self._url_timeout_counts: dict[str, int] = {}
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
        quick_attempts = max(1, self.network.retry.max_attempts)
        quick_waits = [float(value) for value in (self.network.retry.backoff_sec or [])]
        extra_waits = [120, 240]
        total_attempts = quick_attempts + len(extra_waits)
        used_proxies: set[str | None] = set()
        for attempt in range(total_attempts):
            try:
                exclude = used_proxies if attempt >= quick_attempts else None
                proxy = self._proxy_pool.pick(exclude=exclude)
            except ProxyExhaustedError as exc:
                event = build_error_event(
                    error_type="proxy_pool_exhausted",
                    error_source="app.crawler.engines.BrowserEngine",
                    url=request.url,
                    action_required=["refresh_proxy_pool", "add_delay"],
                    metadata=self._proxy_pool.pool_snapshot(),
                )
                logger.error(
                    "Прокси-пул исчерпан в браузерном движке",
                    extra={"url": request.url, "error_event": event},
                )
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
                html = self._read_page_content(page, request.url, proxy)
                self._last_proxy = proxy
                self._url_timeout_counts.pop(request.url, None)
                used_proxies.add(proxy)
                self._proxy_pool.reset_issue_counter(proxy)
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
                self._proxy_pool.mark_forbidden(proxy)
                self._dispose_context(proxy)
                used_proxies.add(proxy)
            except self._timeout_error as exc:
                used_proxies.add(proxy)
                wait = self._compute_wait(attempt, quick_attempts, total_attempts, quick_waits, extra_waits)
                timeout_count = self._url_timeout_counts.get(request.url, 0) + 1
                self._url_timeout_counts[request.url] = timeout_count
                event = build_error_event(
                    error_type="net::ERR_TIMED_OUT",
                    error_source="Playwright Page.goto",
                    url=request.url,
                    proxy=proxy,
                    retry_index=attempt + 1,
                    action_required=["retry", "increase_timeout", "change_proxy"],
                    metadata={
                        "timeout_sec": self.network.request_timeout_sec,
                        "previous_timeouts": timeout_count - 1,
                        "wait_before_retry_sec": wait,
                        "extended_attempt": attempt >= quick_attempts,
                    },
                )
                logger.warning(
                    "Timeout при загрузке страницы браузером, повтор",
                    extra={
                        "url": request.url,
                        "attempt": attempt + 1,
                        "max_attempts": total_attempts,
                        "wait": wait,
                        "proxy": proxy,
                        "extended": attempt >= quick_attempts,
                        "error_event": event,
                    },
                )
                if attempt == total_attempts - 1:
                    raise RuntimeError(f"Не удалось загрузить {request.url}") from exc
                time.sleep(wait)
            except Exception as exc:
                used_proxies.add(proxy)
                wait = self._compute_wait(attempt, quick_attempts, total_attempts, quick_waits, extra_waits)
                handled = self._handle_playwright_exception(
                    exc,
                    request.url,
                    proxy,
                    attempt,
                    total_attempts,
                    wait,
                    extended=attempt >= quick_attempts,
                )
                if not handled:
                    logger.warning(
                        "Ошибка Playwright при загрузке страницы, смена прокси",
                        extra={
                            "url": request.url,
                            "proxy": proxy,
                            "attempt": attempt + 1,
                            "max_attempts": total_attempts,
                            "extended": attempt >= quick_attempts,
                            "wait": wait,
                        },
                        exc_info=True,
                    )
                    self._proxy_pool.mark_bad(proxy)
                    self._dispose_context(proxy)
                if attempt == total_attempts - 1:
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
        self._contexts.clear()

    def mark_last_proxy_bad(self, reason: str | None = None) -> None:
        if self._last_proxy is None:
            return
        marked = self._proxy_pool.register_issue(
            self._last_proxy,
            reason=reason or "empty_category_page",
        )
        if marked:
            self._dispose_context(self._last_proxy)
            self._last_proxy = None

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
        if proxy == self._last_proxy:
            self._last_proxy = None

    def _build_default_headers(self) -> dict[str, str]:
        if not self.network.accept_language:
            return {}
        return {"Accept-Language": self.network.accept_language}

    def _read_page_content(self, page, url: str, proxy: str | None) -> str:
        try:
            return page.content()
        except Exception as exc:
            message = str(exc)
            if "Page.content: Unable to retrieve content because the page is navigating" not in message:
                raise
            jitter = random.uniform(0.5, 1.0)
            event = build_error_event(
                error_type="Page.content:navigating",
                error_source="Playwright Page.content",
                url=url,
                proxy=proxy,
                action_required=["wait_for_networkidle", "retry"],
                metadata={"retry_delay_sec": round(jitter, 2)},
            )
            logger.warning(
                "Playwright не дождался завершения навигации перед чтением контента, повтор",
                extra={
                    "url": url,
                    "proxy": proxy,
                    "retry_delay_sec": round(jitter, 2),
                    "error_event": event,
                },
            )
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(jitter * 1000)
            return page.content()

    def _handle_playwright_exception(
        self,
        exc: Exception,
        url: str,
        proxy: str | None,
        attempt: int,
        total_attempts: int,
        wait: float,
        *,
        extended: bool,
    ) -> bool:
        message = str(exc)
        extra_base = {
            "url": url,
            "proxy": proxy,
            "attempt": attempt + 1,
            "max_attempts": total_attempts,
            "wait": wait,
            "extended": extended,
        }
        if "ERR_PROXY_CONNECTION_FAILED" in message:
            streak = self._proxy_pool.increment_consecutive_error(proxy, "ERR_PROXY_CONNECTION_FAILED")
            event = build_error_event(
                error_type="net::ERR_PROXY_CONNECTION_FAILED",
                error_source="Playwright Page.goto",
                url=url,
                proxy=proxy,
                retry_index=attempt + 1,
                action_required="change_proxy",
                metadata={"consecutive_errors_with_proxy": streak, "wait_before_retry_sec": wait},
            )
            logger.error(
                "Playwright не может подключиться через прокси, исключаем его",
                extra={**extra_base, "error_event": event},
            )
            self._proxy_pool.mark_bad(proxy, reason="ERR_PROXY_CONNECTION_FAILED", log=True)
            self._dispose_context(proxy)
            return True
        if "ERR_SOCKET_NOT_CONNECTED" in message:
            streak = self._proxy_pool.increment_consecutive_error(proxy, "ERR_SOCKET_NOT_CONNECTED")
            event = build_error_event(
                error_type="net::ERR_SOCKET_NOT_CONNECTED",
                error_source="Playwright Page.goto",
                url=url,
                proxy=proxy,
                retry_index=attempt + 1,
                action_required=["retry", "change_proxy", "add_delay"],
                metadata={"consecutive_errors_with_proxy": streak, "wait_before_retry_sec": wait},
            )
            logger.error(
                "Playwright сообщает ERR_SOCKET_NOT_CONNECTED, пробуем другой прокси",
                extra={**extra_base, "error_event": event},
            )
            self._proxy_pool.mark_bad(proxy, reason="ERR_SOCKET_NOT_CONNECTED", log=True)
            self._dispose_context(proxy)
            return True
        if "net::ERR_TIMED_OUT" in message:
            streak = self._proxy_pool.increment_consecutive_error(proxy, "ERR_TIMED_OUT")
            event = build_error_event(
                error_type="net::ERR_TIMED_OUT",
                error_source="Playwright Page.goto",
                url=url,
                proxy=proxy,
                retry_index=attempt + 1,
                action_required=["retry", "increase_timeout", "change_proxy"],
                metadata={
                    "timeout_sec": self.network.request_timeout_sec,
                    "consecutive_errors_with_proxy": streak,
                    "wait_before_retry_sec": wait,
                },
            )
            logger.warning(
                "Playwright сообщает net::ERR_TIMED_OUT, увеличиваем ожидание и меняем прокси",
                extra={**extra_base, "error_event": event},
            )
            self._proxy_pool.mark_bad(proxy, reason="ERR_TIMED_OUT", log=True)
            self._dispose_context(proxy)
            return True
        if isinstance(exc, ProxyBannedError):
            # Уже обработано в отдельном блоке
            return False
        return False
    @staticmethod
    def _compute_wait(
        attempt_index: int,
        quick_attempts: int,
        total_attempts: int,
        quick_waits: list[float],
        extra_waits: list[int],
    ) -> float:
        if attempt_index >= total_attempts - 1:
            return 0.0
        if attempt_index < quick_attempts - 1:
            return quick_waits[min(attempt_index, len(quick_waits) - 1)] if quick_waits else 0.0
        extra_index = attempt_index - (quick_attempts - 1)
        if 0 <= extra_index < len(extra_waits):
            return float(extra_waits[extra_index])
        return quick_waits[-1] if quick_waits else 0.0



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
