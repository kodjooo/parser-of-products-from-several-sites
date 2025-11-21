from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from app.config.loader import ConfigLoaderError, iter_site_configs, load_global_config


def _base_global_payload() -> dict:
    return {
        "sheet": {
            "spreadsheet_id": "TEST_SHEET",
            "write_batch_size": 200,
            "sheet_state_tab": "_state",
            "sheet_runs_tab": "_runs",
        },
        "runtime": {"max_concurrency_per_site": 2, "global_stop": {}},
        "network": {
            "user_agents": ["agent-1"],
            "proxy_pool": [],
            "request_timeout_sec": 10,
            "retry": {"max_attempts": 3, "backoff_sec": [1, 2, 3]},
        },
        "dedupe": {"strip_params_blacklist": ["utm_*"]},
        "state": {"driver": "sqlite", "database": "/tmp/state.db"},
    }


def _site_payload(name: str) -> dict:
    return {
        "site": {
            "name": name,
            "domain": f"{name}.example.com",
            "engine": "http",
        },
        "selectors": {"product_link_selector": ".product-card a"},
        "pagination": {"mode": "numbered_pages", "param_name": "page", "max_pages": 3},
        "limits": {"max_products": 10},
        "category_urls": ["https://example.com/catalog/"],
    }


def test_load_global_config_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "global.yml"
    config_path.write_text(yaml.safe_dump(_base_global_payload()), encoding="utf-8")

    config = load_global_config(config_path)
    assert config.sheet.spreadsheet_id == "TEST_SHEET"
    assert config.network.user_agents == ["agent-1"]


def test_load_global_config_json(tmp_path: Path) -> None:
    config_path = tmp_path / "global.json"
    config_path.write_text(json.dumps(_base_global_payload()), encoding="utf-8")

    config = load_global_config(config_path)
    assert config.runtime.max_concurrency_per_site == 2


def test_load_global_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHEET_SPREADSHEET_ID", "ENV_SHEET")
    monkeypatch.setenv("SHEET_WRITE_BATCH_SIZE", "250")
    monkeypatch.setenv("SHEET_STATE_TAB", "_state")
    monkeypatch.setenv("SHEET_RUNS_TAB", "_runs")
    monkeypatch.setenv("RUNTIME_MAX_CONCURRENCY_PER_SITE", "3")
    monkeypatch.setenv("RUNTIME_PAGE_DELAY_MIN_SEC", "6")
    monkeypatch.setenv("RUNTIME_PAGE_DELAY_MAX_SEC", "9")
    monkeypatch.setenv("RUNTIME_PRODUCT_DELAY_MIN_SEC", "10")
    monkeypatch.setenv("RUNTIME_PRODUCT_DELAY_MAX_SEC", "14")
    monkeypatch.setenv("NETWORK_USER_AGENTS", "env-agent-1,env-agent-2")
    monkeypatch.setenv("NETWORK_REQUEST_TIMEOUT_SEC", "42")
    monkeypatch.setenv("NETWORK_RETRY_MAX_ATTEMPTS", "4")
    monkeypatch.setenv("NETWORK_RETRY_BACKOFF_SEC", "1,2,3")
    monkeypatch.setenv("NETWORK_BROWSER_STORAGE_STATE_PATH", "/tmp/auth.json")
    monkeypatch.setenv("STATE_DATABASE_PATH", "/tmp/env-state.db")

    config = load_global_config(None)
    assert config.sheet.spreadsheet_id == "ENV_SHEET"
    assert config.runtime.max_concurrency_per_site == 3
    assert config.network.retry.max_attempts == 4
    assert config.runtime.page_delay.min_sec == 6
    assert config.runtime.page_delay.max_sec == 9
    assert config.runtime.product_delay.min_sec == 10
    assert config.runtime.product_delay.max_sec == 14
    assert str(config.network.browser_storage_state_path) == "/tmp/auth.json"


def test_iter_site_configs_supports_multiple_files(tmp_path: Path) -> None:
    (tmp_path / "first.yml").write_text(
        yaml.safe_dump(_site_payload("first")), encoding="utf-8"
    )
    (tmp_path / "second.json").write_text(
        json.dumps(_site_payload("second")), encoding="utf-8"
    )

    sites = list(iter_site_configs(tmp_path))
    assert {site.name for site in sites} == {"first", "second"}


def test_invalid_site_config_raises(tmp_path: Path) -> None:
    broken = tmp_path / "broken.yml"
    broken.write_text(yaml.safe_dump({"site": {"name": "broken"}}), encoding="utf-8")

    with pytest.raises(ConfigLoaderError):
        list(iter_site_configs(tmp_path))
