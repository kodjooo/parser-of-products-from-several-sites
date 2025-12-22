#!/usr/bin/env python3
"""Watchdog-скрипт: следит за логами и перезапускает контейнер при частых таймаутах."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Iterator
from datetime import datetime

TRIGGER_PHRASES = (
    "Достигнут предел подряд неудачных загрузок категорий",
    "Достигнут предел подряд неудачных попыток загрузки страниц",
    "Достигнут предел подряд неудачных загрузок карточек",
)

_LOG_FILE: Path | None = None


def should_trigger(line: str) -> bool:
    """Возвращает True, если строка лога сообщает о превышении порога неудачных попыток."""
    return any(phrase in line for phrase in TRIGGER_PHRASES)


def _parse_log_timestamp(line: str) -> datetime | None:
    """Пытается извлечь дату/время из начала строки лога."""
    if len(line) < 19:
        return None
    candidate = line[:19]
    try:
        return datetime.strptime(candidate, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def restart_stack(
    project_dir: Path,
    compose_bin: str,
    service: str,
    mode: str = "stack",
    buildkit: str = "on",
    build: str = "on",
    command_timeout: float | None = None,
) -> None:
    """Перезапускает сервис, используя выбранный режим."""
    env = os.environ.copy()
    if buildkit == "off":
        env["DOCKER_BUILDKIT"] = "0"
        env["COMPOSE_DOCKER_CLI_BUILD"] = "0"
    build_flag: list[str] = ["--build"] if build == "on" else []
    if mode == "service":
        commands: list[list[str]] = [
            [compose_bin, "compose", "stop", service],
            [compose_bin, "compose", "rm", "-f", service],
            [compose_bin, "compose", "up", "-d", *build_flag, service],
        ]
    else:
        commands = [
            [compose_bin, "compose", "down"],
            [compose_bin, "compose", "up", "-d", *build_flag],
        ]
    for command in commands:
        action = "unknown"
        if "down" in command:
            action = "down"
        elif "up" in command:
            action = "up"
        elif "stop" in command:
            action = "stop"
        elif "rm" in command:
            action = "rm"
        _log(f"Запускаем команду ({action}): {' '.join(command)}")
        subprocess.run(
            command,
            cwd=project_dir,
            check=True,
            env=env,
            timeout=command_timeout,
        )


def _get_inode(path: Path) -> int:
    return path.stat().st_ino


def _follow_log(log_path: Path, poll_interval: float) -> Iterator[str]:
    """Генератор новых строк файла (tail -f) с поддержкой ротации/усечения."""
    while not log_path.exists():
        time.sleep(poll_interval)
    handle = log_path.open("r", encoding="utf-8")
    handle.seek(0, os.SEEK_END)
    current_inode = _get_inode(log_path)
    while True:
        line = handle.readline()
        if line:
            yield line
            continue
        time.sleep(poll_interval)
        if not log_path.exists():
            handle.close()
            while not log_path.exists():
                time.sleep(poll_interval)
            handle = log_path.open("r", encoding="utf-8")
            handle.seek(0, os.SEEK_END)
            current_inode = _get_inode(log_path)
            continue
        try:
            stat = log_path.stat()
        except FileNotFoundError:
            continue
        if stat.st_ino != current_inode:
            handle.close()
            handle = log_path.open("r", encoding="utf-8")
            handle.seek(0, os.SEEK_END)
            current_inode = stat.st_ino
            continue
        if stat.st_size < handle.tell():
            handle.seek(0)


def _log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[cooldown-watchdog] {timestamp} {message}"
    print(line, flush=True)
    if _LOG_FILE:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_FILE.open("a", encoding="utf-8") as fp:
            fp.write(line + "\n")


def _run_with_retries(
    action: Callable[[], None],
    attempts: int,
    delay_seconds: float,
) -> bool:
    """Выполняет действие с повторами. attempts=0 означает бесконечные попытки."""
    attempt = 0
    while True:
        attempt += 1
        try:
            action()
            return True
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            _log(f"Ошибка перезапуска (попытка {attempt}): {exc}")
            if attempts and attempt >= attempts:
                return False
            time.sleep(delay_seconds)


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
        "--buildkit",
        choices=("on", "off"),
        default="on",
        help=(
            "Как запускать сборку: on — обычный BuildKit, off — "
            "отключить BuildKit (DOCKER_BUILDKIT=0)."
        ),
    )
    parser.add_argument(
        "--build",
        choices=("on", "off"),
        default="on",
        help="Нужно ли пересобирать сервис при перезапуске (по умолчанию on).",
    )
    parser.add_argument(
        "--command-timeout",
        type=float,
        default=600.0,
        help="Таймаут на выполнение одной docker compose команды (секунды).",
    )
    parser.add_argument(
        "--retry-attempts",
        type=int,
        default=3,
        help="Сколько раз повторять перезапуск при ошибке (0 — бесконечно).",
    )
    parser.add_argument(
        "--retry-delay-seconds",
        type=float,
        default=60.0,
        help="Пауза между попытками перезапуска (секунды).",
    )
    parser.add_argument(
        "--log-output",
        help="Путь к дополнительному файлу лога watchdog (пишется параллельно stdout).",
    )
    args = parser.parse_args()

    log_path = Path(args.log_file)
    project_dir = Path(args.project_dir)
    last_restart_ts = 0.0
    start_dt = datetime.utcnow()
    global _LOG_FILE
    if args.log_output:
        _LOG_FILE = Path(args.log_output)

    _log(f"Старт слежения за {log_path}")
    for line in _follow_log(log_path, args.poll_interval):
        if not should_trigger(line):
            continue
        line_ts = _parse_log_timestamp(line)
        if line_ts and line_ts < start_dt:
            _log("Сигнал слишком старый, пропускаем перезапуск.")
            continue
        now = time.time()
        if now - last_restart_ts < args.debounce_seconds:
            _log("Сигнал уже обрабатывался, пропускаем повторный перезапуск.")
            continue
        _log(f"Обнаружен сигнал в логе: {line.strip()}")
        def _attempt_restart() -> None:
            restart_stack(
                project_dir,
                args.compose_bin,
                args.service,
                mode=args.restart_mode,
                buildkit=args.buildkit,
                build=args.build,
                command_timeout=args.command_timeout,
            )

        if _run_with_retries(_attempt_restart, args.retry_attempts, args.retry_delay_seconds):
            last_restart_ts = now
            _log("Контейнер перезапущен.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
