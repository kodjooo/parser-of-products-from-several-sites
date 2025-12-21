import queue
import threading
import time
from pathlib import Path

from scripts import cooldown_watchdog as watchdog


def test_should_trigger_matches_known_messages() -> None:
    assert watchdog.should_trigger("2025 WARNING Достигнут предел подряд неудачных загрузок категорий ...")
    assert watchdog.should_trigger("Достигнут предел подряд неудачных попыток загрузки страниц, временно приостанавливаем обход")
    assert watchdog.should_trigger("Достигнут предел подряд неудачных загрузок карточек, делаем паузу")
    assert not watchdog.should_trigger("INFO что-то другое")


def test_restart_stack_invokes_commands(monkeypatch, tmp_path: Path) -> None:
    commands: list[tuple[tuple[str, ...], Path]] = []

    def fake_run(cmd, cwd=None, check=None, env=None, timeout=None):
        commands.append((tuple(cmd), Path(cwd)))
        return None

    monkeypatch.setattr(watchdog.subprocess, "run", fake_run)

    watchdog.restart_stack(tmp_path, "docker", "parser")

    assert commands == [
        (("docker", "compose", "down"), tmp_path),
        (("docker", "compose", "up", "-d", "--build", "parser"), tmp_path),
    ]


def test_restart_stack_skips_build(monkeypatch, tmp_path: Path) -> None:
    commands: list[tuple[tuple[str, ...], Path]] = []

    def fake_run(cmd, cwd=None, check=None, env=None, timeout=None):
        commands.append((tuple(cmd), Path(cwd)))
        return None

    monkeypatch.setattr(watchdog.subprocess, "run", fake_run)

    watchdog.restart_stack(tmp_path, "docker", "parser", mode="service", build="off")

    assert commands == [
        (("docker", "compose", "stop", "parser"), tmp_path),
        (("docker", "compose", "rm", "-f", "parser"), tmp_path),
        (("docker", "compose", "up", "-d", "parser"), tmp_path),
    ]


def test_run_with_retries_succeeds_after_retry(monkeypatch) -> None:
    attempts: list[int] = []

    def flaky() -> None:
        attempts.append(1)
        if len(attempts) < 2:
            raise watchdog.subprocess.CalledProcessError(1, ["docker"])

    def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(watchdog.time, "sleep", fake_sleep)

    assert watchdog._run_with_retries(flaky, attempts=3, delay_seconds=0.1) is True
    assert len(attempts) == 2


def test_follow_log_handles_truncate(tmp_path: Path) -> None:
    log_path = tmp_path / "parser.log"
    log_path.write_text("old line\n", encoding="utf-8")

    collected: "queue.Queue[str]" = queue.Queue()

    def reader() -> None:
        for line in watchdog._follow_log(log_path, 0.01):
            collected.put(line)
            if collected.qsize() >= 2:
                break

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    log_path.write_text("", encoding="utf-8")
    with log_path.open("a", encoding="utf-8") as fp:
        fp.write("first\n")
    first = collected.get(timeout=1.0)

    log_path.write_text("", encoding="utf-8")
    with log_path.open("a", encoding="utf-8") as fp:
        fp.write("second\n")
    second = collected.get(timeout=1.0)

    assert first.strip() == "first"
    assert second.strip() == "second"
