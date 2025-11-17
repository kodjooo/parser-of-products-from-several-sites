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
from app.media.image_saver import ImageSaver

logger = get_logger(__name__)


class SheetsWriter:
    """Отвечает за подготовку данных и запись в Google Sheets."""

    SITE_HEADER = [
        "source_site",
        "category",
        "category_url",
        "product_url",
        "product_content",
        "discovered_at",
        "run_id",
        "product_id_hash",
        "page_num",
        "metadata",
        "image_path",
        "name (en)",
        "name (ru)",
        "price (without discount)",
        "price (with discount)",
        "status",
        "note",
        "processed_at",
        "llm_raw",
    ]

    def __init__(
        self,
        context: RuntimeContext,
        client: GoogleSheetsClient | None = None,
        image_saver: ImageSaver | None = None,
    ):
        self.context = context
        self.client = client or GoogleSheetsClient(
            spreadsheet_id=context.config.sheet.spreadsheet_id,
            client_secret_path=self._env_path("GOOGLE_OAUTH_CLIENT_SECRET_PATH"),
            token_path=self._env_path("GOOGLE_OAUTH_TOKEN_PATH"),
            scopes=self._env_scopes("GOOGLE_OAUTH_SCOPES"),
            batch_size=context.config.sheet.write_batch_size,
            subject=self._env_optional("GOOGLE_OAUTH_IMPERSONATED_USER"),
        )
        assets_dir = context.assets_dir or Path("/app/assets/images")
        self.image_saver = image_saver or ImageSaver(context.config.network, assets_dir)
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

    def _env_optional(self, key: str) -> str | None:
        value = os.getenv(key)
        return value or None

    def prepare_site(self, site: SiteConfig) -> None:
        tab_name = site.domain
        if tab_name in self._prepared_tabs:
            return
        self.client.ensure_tabs([tab_name])
        self.client.ensure_header(tab_name, self.SITE_HEADER)
        self._existing_cache[tab_name] = self.client.get_existing_product_urls(tab_name)
        self._prepared_tabs.add(tab_name)

    def append_site_records(self, site: SiteConfig, records: list[ProductRecord]) -> None:
        if not records:
            return
        tab_name = site.domain
        self.prepare_site(site)
        existing = self._existing_cache.get(tab_name, set())
        rows: list[list[str]] = []
        new_image_paths: list[str] = []
        for record in records:
            if record.product_url in existing:
                continue
            existing.add(record.product_url)
            new_path = self._ensure_image_saved(record)
            if new_path:
                new_image_paths.append(new_path)
            rows.append(self._record_to_row(record))
        if not rows:
            return
        logger.info(
            "Подготовлено строк для записи",
            extra={"sheet": tab_name, "rows": len(rows)},
        )
        try:
            self.client.append_rows(tab_name, rows)
        except Exception:
            self._cleanup_images(new_image_paths)
            raise

    def finalize(self, results: list[SiteCrawlResult]) -> None:
        if not results:
            logger.warning("Нет данных для записи в Google Sheets")
            return
        self._write_runs(results)
        self._sync_state_sheet()
        self.image_saver.close()

    def _record_to_row(self, record: ProductRecord) -> list[str]:
        metadata_pairs = dict(record.metadata) if record.metadata else {}
        if record.image_url:
            metadata_pairs.setdefault("image_url", record.image_url)
        metadata_str = ""
        if metadata_pairs:
            metadata_str = ";".join(f"{k}={v}" for k, v in metadata_pairs.items())
        image_path = record.image_path or ""
        if image_path:
            image_path = Path(image_path).name
        processed_at = record.processed_at.isoformat() if record.processed_at else ""
        return [
            record.source_site,
            record.category or "",
            record.category_url,
            record.product_url,
            record.content_text or "",
            record.discovered_at.isoformat(),
            record.run_id,
            record.product_id_hash or "",
            str(record.page_num or ""),
            metadata_str,
            image_path,
            record.name_en or "",
            record.name_ru or "",
            record.price_without_discount or "",
            record.price_with_discount or "",
            record.status,
            record.note or "",
            processed_at,
            record.llm_raw or "",
        ]

    def _ensure_image_saved(self, record: ProductRecord) -> str | None:
        if record.image_path or not record.image_url:
            return None
        path = self.image_saver.save(
            record.image_url,
            record.image_name_hint,
            record.product_url,
        )
        if path:
            record.image_path = Path(path).name
        return path

    def _cleanup_images(self, paths: list[str]) -> None:
        for path_str in paths:
            try:
                Path(path_str).unlink(missing_ok=True)
            except OSError:
                continue

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
