from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
import re

from app.config.models import HumanBehaviorConfig, NetworkConfig, PaginationConfig
from app.crawler.behavior import BehaviorContext
from app.crawler.engines import BrowserEngine, EngineRequest, ProxyPool, ProxyExhaustedError
from app.crawler.utils import pick_user_agent
from app.media.image_saver import ImageSaver
from app.logger import get_logger
from app.network.http_client_factory import HttpClientFactory
from app.monitoring import build_error_event

logger = get_logger(__name__)


@dataclass(slots=True)
class ProductContent:
    text_content: str | None = None
    image_url: str | None = None
    image_path: str | None = None
    title: str | None = None
    name_en: str | None = None
    name_ru: str | None = None
    price_without_discount: str | None = None
    price_with_discount: str | None = None


class ProductContentFetcher:
    """Загружает страницу товара (через HTTP или Playwright), извлекает текст и сохраняет изображение."""

    def __init__(
        self,
        network: NetworkConfig,
        image_dir: Path,
        *,
        fetch_engine: Literal["http", "browser"] = "http",
        behavior_config: HumanBehaviorConfig | None = None,
        shared_browser_engine: BrowserEngine | None = None,
        fail_cooldown_threshold: int = 5,
        fail_cooldown_seconds: int = 3600,
    ):
        self.network = network
        self.image_dir = image_dir
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self._mode = fetch_engine
        self._http_client_factory: HttpClientFactory | None = None
        self._browser: BrowserEngine | None = None
        self._owns_browser = False
        self._proxy_pool = ProxyPool(
            network.proxy_pool,
            allow_direct=network.proxy_allow_direct,
            bad_log_path=network.bad_proxy_log_path,
            revive_after_sec=network.proxy_revive_after_sec,
        )
        if fetch_engine == "browser":
            if shared_browser_engine is not None:
                self._browser = shared_browser_engine
                self._owns_browser = False
            else:
                self._browser = BrowserEngine(network, behavior=behavior_config)
                self._owns_browser = True
        else:
            self._http_client_factory = HttpClientFactory(
                base_kwargs={
                    "timeout": network.request_timeout_sec,
                    "follow_redirects": True,
                }
            )
        self.image_saver = ImageSaver(network, image_dir, proxy_pool=self._proxy_pool)
        self._fail_cooldown_threshold = max(0, fail_cooldown_threshold)
        self._fail_cooldown_seconds = max(0, fail_cooldown_seconds)
        self._product_fail_streak = 0

    def fetch(
        self,
        product_url: str,
        image_selector: str | None = None,
        drop_after_selectors: Sequence[str] | None = None,
        exclude_selectors: Sequence[str] | None = None,
        *,
        download_image: bool = True,
        name_en_selector: str | None = None,
        name_ru_selector: str | None = None,
        price_without_discount_selector: str | None = None,
        price_with_discount_selector: str | Sequence[str] | None = None,
        behavior_context: BehaviorContext | None = None,
    ) -> ProductContent:
        proxy_used: str | None = None
        try:
            if self._browser:
                html = self._fetch_html_browser(product_url, behavior_context)
                proxy_used = getattr(self._browser, "last_proxy", None)
            else:
                html, proxy_used = self._fetch_html_http(product_url)
        except Exception:
            self._register_product_failure()
            raise
        if not html:
            self._register_product_failure()
            return ProductContent()
        self._register_product_success()
        soup = BeautifulSoup(html, "lxml")
        text_content = _extract_text_content(
            soup,
            drop_after_selectors,
            exclude_selectors,
        )
        image_url = None
        if image_selector:
            node = soup.select_one(image_selector)
            if node:
                image_url = _extract_image_from_node(node, product_url)
        if not image_url:
            image_url = _extract_main_image_url(soup, product_url)
        title = _extract_title(soup)
        name_en = _extract_text_by_selector(soup, name_en_selector)
        name_ru = _extract_text_by_selector(soup, name_ru_selector)
        price_wo = _clean_price_text(_extract_text_by_selector(soup, price_without_discount_selector))
        price_w = _clean_price_text(_extract_text_by_selector(soup, price_with_discount_selector))

        image_path = None
        if download_image and image_url:
            if self._browser:
                try:
                    content_bytes, content_type = self._browser.fetch_binary(image_url, proxy_used)
                    image_path = self.image_saver.save_from_content(
                        image_url,
                        title or "product",
                        product_url,
                        content_bytes,
                        content_type,
                    )
                except Exception as exc:  # pragma: no cover - зависит от сети
                    logger.warning(
                        "Не удалось скачать изображение через браузер, fallback на HTTP",
                        extra={"url": image_url, "error": str(exc)},
                    )
                    image_path = self.image_saver.save(image_url, title or "product", product_url, proxy=proxy_used)
            else:
                image_path = self.image_saver.save(image_url, title or "product", product_url, proxy=proxy_used)

        return ProductContent(
            text_content=text_content,
            image_url=image_url,
            image_path=image_path,
            title=title,
            name_en=name_en,
            name_ru=name_ru,
            price_without_discount=price_wo,
            price_with_discount=price_w,
        )

    def _fetch_html_http(self, product_url: str) -> tuple[str | None, str | None]:
        if not self._http_client_factory:
            return None, None
        proxy: str | None = None
        try:
            ua = pick_user_agent(self.network)
            headers = {"User-Agent": ua}
            if self.network.accept_language:
                headers["Accept-Language"] = self.network.accept_language
            try:
                proxy = self._proxy_pool.pick()
            except ProxyExhaustedError:
                event = build_error_event(
                    error_type="proxy_pool_exhausted",
                    error_source="app.crawler.content_fetcher",
                    url=product_url,
                    action_required=["refresh_proxy_pool", "add_delay"],
                    metadata=self._proxy_pool.pool_snapshot(),
                )
                logger.error(
                    "Прокси-пул исчерпан для HTTP-загрузки",
                    extra={"url": product_url, "error_event": event},
                )
                return None, None
            client = self._http_client_factory.get(proxy)
            response = client.get(
                product_url,
                headers=headers,
            )
            response.raise_for_status()
            logger.debug("HTTP fetch product url=%s proxy=%s ua=%s", product_url, proxy, ua)
            self._proxy_pool.reset_issue_counter(proxy)
        except httpx.HTTPError as exc:
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 403 and self._proxy_pool:
                self._proxy_pool.mark_forbidden(proxy)
            logger.warning(
                "Не удалось загрузить страницу товара",
                extra={"url": product_url, "error": str(exc)},
            )
            return None, proxy
        return response.text, proxy

    def _fetch_html_browser(
        self, product_url: str, behavior_context: BehaviorContext | None
    ) -> str | None:
        if not self._browser:
            return None
        request = EngineRequest(
            url=product_url,
            wait_conditions=[],
            pagination=PaginationConfig(mode="numbered_pages"),
            behavior_context=behavior_context,
        )
        try:
            return self._browser.fetch_html(request)
        except Exception as exc:  # pragma: no cover - зависит от внешнего сайта
            logger.warning(
                "Не удалось загрузить страницу товара браузером",
                extra={"url": product_url, "error": str(exc)},
            )
            return None

    def close(self) -> None:
        if self._http_client_factory:
            self._http_client_factory.close()
        if self._browser and self._owns_browser:
            self._browser.shutdown()
        self.image_saver.close()

    def _register_product_success(self) -> None:
        if self._product_fail_streak:
            self._product_fail_streak = 0

    def _register_product_failure(self) -> None:
        self._product_fail_streak += 1
        if (
            self._fail_cooldown_threshold <= 0
            or self._product_fail_streak < self._fail_cooldown_threshold
        ):
            return
        logger.warning(
            "Достигнут предел подряд неудачных загрузок карточек, делаем паузу",
            extra={
                "streak": self._product_fail_streak,
                "threshold": self._fail_cooldown_threshold,
                "cooldown_sec": self._fail_cooldown_seconds,
            },
        )
        self._product_fail_streak = 0


