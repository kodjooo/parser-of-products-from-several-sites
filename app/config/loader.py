from __future__ import annotations
from itertools import chain
from pathlib import Path
from typing import Iterable, Sequence

import yaml
from pydantic import ValidationError

from app.config.env_loader import load_global_config_from_env
from app.config.errors import ConfigLoaderError
from app.config.models import GlobalConfig, SiteConfig
from app.logger import get_logger

logger = get_logger(__name__)


def load_global_config(path: Path | None) -> GlobalConfig:
    """Загружает общую конфигурацию из файла или из окружения."""
    if path:
        return _load_global_config_from_file(path)
    logger.info("Глобальная конфигурация читается из переменных окружения")
    return load_global_config_from_env()


def _load_global_config_from_file(path: Path) -> GlobalConfig:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return GlobalConfig.model_validate(data)
    except FileNotFoundError as exc:
        raise ConfigLoaderError(f"Файл {path} не найден") from exc
    except ValidationError as exc:
        raise ConfigLoaderError(f"Некорректная общая конфигурация: {exc}") from exc


def iter_site_configs(directory: Path) -> Iterable[SiteConfig]:
    """Возвращает генератор валидных конфигураций сайтов."""
    patterns: Sequence[str] = ("*.yml", "*.yaml", "*.json")
    files = chain.from_iterable(sorted(directory.glob(pattern)) for pattern in patterns)
    for site_file in files:
        raw = yaml.safe_load(site_file.read_text(encoding="utf-8"))
        try:
            yield SiteConfig.model_validate(raw)
        except ValidationError as exc:
            raise ConfigLoaderError(
                f"Ошибка в конфигурации сайта {site_file.name}: {exc}"
            ) from exc
