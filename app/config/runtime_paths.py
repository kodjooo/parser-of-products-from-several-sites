from __future__ import annotations

import os
from pathlib import Path

LOCAL_ENV = "local"
DOCKER_ENV = "docker"


def get_run_env() -> str:
    value = (os.getenv("APP_RUN_ENV") or "").strip().lower()
    if value in {LOCAL_ENV, DOCKER_ENV}:
        return value
    # Автоматически определяем docker по наличию служебного файла внутри контейнера.
    if Path("/.dockerenv").exists() or os.getenv("DOCKER_CONTAINER"):
        return DOCKER_ENV
    return LOCAL_ENV


def resolve_path(
    env_name: str,
    *,
    local_default: str,
    docker_default: str,
) -> Path:
    value = (os.getenv(env_name) or "").strip()
    if value:
        return Path(value)
    return Path(docker_default if get_run_env() == DOCKER_ENV else local_default)


def resolve_str_path(
    env_name: str,
    *,
    local_default: str,
    docker_default: str,
) -> str:
    return str(resolve_path(env_name, local_default=local_default, docker_default=docker_default))


def resolve_optional_path(
    env_name: str,
    *,
    local_default: str,
    docker_default: str,
    require_exists: bool = False,
) -> Path | None:
    """
    Возвращает путь, если он явно указан или есть валидное значение по умолчанию.

    Если переменная окружения задана — возвращаем её без дополнительных проверок, чтобы
    можно было создавать файлы по требованию (например, токен Google OAuth).
    Если переменная пустая, подставляем значение в зависимости от APP_RUN_ENV и,
    при необходимости, проверяем существование файла/каталога.
    """
    value = (os.getenv(env_name) or "").strip()
    if value:
        return Path(value)
    candidate = Path(docker_default if get_run_env() == DOCKER_ENV else local_default)
    if require_exists and not candidate.exists():
        return None
    return candidate
