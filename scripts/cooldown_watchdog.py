#!/usr/bin/env python3
"""Watchdog-скрипт: следит за логами и перезапускает контейнер при частых таймаутах."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

TRIGGER_PHRASES = (
    "Достигнут предел подряд неудачных загрузок категорий",
    "Достигнут предел подряд неудачных попыток загрузки страниц",
    "Достигнут предел подряд неудачных загрузок карточек",
)

_LOG_FILE: Path | None = None


def should_trigger(line: str) -> bool:
    """Возвращает True, если строка лога сообщает о превышении порога неудачных попыток."""
    return any(phrase in line for phrase in TRIGGER_PHRASES)


def restart_stack(
    project_dir: Path, compose_bin: str, service: str, mode: str = "stack"
) -> None:
    """Перезапускает сервис, используя выбранный режим."""
    if mode == "service":
        commands: list[list[str]] = [
            [compose_bin, "compose", "stop", service],
            [compose_bin, "compose", "rm", "-f", service],
            [compose_bin, "compose", "up", "-d", "--build", service],
        ]
    else:
        commands = [
            [compose_bin, "compose", "down"],
            [compose_bin, "compose", "up", "-d", "--build", service],
        ]
    for command in commands:
        subprocess.run(command, cwd=project_dir, check=True)


def _follow_log(log_path: Path, poll_interval: float) -> Iterator[str]:
    """Генератор новых строк файла (tail -f)."""
    while not log_path.exists():
        time.sleep(poll_interval)
    with log_path.open("r", encoding="utf-8") as handle:
        handle.seek(0, os.SEEK_END)
        while True:
            line = handle.readline()
            if not line:
                time.sleep(poll_interval)
                continue
            yield line


def _log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[cooldown-watchdog] {timestamp} {message}"
    print(line, flush=True)
    if _LOG_FILE:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_FILE.open("a", encoding="utf-8") as fp:
            fp.write(line + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Следит за логом агента и перезапускает docker compose после серии таймаутов."
    )
    parser.add_argument(
        "--log-file",
        required=True,
        help="Путь к parser.log, который будет прослушиваться (tail -f).",
    )
    parser.add_argument(
        "--project-dir",
        default=os.getcwd(),
        help="Каталог, где расположен docker-compose.yml (по умолчанию текущий).",
    )
    parser.add_argument(
        "--service",
        default="parser",
        help="Название сервиса в docker compose, который нужно перезапустить (по умолчанию parser).",
    )
    parser.add_argument(
        "--compose-bin",
        default="docker",
        help="Бинарь docker/nerdctl/... через который вызывается compose (по умолчанию docker).",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="Интервал чтения файла в секундах.",
    )
    parser.add_argument(
        "--debounce-seconds",
        type=int,
        default=300,
        help="Минимальный интервал между перезапусками, чтобы не перегружать контейнер (секунды).",
    )
    parser.add_argument(
        "--restart-mode",
        choices=("stack", "service"),
        default="stack",
        help=(
            "Какой набор docker compose команд использовать: stack — down/up всей связки, "
            "service — перезапустить только указанный сервис (для запуска watchdog внутри compose)."
        ),
    )
    parser.add_argument(
        "--log-output",
        help="Путь к дополнительному файлу лога watchdog (пишется параллельно stdout).",
    )
    args = parser.parse_args()

    log_path = Path(args.log_file)
    project_dir = Path(args.project_dir)
    last_restart_ts = 0.0
    global _LOG_FILE
    if args.log_output:
        _LOG_FILE = Path(args.log_output)

    _log(f"Старт слежения за {log_path}")
    for line in _follow_log(log_path, args.poll_interval):
        if not should_trigger(line):
            continue
        now = time.time()
        if now - last_restart_ts < args.debounce_seconds:
            _log("Сигнал уже обрабатывался, пропускаем повторный перезапуск.")
            continue
        _log(f"Обнаружен сигнал в логе: {line.strip()}")
        try:
            restart_stack(
                project_dir, args.compose_bin, args.service, mode=args.restart_mode
            )
        except subprocess.CalledProcessError as exc:  # pragma: no cover - внешние ошибки
            _log(f"Не удалось перезапустить docker compose: {exc}")
        else:
            last_restart_ts = now
            _log("Контейнер перезапущен.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
