from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from app.config.models import GlobalConfig, SiteConfig
from app.state.storage import StateStore


@dataclass(slots=True)
class RuntimeContext:
    """Общий контекст выполнения для всего запуска."""

    run_id: str
    started_at: datetime
    config: GlobalConfig
    sites: list[SiteConfig]
    state_store: StateStore
    dry_run: bool = False
    resume: bool = True
    assets_dir: Path | None = None
    flush_page_interval: int = 5

    @property
    def spreadsheet_id(self) -> str:
        return self.config.sheet.spreadsheet_id

    def iter_sites(self) -> Iterable[SiteConfig]:
        return iter(self.sites)
