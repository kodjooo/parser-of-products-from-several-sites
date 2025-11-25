from __future__ import annotations

import os
from datetime import datetime, timezone
import time
from pathlib import Path
from collections.abc import Iterable, Sequence

from app.config.models import SiteConfig
from app.crawler.models import ProductRecord, SiteCrawlResult
from app.logger import get_logger
from app.runtime import RuntimeContext
from app.sheets.client import GoogleSheetsClient
from app.media.image_saver import ImageSaver
from app.config.runtime_paths import resolve_path

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
            client_secret_path=resolve_path(
                "GOOGLE_OAUTH_CLIENT_SECRET_PATH",
                local_default="secrets/google-credentials.json",
                docker_default="/secrets/google-credentials.json",
            ),
            token_path=resolve_path(
                "GOOGLE_OAUTH_TOKEN_PATH",
                local_default="state/token.json",
                docker_default="/var/app/state/token.json",
            ),
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

    def get_existing_urls(self, site: SiteConfig) -> set[str]:
        tab_name = site.domain
        cache = self._existing_cache.get(tab_name)
        if cache is None:
            return set()
        return set(cache)

    def append_site_records(self, site: SiteConfig, records: list[ProductRecord]) -> None:
        if not records:
            return
        tab_name = site.domain
        self.prepare_site(site)
        existing = self._existing_cache.get(tab_name, set())
        rows: list[list[str]] = []
        new_image_paths: list[str] = []
        new_urls: list[str] = []
        for record in records:
            if record.product_url in existing:
                continue
            new_path = self._ensure_image_saved(record)
            if new_path:
                new_image_paths.append(new_path)
            rows.append(self._record_to_row(record))
            new_urls.append(record.product_url)
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
        else:
            existing.update(new_urls)

    def append_site_records_with_retry(
        self,
        site: SiteConfig,
        records: list[ProductRecord],
        *,
        max_attempts: int = 3,
        delay_sec: float | Sequence[float] | None = None,
    ) -> None:
        """
        Оборачивает append_site_records повторными попытками.
        """
        if not records:
            return
        attempts = max(1, max_attempts)
        delay_schedule = self._resolve_delay_schedule(delay_sec, attempts)
        attempt = 1
        while True:
            try:
                self.append_site_records(site, records)
                return
            except Exception:
                if attempt >= attempts:
                    raise
                wait = delay_schedule[attempt - 1]
                logger.warning(
                    "Не удалось записать строки в Google Sheets, повтор через %.1f сек",
                    wait,
                    extra={"site": site.name, "attempt": attempt},
                    exc_info=True,
                )
                time.sleep(wait)
                attempt += 1

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

    def _resolve_delay_schedule(
        self,
        delay_spec: float | Sequence[float] | None,
        attempts: int,
    ) -> list[float]:
        """Возвращает список задержек между попытками."""
        required = max(attempts - 1, 0)
        if required == 0:
            return []

        def _normalize(values: Sequence[float]) -> list[float]:
            normalized: list[float] = []
            for value in values:
                try:
                    normalized.append(max(0.0, float(value)))
                except (TypeError, ValueError) as exc:
                    raise ValueError("Некорректное значение задержки") from exc
            if not normalized:
                normalized.append(0.0)
            return normalized

        if delay_spec is None:
            base = [600.0, 1200.0]
        elif isinstance(delay_spec, Sequence) and not isinstance(delay_spec, (str, bytes)):
            base = _normalize(delay_spec)
        else:
            base = _normalize([delay_spec])

        while len(base) < required:
            base.append(base[-1])
        return base[:required]

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
