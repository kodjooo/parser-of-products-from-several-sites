from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config.models import (
    BehaviorMouseConfig,
    BehaviorNavigationConfig,
    BehaviorScrollConfig,
    DelayConfig,
    HumanBehaviorConfig,
)
from app.crawler.behavior import BehaviorContext, HumanBehaviorController


class _StubMouse:
    def __init__(self) -> None:
        self.moves: list[tuple[int, int, int]] = []

    def move(self, x: int, y: int, steps: int = 1) -> None:
        self.moves.append((x, y, steps))


class _StubExtraPage:
    def __init__(self) -> None:
        self.opened: list[str] = []
        self.closed = False

    def set_default_timeout(self, _: float) -> None:
        pass

    def goto(self, url: str, wait_until: str = "domcontentloaded") -> None:  # noqa: ARG002
        self.opened.append(url)

    def close(self) -> None:
        self.closed = True


class _StubContext:
    def __init__(self) -> None:
        self.created_pages: list[_StubExtraPage] = []

    def new_page(self) -> _StubExtraPage:
        page = _StubExtraPage()
        self.created_pages.append(page)
        return page


@dataclass
class _StubElement:
    href: str

    def get_attribute(self, name: str) -> str | None:
        if name == "href":
            return self.href
        return None

    def hover(self, timeout: int = 1000) -> None:  # noqa: ARG002
        return

    def bounding_box(self) -> dict[str, float]:
        return {"x": 100.0, "y": 200.0, "width": 60.0, "height": 20.0}


class _StubPage:
    def __init__(self) -> None:
        self.mouse = _StubMouse()
        self.viewport_size = {"width": 1200, "height": 900}
        self._context = _StubContext()
        self._nodes: dict[str, list[Any]] = {}

    def evaluate(self, script: str, *args: Any) -> None:  # noqa: ARG002
        return

    def wait_for_timeout(self, ms: int) -> None:  # noqa: ARG002
        return

    def query_selector_all(self, selector: str) -> list[Any]:
        return self._nodes.get(selector, [])

    def set_nodes(self, selector: str, nodes: list[Any]) -> None:
        self._nodes[selector] = nodes

    @property
    def context(self) -> _StubContext:  # pragma: no cover - используется в тестах
        return self._context


def _zero_delay() -> DelayConfig:
    return DelayConfig(min_sec=0.0, max_sec=0.0)


def test_behavior_controller_scroll_and_mouse() -> None:
    config = HumanBehaviorConfig(
        enabled=True,
        debug=False,
        action_delay=_zero_delay(),
        scroll=BehaviorScrollConfig(
            probability=1.0,
            skip_probability=0.0,
            min_depth_percent=10,
            max_depth_percent=10,
            min_steps=1,
            max_steps=1,
            pause_between_steps=_zero_delay(),
        ),
        mouse=BehaviorMouseConfig(
            move_count_min=1,
            move_count_max=1,
            hover_probability=0.0,
            hover_selectors=[],
        ),
        navigation=BehaviorNavigationConfig(
            back_probability=0.0,
            extra_products_probability=0.0,
            extra_products_limit=0,
            visit_root_probability=0.0,
            max_additional_chain=0,
        ),
    )
    controller = HumanBehaviorController(config, default_timeout_sec=5.0)
    page = _StubPage()

    result = controller.apply(page, context=None, meta={"url": "https://demo.example"})

    assert any(action.startswith("scroll") for action in result.actions)
    assert any(action.startswith("mouse_move") for action in result.actions)


def test_behavior_controller_additional_products() -> None:
    config = HumanBehaviorConfig(
        enabled=True,
        debug=True,
        action_delay=_zero_delay(),
        scroll=BehaviorScrollConfig(
            probability=0.0,
            skip_probability=1.0,
            min_depth_percent=10,
            max_depth_percent=10,
            min_steps=1,
            max_steps=1,
            pause_between_steps=_zero_delay(),
        ),
        mouse=BehaviorMouseConfig(
            move_count_min=0,
            move_count_max=0,
            hover_probability=0.0,
            hover_selectors=[],
        ),
        navigation=BehaviorNavigationConfig(
            back_probability=0.0,
            extra_products_probability=1.0,
            extra_products_limit=2,
            visit_root_probability=0.0,
            max_additional_chain=1,
        ),
    )
    controller = HumanBehaviorController(config, default_timeout_sec=5.0)
    page = _StubPage()
    page.set_nodes(
        ".product a",
        [
            _StubElement("/p/1"),
            _StubElement("/p/2"),
        ],
    )
    context = BehaviorContext(
        product_link_selector=".product a",
        category_url="https://demo.example/catalog/",
        base_url="https://demo.example",
        root_url="https://demo.example",
    )

    result = controller.apply(page, context=context, meta={"url": "https://demo.example/catalog/"})

    assert any(action.startswith("extra_product") for action in result.actions)
    assert page.context.created_pages
    opened = page.context.created_pages[0].opened[0]
    assert opened.startswith("https://demo.example/p/")
