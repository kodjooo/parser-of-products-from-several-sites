from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    Field,
    HttpUrl,
    PositiveInt,
    RootModel,
    field_validator,
    model_validator,
)


def _default_retry_backoff() -> list[float]:
    return [2.0, 5.0, 10.0]


class RetryPolicy(BaseModel):
    """Настройки повторов HTTP/Google API."""

    max_attempts: PositiveInt = Field(default=3, le=10)
    backoff_sec: list[float] = Field(default_factory=_default_retry_backoff)


class NetworkConfig(BaseModel):
    """Глобальные сетевые настройки."""

    user_agents: list[str]
    proxy_pool: list[str] = Field(default_factory=list)
    proxy_allow_direct: bool = False
    request_timeout_sec: float = Field(default=30, gt=0)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    browser_storage_state_path: Path | None = None
    accept_language: str | None = None
    browser_headless: bool = True
    browser_preview_delay_sec: float = Field(default=0.0, ge=0.0)
    browser_preview_before_behavior_sec: float = Field(default=0.0, ge=0.0)
    browser_extra_page_preview_sec: float = Field(default=0.0, ge=0.0)
    browser_slow_mo_ms: int = Field(default=0, ge=0)

    @field_validator("user_agents")
    @classmethod
    def _ensure_user_agents(cls, value: list[str]) -> list[str]:
        if not value:
            msg = "Нужно указать минимум один User-Agent"
            raise ValueError(msg)
        return value


class SheetConfig(BaseModel):
    """Настройки Google Sheets."""

    spreadsheet_id: str
    write_batch_size: PositiveInt = Field(default=200, le=500)
    sheet_state_tab: str = Field(default="_state")
    sheet_runs_tab: str = Field(default="_runs")


class GlobalStopConfig(BaseModel):
    stop_after_products: int | None = None
    stop_after_minutes: int | None = None


class DelayConfig(BaseModel):
    min_sec: float = Field(default=0.0, ge=0)
    max_sec: float = Field(default=0.0, ge=0)

    @model_validator(mode="after")
    def _ensure_bounds(self) -> "DelayConfig":
        if self.max_sec < self.min_sec:
            msg = "max_sec должен быть не меньше min_sec"
            raise ValueError(msg)
        return self


def _default_page_delay() -> DelayConfig:
    return DelayConfig(min_sec=5.0, max_sec=8.0)


def _default_product_delay() -> DelayConfig:
    return DelayConfig(min_sec=8.0, max_sec=12.0)


def _default_behavior_action_delay() -> DelayConfig:
    return DelayConfig(min_sec=0.3, max_sec=0.9)


class BehaviorScrollConfig(BaseModel):
    probability: float = Field(default=0.7, ge=0.0, le=1.0)
    skip_probability: float = Field(default=0.2, ge=0.0, le=1.0)
    min_depth_percent: int = Field(default=25, ge=1, le=100)
    max_depth_percent: int = Field(default=85, ge=1, le=100)
    min_steps: int = Field(default=2, ge=1)
    max_steps: int = Field(default=5, ge=1)
    pause_between_steps: DelayConfig = Field(
        default_factory=lambda: DelayConfig(min_sec=0.2, max_sec=0.8)
    )

    @model_validator(mode="after")
    def _ensure_bounds(self) -> "BehaviorScrollConfig":
        if self.max_depth_percent < self.min_depth_percent:
            msg = "max_depth_percent должен быть не меньше min_depth_percent"
            raise ValueError(msg)
        if self.max_steps < self.min_steps:
            msg = "max_steps должен быть не меньше min_steps"
            raise ValueError(msg)
        return self


class BehaviorMouseConfig(BaseModel):
    move_count_min: int = Field(default=1, ge=0)
    move_count_max: int = Field(default=3, ge=0)
    hover_probability: float = Field(default=0.35, ge=0.0, le=1.0)
    hover_selectors: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ensure_bounds(self) -> "BehaviorMouseConfig":
        if self.move_count_max < self.move_count_min:
            msg = "move_count_max должен быть не меньше move_count_min"
            raise ValueError(msg)
        return self


class BehaviorNavigationConfig(BaseModel):
    back_probability: float = Field(default=0.25, ge=0.0, le=1.0)
    extra_products_probability: float = Field(default=0.3, ge=0.0, le=1.0)
    extra_products_limit: int = Field(default=2, ge=0, le=5)
    visit_root_probability: float = Field(default=0.15, ge=0.0, le=1.0)
    max_additional_chain: int = Field(default=2, ge=0)


