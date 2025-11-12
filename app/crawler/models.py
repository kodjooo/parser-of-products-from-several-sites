from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal


Status = Literal["new", "duplicate", "failed"]


@dataclass(slots=True)
class ProductRecord:
    source_site: str
    category_url: str
    product_url: str
    run_id: str
    discovered_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    status: Status = "new"
    note: str | None = None
    page_num: int | None = None
    product_id_hash: str | None = None
    metadata: dict[str, str] | None = None
    content_text: str | None = None
    image_url: str | None = None
    image_path: str | None = None


@dataclass(slots=True)
class CategoryMetrics:
    site_name: str
    category_url: str
    total_found: int = 0
    total_written: int = 0
    total_duplicates: int = 0
    total_failed: int = 0
    last_page: int | None = None


@dataclass(slots=True)
class SiteCrawlResult:
    site_name: str
    sheet_tab: str
    records: list[ProductRecord]
    metrics: list[CategoryMetrics]
