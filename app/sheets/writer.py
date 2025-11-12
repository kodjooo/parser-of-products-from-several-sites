from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from app.config.models import SiteConfig
from app.crawler.models import ProductRecord, SiteCrawlResult
from app.logger import get_logger
from app.runtime import RuntimeContext
from app.sheets.client import GoogleSheetsClient

logger = get_logger(__name__)


class SheetsWriter:
    """Отвечает за подготовку данных и запись в Google Sheets."""

    def __init__(
        self,
        context: RuntimeContext,
        client: GoogleSheetsClient | None = None,
    ):
        self.context = context
        self.client = client or GoogleSheetsClient(
            spreadsheet_id=context.config.sheet.spreadsheet_id,
            client_secret_path=self._env_path("GOOGLE_OAUTH_CLIENT_SECRET_PATH"),
            token_path=self._env_path("GOOGLE_OAUTH_TOKEN_PATH"),
            scopes=self._env_scopes("GOOGLE_OAUTH_SCOPES"),
            batch_size=context.config.sheet.write_batch_size,
        )
        self.state_tab = context.config.sheet.sheet_state_tab
        self.runs_tab = context.config.sheet.sheet_runs_tab
        self.client.ensure_aux_tabs(self.state_tab, self.runs_tab)
        self._prepared_tabs: set[str] = set()
        self._existing_cache: dict[str, set[str]] = {}

    def _env_path(self, key: str) -> Path:
        value = os.getenv(key)
        if not value:
            raise RuntimeError(f"Не задана переменная окружения {key}")
        return Path(value)

    def _env_scopes(self, key: str) -> list[str]:
        value = os.getenv(key)
        if not value:
            raise RuntimeError(f"Не задана переменная окружения {key}")
        return [scope.strip() for scope in value.split(",") if scope.strip()]

    def prepare_site(self, site: SiteConfig) -> None:
        tab_name = site.domain
        if tab_name in self._prepared_tabs:
            return
        self.client.ensure_tabs([tab_name])
        self._existing_cache[tab_name] = self.client.get_existing_product_urls(tab_name)
        self._prepared_tabs.add(tab_name)

    def append_site_records(self, site: SiteConfig, records: list[ProductRecord]) -> None:
        if not records:
            return
        tab_name = site.domain
        self.prepare_site(site)
        existing = self._existing_cache.get(tab_name, set())
        rows: list[list[str]] = []
        for record in records:
            if record.product_url in existing:
                continue
            existing.add(record.product_url)
            rows.append(self._record_to_row(record))
        if not rows:
            return
        logger.info(
            "Подготовлено строк для записи",
            extra={"sheet": tab_name, "rows": len(rows)},
        )
        self.client.append_rows(tab_name, rows)

    def finalize(self, results: list[SiteCrawlResult]) -> None:
        if not results:
            logger.warning("Нет данных для записи в Google Sheets")
            return
        self._write_runs(results)
        self._sync_state_sheet()

    def _record_to_row(self, record: ProductRecord) -> list[str]:
        metadata_pairs = dict(record.metadata) if record.metadata else {}
        if record.image_url:
            metadata_pairs.setdefault("image_url", record.image_url)
        metadata_str = ""
        if metadata_pairs:
            metadata_str = ";".join(f"{k}={v}" for k, v in metadata_pairs.items())
        return [
            record.source_site,
            record.category_url,
            record.product_url,
            record.content_text or "",
            record.discovered_at.isoformat(),
            record.run_id,
            record.status,
            record.note or "",
            record.product_id_hash or "",
            str(record.page_num or ""),
            metadata_str,
            record.image_path or "",
        ]

    def _write_runs(self, results: list[SiteCrawlResult]) -> None:
        finished = datetime.now(timezone.utc).isoformat()
        rows: list[list[str]] = []
        for result in results:
            total_written = sum(metric.total_written for metric in result.metrics)
            total_found = sum(metric.total_found for metric in result.metrics)
            total_failed = sum(metric.total_failed for metric in result.metrics)
            rows.append(
                [
                    self.context.run_id,
                    result.sheet_tab,
                    self.context.started_at.isoformat(),
                    finished,
                    str(total_found),
                    str(total_written),
                    str(total_failed),
                ]
            )
        self.client.append_rows(self.runs_tab, rows)

    def _sync_state_sheet(self) -> None:
        rows = []
        for state in self.context.state_store.iter_all():
            rows.append(
                [
                    state.site_name,
                    state.category_url,
                    str(state.last_page or ""),
                    str(state.last_offset or ""),
                    str(state.last_product_count or ""),
                    state.last_run_ts.isoformat() if state.last_run_ts else "",
                ]
            )
        self.client.replace_state_rows(self.state_tab, rows)
