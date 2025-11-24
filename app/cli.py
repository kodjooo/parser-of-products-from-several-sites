from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from app.logger import configure_logging
from app.workflow.runner import AgentRunner, RunnerOptions

console = Console()
cli = typer.Typer(help="Гибкий агент сбора ссылок товаров из категорий сайтов.")


@cli.command("run")
def run_agent(
    config_path: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        envvar="GLOBAL_CONFIG_PATH",
        help="Путь к общей конфигурации запуска (YAML/JSON). "
        "Если не указан, используется конфигурация из переменных окружения.",
    ),
    sites_dir: Path = typer.Option(
        ...,
        "--sites-dir",
        exists=True,
        file_okay=False,
        dir_okay=True,
        readable=True,
        envvar="SITE_CONFIG_DIR",
        help="Каталог с конфигурациями сайтов.",
    ),
    run_id: Optional[str] = typer.Option(
        None,
        "--run-id",
        help="Идентификатор запуска (по умолчанию генерируется UUID4).",
    ),
    log_level: str = typer.Option(
        "INFO",
        "--log-level",
        envvar="LOG_LEVEL",
        help="Уровень логирования (DEBUG/INFO/WARNING/ERROR/CRITICAL). "
        "Можно задать через переменную окружения LOG_LEVEL.",
    ),
    resume: bool = typer.Option(
        True,
        "--resume/--no-resume",
        help="Продолжать с учётом сохранённого state.",
    ),
    reset_state: bool = typer.Option(
        False,
        "--reset-state",
        help="Перед запуском стереть локальное состояние.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Выполнить обход без записи данных в Google Sheets.",
    ),
) -> None:
    """Точка входа для запуска агента."""
    configure_logging(log_level.upper())  # type: ignore[arg-type]
    runner = AgentRunner()
    options = RunnerOptions(
        config_path=config_path,
        sites_dir=sites_dir,
        run_id=run_id,
        resume=resume,
        reset_state=reset_state,
        dry_run=dry_run,
    )
    runner.run(options)
    console.print("[bold green]Запуск агента завершён[/bold green]")


def entrypoint() -> None:
    """CLI entrypoint для Docker."""
    cli()
