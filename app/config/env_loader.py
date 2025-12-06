from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from app.config.errors import ConfigLoaderError
from app.config.models import (
    BehaviorMouseConfig,
    BehaviorNavigationConfig,
    BehaviorScrollConfig,
    DedupeConfig,
    DelayConfig,
    GlobalConfig,
    GlobalStopConfig,
    HumanBehaviorConfig,
    NetworkConfig,
    RetryPolicy,
    RuntimeConfig,
    SheetConfig,
    StateConfig,
)
from app.config.runtime_paths import resolve_optional_path, resolve_path


def load_global_config_from_env() -> GlobalConfig:
    """Строит глобальную конфигурацию на основе переменных окружения."""
    sheet = SheetConfig(
        spreadsheet_id=_require("SHEET_SPREADSHEET_ID"),
        write_batch_size=_int("SHEET_WRITE_BATCH_SIZE", default=200),
        sheet_state_tab=os.getenv("SHEET_STATE_TAB", "_state"),
        sheet_runs_tab=os.getenv("SHEET_RUNS_TAB", "_runs"),
    )

    runtime = RuntimeConfig(
        max_concurrency_per_site=_int("RUNTIME_MAX_CONCURRENCY_PER_SITE", default=1),
        global_stop=GlobalStopConfig(
            stop_after_products=_int("RUNTIME_STOP_AFTER_PRODUCTS"),
            stop_after_minutes=_int("RUNTIME_STOP_AFTER_MINUTES"),
        ),
        page_delay=_delay_from_env(
            prefix="RUNTIME_PAGE_DELAY",
            default_min=5.0,
            default_max=8.0,
        ),
        product_delay=_delay_from_env(
            prefix="RUNTIME_PRODUCT_DELAY",
            default_min=8.0,
            default_max=12.0,
        ),
        behavior=_behavior_from_env(),
        product_fetch_engine=_product_fetch_engine(),
    )

    headless_flag = _bool("NETWORK_BROWSER_HEADLESS", default=True)
    if headless_flag is None:
        headless_flag = True
    revive_minutes = _float("NETWORK_PROXY_REVIVE_AFTER_MINUTES", default=30.0)
    if revive_minutes is None:
        revive_minutes = 30.0
    network = NetworkConfig(
        user_agents=_list_required("NETWORK_USER_AGENTS"),
        proxy_pool=_list("NETWORK_PROXY_POOL"),
        proxy_allow_direct=_bool("NETWORK_PROXY_ALLOW_DIRECT", default=False) or False,
        proxy_revive_after_sec=max(0.0, revive_minutes * 60.0),
        request_timeout_sec=_float("NETWORK_REQUEST_TIMEOUT_SEC", default=30.0),
        retry=RetryPolicy(
            max_attempts=_int("NETWORK_RETRY_MAX_ATTEMPTS", default=3),
            backoff_sec=_float_list(
                "NETWORK_RETRY_BACKOFF_SEC",
                default=[2.0, 5.0, 10.0],
            ),
        ),
        browser_storage_state_path=resolve_optional_path(
            "NETWORK_BROWSER_STORAGE_STATE_PATH",
            local_default="secrets/auth.json",
            docker_default="/secrets/auth.json",
            require_exists=True,
        ),
        accept_language=os.getenv("NETWORK_ACCEPT_LANGUAGE"),
        browser_headless=headless_flag,
        browser_preview_delay_sec=_float("NETWORK_BROWSER_PREVIEW_DELAY_SEC", default=0.0)
        or 0.0,
        browser_preview_before_behavior_sec=_float(
            "NETWORK_BROWSER_PREVIEW_BEFORE_BEHAVIOR_SEC",
            default=0.0,
        )
        or 0.0,
        browser_extra_page_preview_sec=_float(
            "NETWORK_BROWSER_EXTRA_PAGE_PREVIEW_SEC",
            default=0.0,
        )
        or 0.0,
        browser_slow_mo_ms=_int("NETWORK_BROWSER_SLOW_MO_MS", default=0) or 0,
        bad_proxy_log_path=resolve_optional_path(
            "NETWORK_BAD_PROXY_LOG_PATH",
            local_default="logs/bad_proxies.log",
            docker_default="/var/log/parser/bad_proxies.log",
        ),
    )

    dedupe = DedupeConfig(
        strip_params_blacklist=_list("DEDUPE_STRIP_PARAMS_BLACKLIST"),
    )

    state = StateConfig(
        driver=os.getenv("STATE_DRIVER", "sqlite"),  # type: ignore[arg-type]
        database=resolve_path(
            "STATE_DATABASE_PATH",
            local_default="state/runtime.db",
            docker_default="/var/app/state/runtime.db",
        ),
    )

    return GlobalConfig(
        sheet=sheet,
        runtime=runtime,
        network=network,
        dedupe=dedupe,
        state=state,
    )