class HumanBehaviorConfig(BaseModel):
    enabled: bool = False
    debug: bool = False
    action_delay: DelayConfig = Field(default_factory=_default_behavior_action_delay)
    scroll: BehaviorScrollConfig = Field(default_factory=BehaviorScrollConfig)
    mouse: BehaviorMouseConfig = Field(default_factory=BehaviorMouseConfig)
    navigation: BehaviorNavigationConfig = Field(default_factory=BehaviorNavigationConfig)


class RuntimeConfig(BaseModel):
    """Общие лимиты выполнения."""

    max_concurrency_per_site: PositiveInt = Field(default=1, le=10)
    global_stop: GlobalStopConfig = Field(default_factory=GlobalStopConfig)
    page_delay: DelayConfig = Field(default_factory=_default_page_delay)
    product_delay: DelayConfig = Field(default_factory=_default_product_delay)
    behavior: HumanBehaviorConfig = Field(default_factory=HumanBehaviorConfig)
    product_fetch_engine: Literal["http", "browser"] = Field(default="http")


class DedupeConfig(BaseModel):
    """Правила нормализации ссылок."""

    strip_params_blacklist: list[str] = Field(default_factory=list)


class StateConfig(BaseModel):
    """Настройки хранения локального состояния."""

    driver: Literal["sqlite", "jsonl"] = Field(default="sqlite")
    database: Path = Field(default=Path("/var/app/state/runtime.db"))
    snapshots_dir: Path | None = None


class WaitCondition(BaseModel):
    type: Literal["selector", "delay"]
    value: str | float
    timeout_sec: float = Field(default=15, gt=0)


class StopCondition(BaseModel):
    type: Literal["missing_selector", "no_new_products", "custom"]
    value: str | None = None


class PaginationConfig(BaseModel):
    mode: Literal["numbered_pages", "next_button", "infinite_scroll"]
    param_name: str | None = None
    next_button_selector: str | None = None
    max_pages: int | None = Field(default=100, ge=1)
    max_scrolls: int | None = Field(default=100, ge=1)


SelectorValue = str | list[str] | None


class SelectorConfig(BaseModel):
    product_link_selector: str
    base_url: HttpUrl | None = None
    allowed_domains: list[str] = Field(default_factory=list)
    main_image_selector: str | None = None
    content_drop_after: list[str] = Field(
        default_factory=list,
        description=(
            "Селекторы элементов, после которых (включая их) текст товара нужно обрезать"
        ),
    )
    name_en_selector: str | None = None
    name_ru_selector: str | None = None
    price_without_discount_selector: str | None = None
    price_with_discount_selector: SelectorValue = None
    category_labels: dict[str, str] = Field(default_factory=dict)
    hover_targets: list[str] = Field(
        default_factory=list,
        description="Селекторы элементов, на которые нужно имитировать наведение курсора (per-site)",
    )
    product_hover_targets: list[str] | None = Field(
        default=None,
        description="Селекторы для hover на карточках товаров (если нужно отличать от категорий)",
    )


class SiteLimits(BaseModel):
    max_products: int | None = None
    max_pages: int | None = None
    max_scrolls: int | None = None


class SiteConfig(BaseModel):
    site: dict[str, Any]
    selectors: SelectorConfig
    pagination: PaginationConfig
    limits: SiteLimits = Field(default_factory=SiteLimits)
    wait_conditions: list[WaitCondition] = Field(default_factory=list)
    stop_conditions: list[StopCondition] = Field(default_factory=list)
    category_urls: list[HttpUrl]

    @field_validator("category_urls")
    @classmethod
    def _ensure_categories(cls, value: list[HttpUrl]) -> list[HttpUrl]:
        if not value:
            msg = "Для сайта нужно указать минимум один category_url"
            raise ValueError(msg)
        return value

    @property
    def name(self) -> str:
        return self.site["name"]

    @property
    def domain(self) -> str:
        return self.site["domain"]

    @property
    def engine(self) -> str:
        return self.site.get("engine", "http")

    @property
    def base_url(self) -> str | None:
        return self.site.get("base_url") or (
            str(self.selectors.base_url) if self.selectors.base_url else None
        )


class GlobalConfig(BaseModel):
    sheet: SheetConfig
    runtime: RuntimeConfig
    network: NetworkConfig
    dedupe: DedupeConfig = Field(default_factory=DedupeConfig)
    state: StateConfig = Field(default_factory=StateConfig)