def _extract_text_content(
    soup: BeautifulSoup,
    drop_after_selectors: Sequence[str] | None = None,
    exclude_selectors: Sequence[str] | None = None,
) -> str | None:
    if drop_after_selectors:
        _strip_after_selectors(soup, drop_after_selectors)
    if exclude_selectors:
        _remove_selectors(soup, exclude_selectors)
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    text = " ".join(soup.get_text(separator=" ", strip=True).split())
    return text or None


def _strip_after_selectors(soup: BeautifulSoup, selectors: Sequence[str]) -> None:
    for selector in selectors:
        if not selector:
            continue
        node = soup.select_one(selector)
        if not node:
            continue
        to_remove = [node] + list(node.find_all_next())
        for target in to_remove:
            target.decompose()


def _remove_selectors(soup: BeautifulSoup, selectors: Sequence[str]) -> None:
    for selector in selectors:
        if not selector:
            continue
        for node in soup.select(selector):
            node.decompose()


def _extract_text_by_selector(
    soup: BeautifulSoup, selector: str | Sequence[str] | None
) -> str | None:
    if not selector:
        return None
    if isinstance(selector, str):
        selectors = [selector]
    else:
        selectors = [item for item in selector if item]
    for css in selectors:
        node = soup.select_one(css)
        if not node:
            continue
        text = node.get_text(" ", strip=True)
        if text:
            return text
    return None