def _behavior_from_env() -> HumanBehaviorConfig:
    enabled = _bool("BEHAVIOR_ENABLED", default=False) or False
    debug = _bool("BEHAVIOR_DEBUG", default=False) or False
    action_delay = _delay_from_env(
        prefix="BEHAVIOR_ACTION_DELAY",
        default_min=0.3,
        default_max=0.9,
    )
    scroll_probability = _float("BEHAVIOR_SCROLL_PROBABILITY")
    if scroll_probability is None:
        scroll_probability = 0.7
    scroll_skip = _float("BEHAVIOR_SCROLL_SKIP_PROBABILITY")
    if scroll_skip is None:
        scroll_skip = 0.2
    scroll_min_depth = _int("BEHAVIOR_SCROLL_MIN_DEPTH")
    if scroll_min_depth is None:
        scroll_min_depth = 25
    scroll_max_depth = _int("BEHAVIOR_SCROLL_MAX_DEPTH")
    if scroll_max_depth is None:
        scroll_max_depth = 85
    scroll_min_steps = _int("BEHAVIOR_SCROLL_MIN_STEPS")
    if scroll_min_steps is None:
        scroll_min_steps = 2
    scroll_max_steps = _int("BEHAVIOR_SCROLL_MAX_STEPS")
    if scroll_max_steps is None:
        scroll_max_steps = 5
    scroll = BehaviorScrollConfig(
        probability=scroll_probability,
        skip_probability=scroll_skip,
        min_depth_percent=scroll_min_depth,
        max_depth_percent=scroll_max_depth,
        min_steps=scroll_min_steps,
        max_steps=scroll_max_steps,
        pause_between_steps=_delay_from_env(
            prefix="BEHAVIOR_SCROLL_STEP_DELAY",
            default_min=0.2,
            default_max=0.8,
        ),
    )
    move_min = _int("BEHAVIOR_MOUSE_MOVE_MIN")
    if move_min is None:
        move_min = 1
    move_max = _int("BEHAVIOR_MOUSE_MOVE_MAX")
    if move_max is None:
        move_max = 3
    hover_probability = _float("BEHAVIOR_MOUSE_HOVER_PROBABILITY")
    if hover_probability is None:
        hover_probability = 0.35
    mouse = BehaviorMouseConfig(
        move_count_min=move_min,
        move_count_max=move_max,
        hover_probability=hover_probability,
    )
    back_probability = _float("BEHAVIOR_NAV_BACK_PROBABILITY")
    if back_probability is None:
        back_probability = 0.25
    extra_probability = _float("BEHAVIOR_NAV_EXTRA_PRODUCTS_PROBABILITY")
    if extra_probability is None:
        extra_probability = 0.3
    extra_limit = _int("BEHAVIOR_NAV_EXTRA_PRODUCTS_LIMIT")
    if extra_limit is None:
        extra_limit = 2
    visit_root_probability = _float("BEHAVIOR_NAV_VISIT_ROOT_PROBABILITY")
    if visit_root_probability is None:
        visit_root_probability = 0.15
    max_chain = _int("BEHAVIOR_NAV_MAX_CHAIN")
    if max_chain is None:
        max_chain = 2
    navigation = BehaviorNavigationConfig(
        back_probability=back_probability,
        extra_products_probability=extra_probability,
        extra_products_limit=extra_limit,
        visit_root_probability=visit_root_probability,
        max_additional_chain=max_chain,
    )
    return HumanBehaviorConfig(
        enabled=enabled,
        debug=debug,
        action_delay=action_delay,
        scroll=scroll,
        mouse=mouse,
        navigation=navigation,
    )


def _product_fetch_engine() -> str:
    value = os.getenv("PRODUCT_FETCH_ENGINE", "http").strip().lower()
    if value not in {"http", "browser"}:
        raise ConfigLoaderError("PRODUCT_FETCH_ENGINE должен быть 'http' или 'browser'")
    return value


def _delay_from_env(*, prefix: str, default_min: float, default_max: float) -> DelayConfig:
    min_value = _float(f"{prefix}_MIN_SEC", default=default_min)
    max_value = _float(f"{prefix}_MAX_SEC", default=default_max)
    if min_value is None:
        min_value = default_min
    if max_value is None:
        max_value = default_max
    return DelayConfig(min_sec=min_value, max_sec=max_value)


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ConfigLoaderError(f"Переменная окружения {name} не задана")
    return value


def _int(name: str, default: int | None = None) -> int | None:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigLoaderError(f"Ожидается целое число в {name}") from exc


def _float(name: str, default: float | None = None) -> float | None:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ConfigLoaderError(f"Ожидается число (float) в {name}") from exc


def _list(name: str, default: Iterable[str] | None = None) -> list[str]:
    value = os.getenv(name)
    if value is None:
        return list(default) if default is not None else []
    tokens = [
        token.strip()
        for token in value.replace("\n", ",").split(",")
        if token.strip()
    ]
    return tokens


def _list_required(name: str) -> list[str]:
    values = _list(name)
    if not values:
        raise ConfigLoaderError(f"Переменная {name} должна содержать минимум одно значение")
    return values


def _float_list(name: str, default: Iterable[float] | None = None) -> list[float]:
    value = os.getenv(name)
    if value is None:
        return list(default) if default is not None else []
    tokens = [
        token.strip()
        for token in value.replace("\n", ",").split(",")
        if token.strip()
    ]
    try:
        return [float(token) for token in tokens]
    except ValueError as exc:
        raise ConfigLoaderError(f"Элементы {name} должны быть числами") from exc


def _bool(name: str, default: bool | None = None) -> bool | None:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}
