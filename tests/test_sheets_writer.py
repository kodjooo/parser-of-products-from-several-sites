from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import json
import os
import pytest

from app.config.models import GlobalConfig, SiteConfig
from app.crawler.models import CategoryMetrics, ProductRecord, SiteCrawlResult
from app.runtime import RuntimeContext
from app.sheets.writer import SheetsWriter
from app.state.storage import CategoryState
from app.sheets.client import GoogleSheetsClient


class FakeSheetsClient:
    def __init__(self) -> None:
        self.tabs: set[str] = set()
        self.appended: dict[str, list[list[str]]] = {}
        self.existing: dict[str, set[str]] = {"demo.example": {"https://demo/p/1"}}
        self.state_rows: list[list[str]] = []
        self.headers: dict[str, list[str]] = {}

    def ensure_tabs(self, tab_names):
        self.tabs.update(tab_names)

    def ensure_aux_tabs(self, *tab_names):
        self.tabs.update(tab_names)

    def ensure_header(self, tab_name: str, header: list[str]) -> None:
        self.headers[tab_name] = header

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


class FakeImageSaver:
    def __init__(self) -> None:
        self.saved: list[str] = []

    def save(self, url: str, title: str | None, fallback_id: str) -> str | None:
        path = f"/tmp/{len(self.saved)+1}.jpg"
        self.saved.append(path)
        return path

    def close(self) -> None:
        return None


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


def _service_account_json(tmp_path: Path) -> Path:
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
    return secret_path


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
        image_url="https://cdn/images/product.jpg",
        metadata={"color": "red"},
        image_name_hint="Demo Product",
        category="catalog",
        name_en="Demo EN",
        name_ru="Демо",
        price_without_discount="1000",
        price_with_discount="900",
        note="Первая запись",
        status="Не обработано",
    )
    record_dup = ProductRecord(
        source_site="demo.example",
        category_url="https://demo.example/catalog/",
        product_url="https://demo/p/1",
        run_id="run-123",
        category="catalog",
    )
    result = SiteCrawlResult(
        site_name="demo",
        sheet_tab="demo.example",
        records=[record_new, record_dup],
        metrics=[CategoryMetrics(site_name="demo", category_url="https://demo.example/catalog/")],
    )

    fake_client = FakeSheetsClient()
    secret_path = _service_account_json(tmp_path)
    os.environ["GOOGLE_OAUTH_CLIENT_SECRET_PATH"] = str(secret_path)
    os.environ["GOOGLE_OAUTH_TOKEN_PATH"] = str(tmp_path / "token.json")
    os.environ["GOOGLE_OAUTH_SCOPES"] = "https://www.googleapis.com/auth/spreadsheets"
    image_saver = FakeImageSaver()
    writer = SheetsWriter(context, client=fake_client, image_saver=image_saver)  # type: ignore[arg-type]
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
    assert appended_row[1] == "catalog"
    assert appended_row[2] == "https://demo.example/catalog/"
    assert appended_row[3] == "https://demo/p/2"
    assert appended_row[4] == "Описание товара"
    assert appended_row[10] == "1.jpg"
    assert appended_row[11] == "Demo EN"
    assert appended_row[12] == "Демо"
    assert appended_row[13] == "1000"
    assert appended_row[14] == "900"
    assert appended_row[15] == "Не обработано"
    assert appended_row[16] == "Первая запись"
    assert "image_url=https://cdn/images/product.jpg" in appended_row[9]
    assert fake_client.state_rows[0][0] == "demo"
    assert fake_client.headers["demo.example"] == SheetsWriter.SITE_HEADER


def test_append_with_retry_retries_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _global_config(tmp_path)
    context = RuntimeContext(
        run_id="run-xyz",
        started_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        config=config,
        sites=[],
        state_store=StateStub(),  # type: ignore[arg-type]
        dry_run=False,
        resume=True,
        assets_dir=tmp_path / "assets",
    )
    secret_path = _service_account_json(tmp_path)
    os.environ["GOOGLE_OAUTH_CLIENT_SECRET_PATH"] = str(secret_path)
    os.environ["GOOGLE_OAUTH_TOKEN_PATH"] = str(tmp_path / "token.json")
    os.environ["GOOGLE_OAUTH_SCOPES"] = "https://www.googleapis.com/auth/spreadsheets"

    client = FakeSheetsClient()
    call_count = {"value": 0}
    original_append = client.append_rows

    def flaky_append(tab_name, rows):
        call_count["value"] += 1
        if call_count["value"] == 1:
            raise RuntimeError("boom")
        return original_append(tab_name, rows)

    client.append_rows = flaky_append  # type: ignore[assignment]

    monkeypatch.setattr("time.sleep", lambda _: None)

    writer = SheetsWriter(context, client=client, image_saver=FakeImageSaver())  # type: ignore[arg-type]
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
    record = ProductRecord(
        source_site="demo.example",
        category_url="https://demo.example/catalog/",
        product_url="https://demo/p/new",
        run_id="run-xyz",
    )
    writer.append_site_records_with_retry(site, [record], delay_sec=0.01)
    assert call_count["value"] == 2
    assert client.appended["demo.example"][0][3] == "https://demo/p/new"


class DummySheetsAPI:
    def __init__(self, response: dict):
        self._response = response
        self.last_range: str | None = None

    def values(self):
        return self

    def get(self, spreadsheetId: str, range: str):
        self.last_range = range
        return self

    def execute(self):
        return self._response

    # unused stubs
    def append(self, **kwargs):
        return self

    def update(self, **kwargs):
        return self


class DummyService:
    def __init__(self, response: dict):
        self.api = DummySheetsAPI(response)

    def spreadsheets(self):
        return self.api


def test_google_client_reads_product_column(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secret_path = tmp_path / "secret.json"
    secret_path.write_text("{}", encoding="utf-8")
    dummy_service = DummyService({"values": [["product_url"], ["https://demo/p/1"]]})
    monkeypatch.setattr("app.sheets.client.build", lambda *args, **kwargs: dummy_service)
    monkeypatch.setattr("app.sheets.client.GoogleSheetsClient._authorize", lambda self: None)
    monkeypatch.setattr("app.sheets.client.GoogleSheetsClient._detect_client_type", lambda self: "service_account")

    client = GoogleSheetsClient(
        spreadsheet_id="TEST",
        client_secret_path=secret_path,
        token_path=tmp_path / "token.json",
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
        batch_size=200,
    )
    urls = client.get_existing_product_urls("demo.example")
    assert dummy_service.api.last_range == "demo.example!D:D"
    assert urls == {"https://demo/p/1"}