_PRICE_PATTERN = re.compile(r"(\d[\d\s.,]*)(?:\s*(₽|руб(?:\.|ль|ля|лей)?))?", re.IGNORECASE)


def _clean_price_text(value: str | None) -> str | None:
    if not value:
        return value
    normalized = value.replace("\xa0", " ").strip()
    if not normalized:
        return None
    match = _PRICE_PATTERN.search(normalized)
    if not match:
        return None
    amount = match.group(1) or ""
    currency = match.group(2) or ("₽" if "₽" in normalized else "")
    amount = re.sub(r"[^\d.,]", " ", amount)
    amount = re.sub(r"\s+", " ", amount).strip()
    if not amount:
        return None
    if currency:
        currency = currency.strip()
        if currency.startswith(("руб", "РУБ", "Руб", "рУб")):
            currency = "руб."
        elif currency != "₽":
            currency = currency
    return f"{amount} {currency}".strip()


def _extract_title(soup: BeautifulSoup) -> str | None:
    meta = soup.find("meta", attrs={"property": "og:title"})
    if meta and meta.get("content"):
        return meta["content"].strip()
    title_tag = soup.find("title")
    if title_tag and title_tag.text:
        return title_tag.text.strip()
    h1 = soup.find("h1")
    if h1 and h1.text:
        return h1.text.strip()
    return None


def _extract_image_from_node(node: Any, base_url: str) -> str | None:
    srcset = node.get("srcset") or node.get("data-srcset")
    if srcset:
        return _pick_best_srcset(srcset, base_url)
    src = node.get("src") or node.get("data-src") or node.get("data-nuxt-img")
    if src:
        return urljoin(base_url, src)
    # check nested <source> elements (picture tag)
    if hasattr(node, "find_all"):
        for child in node.find_all("source"):
            source_srcset = child.get("srcset") or child.get("data-srcset")
            if source_srcset:
                url = _pick_best_srcset(source_srcset, base_url)
                if url:
                    return url
    return None


def _extract_main_image_url(soup: BeautifulSoup, base_url: str) -> str | None:
    # 1) og:image
    meta_og = soup.find("meta", attrs={"property": "og:image"})
    if meta_og and meta_og.get("content"):
        return urljoin(base_url, meta_og["content"])

    # 2) data-zoom/data-large attributes
    for attr in ("data-zoom-image", "data-large_image", "data-src", "data-large-src"):
        node = soup.find(attrs={attr: True})
        if node:
            return urljoin(base_url, node.get(attr))

    # 3) srcset с максимальным дескриптором / fallback на обычный src
    for img in soup.find_all("img"):
        url = _extract_image_from_node(img, base_url)
        if url:
            return url

    return None


def _pick_best_srcset(srcset: str, base_url: str) -> str | None:
    candidates = [
        part.strip().split(" ")
        for part in srcset.split(",")
        if part.strip()
    ]
    best_url = None
    best_priority = -1
    best_score = -1.0
    for candidate in candidates:
        url_part = candidate[0]
        descriptor = candidate[1] if len(candidate) > 1 else ""
        priority = 0
        score = 0.0
        if descriptor.endswith("w"):
            priority = 2
            try:
                score = float(descriptor[:-1])
            except ValueError:
                score = 0.0
        elif descriptor.endswith("x"):
            priority = 1
            try:
                score = float(descriptor[:-1])
            except ValueError:
                score = 0.0
        if priority > best_priority or (priority == best_priority and score > best_score):
            best_priority = priority
            best_score = score
            best_url = urljoin(base_url, url_part)
    return best_url
