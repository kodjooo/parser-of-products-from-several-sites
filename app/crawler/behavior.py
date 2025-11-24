from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

from app.config.models import HumanBehaviorConfig
from app.crawler.utils import jitter_sleep
from app.logger import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class BehaviorContext:
    """Дополнительные данные страницы для поведенческого слоя."""

    product_link_selector: str | None = None
    category_url: str | None = None
    base_url: str | None = None
    root_url: str | None = None


@dataclass(slots=True)
class BehaviorResult:
    actions: list[str] = field(default_factory=list)
    duration_sec: float = 0.0


class HumanBehaviorController:
    """Имитация поведения пользователя поверх Playwright."""

    def __init__(
        self,
        config: HumanBehaviorConfig | None,
        default_timeout_sec: float,
        *,
        extra_page_preview_sec: float = 0.0,
    ):
        self.config = config or HumanBehaviorConfig()
        self.enabled = bool(self.config.enabled)
        self.debug = bool(self.config.debug)
        self._timeout_sec = default_timeout_sec
        self._extra_page_preview_sec = max(0.0, float(extra_page_preview_sec or 0.0))

    def apply(
        self,
        page: Any,
        *,
        context: BehaviorContext | None,
        meta: dict[str, Any] | None = None,
    ) -> BehaviorResult:
        result = BehaviorResult()
        if not self.enabled:
            return result
        started = time.perf_counter()
        meta = meta or {}
        actions: list[str] = []
        limit_value = self.config.navigation.max_additional_chain
        remaining_nav: int | None
        if limit_value <= 0:
            remaining_nav = 0
        else:
            remaining_nav = limit_value
        try:
            actions.extend(self._maybe_scroll(page))
            actions.extend(self._maybe_move_mouse(page))
            actions.extend(self._maybe_hover(page))
            nav_actions = self._maybe_back_and_forward(page, remaining_nav)
            actions.extend(nav_actions)
            remaining_nav = _decrease_remaining(remaining_nav, len(nav_actions))
            root_actions = self._maybe_visit_root(page, context, remaining_nav)
            actions.extend(root_actions)
            remaining_nav = _decrease_remaining(remaining_nav, len(root_actions))
            extra_actions = self._maybe_open_extra_products(page, context, remaining_nav)
            actions.extend(extra_actions)
            result.actions = actions
        finally:
            result.duration_sec = max(0.0, time.perf_counter() - started)
            log_payload = {
                "url": meta.get("url"),
                "proxy": meta.get("proxy"),
                "context": meta.get("context"),
                "actions": actions,
                "duration_sec": round(result.duration_sec, 3),
            }
            if actions or self.debug:
                logger.info("Поведенческий слой отработал", extra=log_payload)
            else:
                logger.debug("Поведенческий слой пропущен", extra=log_payload)
        return result

    def _maybe_scroll(self, page: Any) -> list[str]:
        if random.random() > self.config.scroll.probability:
            return []
        if random.random() < self.config.scroll.skip_probability:
            return []
        if not hasattr(page, "evaluate"):
            return []
        actions: list[str] = []
        steps = random.randint(self.config.scroll.min_steps, self.config.scroll.max_steps)
        depth = random.randint(
            self.config.scroll.min_depth_percent,
            self.config.scroll.max_depth_percent,
        )
        current = 0
        for _ in range(steps):
            increment = depth / steps
            current = min(100, current + increment + random.uniform(-5, 5))
            fraction = max(0.0, min(1.0, current / 100))
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight * arguments[0]);", fraction)
            except Exception as exc:  # pragma: no cover - зависит от браузера
                logger.debug("Не удалось выполнить скролл", extra={"error": str(exc)})
                break
            actions.append(f"scroll:{int(fraction * 100)}")
            self._wait(self.config.scroll.pause_between_steps)
        if random.random() < 0.15:
            try:
                page.evaluate("window.scrollTo(0, 0);")
                actions.append("scroll:back-to-top")
            except Exception:  # pragma: no cover - зависит от браузера
                pass
        return actions

    def _maybe_move_mouse(self, page: Any) -> list[str]:
        if not hasattr(page, "mouse"):
            return []
        count = random.randint(self.config.mouse.move_count_min, self.config.mouse.move_count_max)
        if count <= 0:
            return []
        viewport = getattr(page, "viewport_size", None) or {"width": 1920, "height": 1080}
        width = viewport.get("width", 1920)
        height = viewport.get("height", 1080)
        actions: list[str] = []
        for _ in range(count):
            target_x = random.randint(int(width * 0.1), max(int(width * 0.9), 1))
            target_y = random.randint(int(height * 0.1), max(int(height * 0.9), 1))
            try:
                page.mouse.move(target_x, target_y, steps=random.randint(10, 25))
                actions.append(f"mouse_move:{target_x}x{target_y}")
            except Exception as exc:  # pragma: no cover
                logger.debug("Не удалось переместить курсор", extra={"error": str(exc)})
                break
            self._wait(self.config.action_delay)
        return actions

    def _maybe_hover(self, page: Any) -> list[str]:
        if not self.config.mouse.hover_selectors:
            return []
        if random.random() > self.config.mouse.hover_probability:
            return []
        if not hasattr(page, "query_selector_all"):
            return []
        actions: list[str] = []
        selectors = [selector for selector in self.config.mouse.hover_selectors if selector]
        random.shuffle(selectors)
        for selector in selectors:
            try:
                nodes = page.query_selector_all(selector)
            except Exception:  # pragma: no cover
                nodes = []
            if not nodes:
                continue
            node = random.choice(nodes)
            if not hasattr(page, "mouse"):
                continue
            try:
                bbox = node.bounding_box()
            except Exception:  # pragma: no cover
                bbox = None
            if not bbox:
                continue
            target_x = bbox.get("x", 0) + bbox.get("width", 0) / 2 + random.uniform(-5, 5)
            target_y = bbox.get("y", 0) + bbox.get("height", 0) / 2 + random.uniform(-5, 5)
            target_x = max(0, target_x)
            target_y = max(0, target_y)
            try:
                page.mouse.move(
                    target_x,
                    target_y,
                    steps=random.randint(15, 30),
                )
            except Exception as exc:  # pragma: no cover
                logger.debug(
                    "Не удалось плавно переместить курсор к элементу",
                    extra={"selector": selector, "error": str(exc)},
                )
                continue
            actions.append(f"hover:{selector}")
            self._wait(self.config.action_delay)
            break
        return actions

    def _maybe_open_extra_products(
        self,
        page: Any,
        context: BehaviorContext | None,
        remaining: int | None,
    ) -> list[str]:
        config = self.config.navigation
        if random.random() > config.extra_products_probability:
            return []
        if remaining is not None and remaining <= 0:
            return []
        if not context or not context.product_link_selector:
            return []
        if not hasattr(page, "query_selector_all"):
            return []
        try:
            nodes = page.query_selector_all(context.product_link_selector)
        except Exception:  # pragma: no cover
            nodes = []
        if not nodes:
            return []
        random.shuffle(nodes)
        limit = max(0, config.extra_products_limit)
        if remaining is not None:
            limit = min(limit, remaining)
        base_url = context.base_url or context.category_url or ""
        actions: list[str] = []
        for node in nodes[:limit]:
            href = None
            try:
                href = node.get_attribute("href")
            except Exception:  # pragma: no cover
                continue
            if not href:
                continue
            absolute = urljoin(base_url, href)
            if not absolute:
                continue
            self._scroll_to_node(page, node)
            opened = self._open_in_new_page(page, absolute)
            if opened:
                actions.append(f"extra_product:{absolute}")
            self._wait(self.config.action_delay)
        return actions

    def _maybe_visit_root(
        self,
        page: Any,
        context: BehaviorContext | None,
        remaining: int | None,
    ) -> list[str]:
        config = self.config.navigation
        if random.random() > config.visit_root_probability:
            return []
        if not context or not context.root_url:
            return []
        if remaining is not None and remaining <= 0:
            return []
        opened = self._open_in_new_page(page, context.root_url)
        if not opened:
            return []
        return [f"visit_root:{context.root_url}"]

    def _maybe_back_and_forward(self, page: Any, remaining: int | None) -> list[str]:
        if random.random() > self.config.navigation.back_probability:
            return []
        if not hasattr(page, "go_back"):
            return []
        if remaining is not None and remaining < 2:
            return []
        actions: list[str] = []
        try:
            page.go_back(wait_until="domcontentloaded", timeout=self._timeout_sec * 1000)
            actions.append("back")
            self._wait(self.config.action_delay)
            page.go_forward(wait_until="domcontentloaded", timeout=self._timeout_sec * 1000)
            actions.append("forward")
        except Exception as exc:  # pragma: no cover
            logger.debug("Back/forward не удались", extra={"error": str(exc)})
        return actions

    def _open_in_new_page(self, page: Any, url: str) -> bool:
        context = getattr(page, "context", None)
        if not context:
            return False
        try:
            extra_page = context.new_page()
        except Exception:  # pragma: no cover
            return False
        success = False
        try:
            extra_page.set_default_timeout(self._timeout_sec * 1000)
            extra_page.goto(url, wait_until="domcontentloaded")
            if self._extra_page_preview_sec > 0:
                extra_page.wait_for_timeout(self._extra_page_preview_sec * 1000)
            success = True
        except Exception as exc:  # pragma: no cover
            logger.debug("Не удалось открыть дополнительную страницу", extra={"url": url, "error": str(exc)})
        finally:
            try:
                extra_page.close()
            except Exception:  # pragma: no cover
                pass
        return success

    def _scroll_to_node(self, page: Any, node: Any) -> None:
        if not hasattr(page, "evaluate") or node is None:
            return
        try:
            page.evaluate(
                "(element) => element.scrollIntoView({behavior: 'smooth', block: 'center', inline: 'center'});",
                node,
            )
            self._wait(self.config.action_delay)
        except Exception as exc:  # pragma: no cover
            logger.debug("Не удалось плавно проскроллить к элементу", extra={"error": str(exc)})

    def _wait(self, delay: Any) -> None:
        if hasattr(delay, "min_sec") and hasattr(delay, "max_sec"):
            jitter_sleep(delay.min_sec, delay.max_sec)
        else:
            jitter_sleep(self.config.action_delay.min_sec, self.config.action_delay.max_sec)


def _decrease_remaining(current: int | None, used: int) -> int | None:
    if current is None:
        return None
    remaining = max(0, current - used)
    return remaining
