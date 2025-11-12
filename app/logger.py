import logging
from typing import Literal, Optional

from rich.console import Console
from rich.logging import RichHandler

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

_configured = False


def configure_logging(level: LogLevel = "INFO") -> None:
    """Настраивает цветной логгер один раз за запуск."""
    global _configured
    if _configured:
        return

    console = Console()
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, show_path=False, markup=True)],
    )
    _configured = True


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Возвращает готовый логгер модуля."""
    configure_logging()
    return logging.getLogger(name)
