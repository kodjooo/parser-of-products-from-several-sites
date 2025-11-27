#!/usr/bin/env python3
"""Утилита для предварительного создания рабочих директорий агента."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable

RUNTIME_DIRS = (
    Path("state"),
    Path("assets/images"),
    Path("logs"),
)


def ensure_directories(base_path: Path, dirs: Iterable[Path]) -> list[Path]:
    """Создаёт каталоги относительно base_path и возвращает список созданных путей."""
    created: list[Path] = []
    for relative in dirs:
        target = (base_path / relative).resolve()
        if not target.exists():
            target.mkdir(parents=True, exist_ok=True)
            created.append(target)
    return created


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "создаёт рабочие каталоги state/assets/images/logs рядом с проектом "
            "и выводит их абсолютные пути"
        )
    )
    parser.add_argument(
        "--base",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="корневой каталог проекта (по умолчанию определяем автоматически)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    base_path: Path = args.base.resolve()
    created = ensure_directories(base_path, RUNTIME_DIRS)
    for directory in RUNTIME_DIRS:
        path = (base_path / directory).resolve()
        status = "created" if path in created else "exists"
        print(f"[{status}] {path}")


if __name__ == "__main__":
    main()
