from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from app.config.errors import ConfigLoaderError
from app.config.models import (
    DedupeConfig,
    DelayConfig,
    GlobalConfig,
    GlobalStopConfig,
    NetworkConfig,
    RetryPolicy,
    RuntimeConfig,
    SheetConfig,
    StateConfig,
)


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
    )

    network = NetworkConfig(
        user_agents=_list_required("NETWORK_USER_AGENTS"),
        proxy_pool=_list("NETWORK_PROXY_POOL"),
        request_timeout_sec=_float("NETWORK_REQUEST_TIMEOUT_SEC", default=30.0),
        retry=RetryPolicy(
            max_attempts=_int("NETWORK_RETRY_MAX_ATTEMPTS", default=3),
            backoff_sec=_float_list(
                "NETWORK_RETRY_BACKOFF_SEC",
                default=[2.0, 5.0, 10.0],
            ),
        ),
        browser_storage_state_path=_path("NETWORK_BROWSER_STORAGE_STATE_PATH"),
    )

    dedupe = DedupeConfig(
        strip_params_blacklist=_list("DEDUPE_STRIP_PARAMS_BLACKLIST"),
    )

    state = StateConfig(
        driver=os.getenv("STATE_DRIVER", "sqlite"),  # type: ignore[arg-type]
        database=Path(
            os.getenv("STATE_DATABASE_PATH", "/var/app/state/runtime.db")
        ),
    )

    return GlobalConfig(
        sheet=sheet,
        runtime=runtime,
        network=network,
        dedupe=dedupe,
        state=state,
    )


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


def _path(name: str) -> Path | None:
    value = os.getenv(name)
    if not value:
        return None
    return Path(value)
