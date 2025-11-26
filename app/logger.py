import logging
import os
from pathlib import Path
from typing import Literal, Optional

from rich.console import Console
from rich.logging import RichHandler

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

_configured = False


def configure_logging(level: LogLevel = "INFO") -> None:
    """Настраивает цветной логгер один раз за запуск."""
    global _configured
    if not _configured:
        console = Console()
        handlers: list[logging.Handler] = [
            RichHandler(console=console, show_path=False, markup=True)
        ]
        file_handler = _build_file_handler(console)
        if file_handler:
            handlers.append(file_handler)
        logging.basicConfig(
            level=level,
            format="%(message)s",
            datefmt="[%X]",
            handlers=handlers,
        )
        _configured = True
    else:
        logging.getLogger().setLevel(level)


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Возвращает готовый логгер модуля."""
    configure_logging()
    return logging.getLogger(name)


def _build_file_handler(console: Console) -> logging.Handler | None:
    """Создаёт файловый обработчик, если указан LOG_FILE_PATH."""
    log_path_str = os.getenv("LOG_FILE_PATH")
    if not log_path_str:
        return None
    try:
        log_path = Path(log_path_str).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        return handler
    except Exception as exc:  # pragma: no cover
        console.print(
            f"[yellow]Не удалось настроить файловый логгер '{log_path_str}': {exc}[/yellow]"
        )
        return None
