from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from app.config.runtime_paths import resolve_str_path
from app.logger import configure_logging, get_logger
from app.workflow.runner import AgentRunner, RunnerOptions

console = Console()
cli = typer.Typer(help="Гибкий агент сбора ссылок товаров из категорий сайтов.")
logger = get_logger(__name__)


def _resolve_sites_dir_cli(sites_dir: Optional[Path]) -> Path:
    if sites_dir is not None:
        return sites_dir
    return Path(
        resolve_str_path(
            "SITE_CONFIG_DIR",
            local_default="config/sites",
            docker_default="/app/config/sites",
        )
    )


def _build_runner_options(
    *,
    config_path: Optional[Path],
    sites_dir: Path,
    run_id: Optional[str],
    resume: bool,
    reset_state: bool,
    dry_run: bool,
) -> RunnerOptions:
    return RunnerOptions(
        config_path=config_path,
        sites_dir=sites_dir,
        run_id=run_id,
        resume=resume,
        reset_state=reset_state,
        dry_run=dry_run,
    )


def _common_run_options() -> dict:
    return dict(
        config_path=typer.Option(
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
        sites_dir=typer.Option(
            None,
            "--sites-dir",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            envvar="SITE_CONFIG_DIR",
            help="Каталог с конфигурациями сайтов.",
        ),
        log_level=typer.Option(
            "INFO",
            "--log-level",
            envvar="LOG_LEVEL",
            help="Уровень логирования (DEBUG/INFO/WARNING/ERROR/CRITICAL). "
            "Можно задать через переменную окружения LOG_LEVEL.",
        ),
        resume=typer.Option(
            True,
            "--resume/--no-resume",
            help="Продолжать с учётом сохранённого state.",
        ),
        reset_state=typer.Option(
            False,
            "--reset-state",
            help="Перед запуском стереть локальное состояние.",
        ),
        dry_run=typer.Option(
            False,
            "--dry-run",
            help="Выполнить обход без записи данных в Google Sheets.",
        ),
    )


@cli.command("run")
def run_agent(
    config_path: Optional[Path] = _common_run_options()["config_path"],
    sites_dir: Optional[Path] = _common_run_options()["sites_dir"],
    run_id: Optional[str] = typer.Option(
        None,
        "--run-id",
        help="Идентификатор запуска (по умолчанию генерируется UUID4).",
    ),
    log_level: str = _common_run_options()["log_level"],
    resume: bool = _common_run_options()["resume"],
    reset_state: bool = _common_run_options()["reset_state"],
    dry_run: bool = _common_run_options()["dry_run"],
) -> None:
    """Точка входа для единичного запуска агента."""
    configure_logging(log_level.upper())  # type: ignore[arg-type]
    sites_dir_path = _resolve_sites_dir_cli(sites_dir)
    runner = AgentRunner()
    options = _build_runner_options(
        config_path=config_path,
        sites_dir=sites_dir_path,
        run_id=run_id,
        resume=resume,
        reset_state=reset_state,
        dry_run=dry_run,
    )
    runner.run(options)
    console.print("[bold green]Запуск агента завершён[/bold green]")


@cli.command("watch")
def watch_agent(
    config_path: Optional[Path] = _common_run_options()["config_path"],
    sites_dir: Optional[Path] = _common_run_options()["sites_dir"],
    log_level: str = _common_run_options()["log_level"],
    resume: bool = _common_run_options()["resume"],
    reset_state: bool = _common_run_options()["reset_state"],
    dry_run: bool = _common_run_options()["dry_run"],
    success_delay: float = typer.Option(
        300.0,
        "--success-delay",
        min=0.0,
        help="Пауза между успешными циклами (секунды).",
    ),
    error_delay: float = typer.Option(
        120.0,
        "--error-delay",
        min=0.0,
        help="Пауза перед повторным запуском после ошибки (секунды).",
    ),
    max_runs: Optional[int] = typer.Option(
        None,
        "--max-runs",
        min=1,
        help="Опциональный лимит числа итераций watch-режима.",
    ),
) -> None:
    """Непрерывный режим: перезапускает агента после завершения или ошибки."""
    configure_logging(log_level.upper())  # type: ignore[arg-type]
    sites_dir_path = _resolve_sites_dir_cli(sites_dir)
    runner = AgentRunner()
    options = _build_runner_options(
        config_path=config_path,
        sites_dir=sites_dir_path,
        run_id=None,
        resume=resume,
        reset_state=reset_state,
        dry_run=dry_run,
    )
    runs_completed = 0
    console.print(
        "[cyan]Watch-режим активирован[/cyan]: "
        f"success_delay={success_delay}s, error_delay={error_delay}s, "
        f"max_runs={max_runs or '∞'}",
    )
    try:
        while max_runs is None or runs_completed < max_runs:
            wait_time = success_delay
            try:
                runner.run(options)
                runs_completed += 1
                logger.info("Цикл обхода #%s завершён", runs_completed)
            except Exception:  # pragma: no cover - зависит от окружения запуска
                logger.exception(
                    "Запуск агента завершился ошибкой, повтор через %s секунд",
                    error_delay,
                )
                wait_time = error_delay
            if max_runs is not None and runs_completed >= max_runs:
                break
            if wait_time <= 0:
                continue
            typer.echo(f"Следующий запуск через {wait_time:.0f} секунд")
            try:
                typer.sleep(wait_time)
            except KeyboardInterrupt:
                raise
    except KeyboardInterrupt:
        console.print("[yellow]Watch-режим остановлен пользователем[/yellow]")


def entrypoint() -> None:
    """CLI entrypoint для Docker."""
    cli()
