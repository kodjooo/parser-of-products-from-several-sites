from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import json
import os

from app.config.models import GlobalConfig, SiteConfig
from app.crawler.models import CategoryMetrics, ProductRecord, SiteCrawlResult
from app.runtime import RuntimeContext
from app.sheets.writer import SheetsWriter
from app.state.storage import CategoryState


class FakeSheetsClient:
    def __init__(self) -> None:
        self.tabs: set[str] = set()
        self.appended: dict[str, list[list[str]]] = {}
        self.existing: dict[str, set[str]] = {"demo.example": {"https://demo/p/1"}}
        self.state_rows: list[list[str]] = []

    def ensure_tabs(self, tab_names):
        self.tabs.update(tab_names)

    def ensure_aux_tabs(self, *tab_names):
        self.tabs.update(tab_names)

    def get_existing_product_urls(self, tab_name: str) -> set[str]:
        return set(self.existing.get(tab_name, set()))

    def append_rows(self, tab_name: str, rows: list[list[str]]) -> None:
        self.appended.setdefault(tab_name, []).extend(rows)

    def replace_state_rows(self, tab_name: str, rows: list[list[str]]) -> None:
        self.state_rows = rows


class StateStub:
    def __init__(self):
        self._items = [
            CategoryState(
                site_name="demo",
                category_url="https://demo.example/catalog/",
                last_page=2,
                last_offset=None,
                last_product_count=3,
                last_run_ts=datetime(2024, 1, 1, tzinfo=timezone.utc),
            )
        ]

    def iter_all(self):
        return iter(self._items)


def _global_config(tmp_path: Path) -> GlobalConfig:
    return GlobalConfig.model_validate(
        {
            "sheet": {
                "spreadsheet_id": "TEST",
                "write_batch_size": 200,
                "sheet_state_tab": "_state",
                "sheet_runs_tab": "_runs",
            },
            "runtime": {"max_concurrency_per_site": 1, "global_stop": {}},
            "network": {
                "user_agents": ["agent"],
                "proxy_pool": [],
                "request_timeout_sec": 5,
                "retry": {"max_attempts": 1, "backoff_sec": [1]},
            },
            "dedupe": {"strip_params_blacklist": []},
            "state": {"driver": "sqlite", "database": str(tmp_path / "state.db")},
        }
    )


def test_sheets_writer_deduplicates_and_exports_state(tmp_path: Path) -> None:
    config = _global_config(tmp_path)
    context = RuntimeContext(
        run_id="run-123",
        started_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        config=config,
        sites=[],
        state_store=StateStub(),  # type: ignore[arg-type]
        dry_run=False,
        resume=True,
        assets_dir=tmp_path / "assets",
    )
    record_new = ProductRecord(
        source_site="demo.example",
        category_url="https://demo.example/catalog/",
        product_url="https://demo/p/2",
        run_id="run-123",
        content_text="Описание товара",
        image_path="/app/assets/images/product.jpg",
        image_url="https://cdn/images/product.jpg",
        metadata={"color": "red"},
    )
    record_dup = ProductRecord(
        source_site="demo.example",
        category_url="https://demo.example/catalog/",
        product_url="https://demo/p/1",
        run_id="run-123",
    )
    result = SiteCrawlResult(
        site_name="demo",
        sheet_tab="demo.example",
        records=[record_new, record_dup],
        metrics=[CategoryMetrics(site_name="demo", category_url="https://demo.example/catalog/")],
    )

    fake_client = FakeSheetsClient()
    service_json = {
        "type": "service_account",
        "project_id": "demo",
        "private_key_id": "dummy",
        "private_key": "-----BEGIN PRIVATE KEY-----\nABCDEF\n-----END PRIVATE KEY-----\n",
        "client_email": "demo@demo",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    secret_path = tmp_path / "secret.json"
    secret_path.write_text(json.dumps(service_json), encoding="utf-8")
    os.environ["GOOGLE_OAUTH_CLIENT_SECRET_PATH"] = str(secret_path)
    os.environ["GOOGLE_OAUTH_TOKEN_PATH"] = str(tmp_path / "token.json")
    os.environ["GOOGLE_OAUTH_SCOPES"] = "https://www.googleapis.com/auth/spreadsheets"
    writer = SheetsWriter(context, client=fake_client)  # type: ignore[arg-type]
    site = SiteConfig.model_validate(
        {
            "site": {"name": "demo", "domain": "demo.example"},
            "selectors": {"product_link_selector": ".card a"},
            "pagination": {"mode": "numbered_pages"},
            "limits": {},
            "category_urls": ["https://demo.example/catalog/"],
        }
    )
    writer.prepare_site(site)
    writer.append_site_records(site, [record_new, record_dup])
    writer.finalize([result])

    appended_row = fake_client.appended["demo.example"][0]
    assert appended_row[2] == "https://demo/p/2"
    assert appended_row[3] == "Описание товара"
    assert appended_row[-1] == "/app/assets/images/product.jpg"
    assert "image_url=https://cdn/images/product.jpg" in appended_row[-2]
    assert fake_client.state_rows[0][0] == "demo"
