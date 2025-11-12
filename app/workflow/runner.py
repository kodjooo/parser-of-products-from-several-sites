from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
import os

from app.config.loader import ConfigLoaderError, iter_site_configs, load_global_config
from app.crawler.service import CrawlService
from app.logger import get_logger
from app.runtime import RuntimeContext
from app.state.storage import StateStore
from app.sheets.writer import SheetsWriter

console = Console()
logger = get_logger(__name__)


@dataclass(slots=True)
class RunnerOptions:
    config_path: Path | None
    sites_dir: Path
    run_id: str | None = None
    resume: bool = True
    reset_state: bool = False
    dry_run: bool = False


class AgentRunner:
    """Высокоуровневый раннер, который координирует запуск агента."""

    def __init__(self) -> None:
        self.latest_results = []

    def run(self, options: RunnerOptions) -> None:
        load_dotenv()
        run_id = options.run_id or str(uuid.uuid4())
        logger.info(
            "Запуск агента",
            extra={
                "run_id": run_id,
                "config": str(options.config_path) if options.config_path else "env",
                "sites_dir": str(options.sites_dir),
            },
        )

        try:
            global_config = load_global_config(options.config_path)
            site_configs = list(iter_site_configs(options.sites_dir))
        except ConfigLoaderError as exc:
            console.print(f"[bold red]Ошибка конфигурации:[/bold red] {exc}")
            raise

        state_store = StateStore(global_config.state.database)
        if options.reset_state:
            logger.warning("Запрошен полный сброс локального состояния")
            state_store.reset_all()

        assets_dir = Path(os.getenv("PRODUCT_IMAGE_DIR", "/app/assets/images"))
        assets_dir.mkdir(parents=True, exist_ok=True)
        flush_pages = int(os.getenv("WRITE_FLUSH_PAGE_INTERVAL", "5") or "5")
        if flush_pages < 1:
            flush_pages = 5

        context = RuntimeContext(
            run_id=run_id,
            started_at=datetime.now(timezone.utc),
            config=global_config,
            sites=site_configs,
            state_store=state_store,
            dry_run=options.dry_run,
            resume=options.resume,
            assets_dir=assets_dir,
            flush_page_interval=flush_pages,
        )
        try:
            self._execute(context)
        finally:
            state_store.close()

    def _execute(self, context: RuntimeContext) -> None:
        console.print(
            f"[yellow]Контекст подготовлен[/yellow]: лист={context.spreadsheet_id}, "
            f"сайтов={len(context.sites)}, resume={context.resume}, "
            f"dry_run={context.dry_run}"
        )
        writer = SheetsWriter(context) if not context.dry_run else None
        crawler = CrawlService(context, writer=writer)
        self.latest_results = crawler.collect()
        total_records = sum(len(result.records) for result in self.latest_results)
        console.print(
            f"[green]Обход завершён[/green]: сайтов={len(self.latest_results)}, "
            f"ссылок={total_records}"
        )
        if writer is None:
            console.print("[cyan]Dry-run: запись в Google Sheets пропущена[/cyan]")
            return
        writer.finalize(self.latest_results)
        console.print("[green]Данные записаны в Google Sheets[/green]")
