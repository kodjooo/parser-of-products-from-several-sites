from pathlib import Path

from scripts import cooldown_watchdog as watchdog


def test_should_trigger_matches_known_messages() -> None:
    assert watchdog.should_trigger("2025 WARNING Достигнут предел подряд неудачных загрузок категорий ...")
    assert watchdog.should_trigger("Достигнут предел подряд неудачных попыток загрузки страниц, временно приостанавливаем обход")
    assert watchdog.should_trigger("Достигнут предел подряд неудачных загрузок карточек, делаем паузу")
    assert not watchdog.should_trigger("INFO что-то другое")


def test_restart_stack_invokes_commands(monkeypatch, tmp_path: Path) -> None:
    commands: list[tuple[tuple[str, ...], Path]] = []

    def fake_run(cmd, cwd=None, check=None):
        commands.append((tuple(cmd), Path(cwd)))
        return None

    monkeypatch.setattr(watchdog.subprocess, "run", fake_run)

    watchdog.restart_stack(tmp_path, "docker", "parser")

    assert commands == [
        (("docker", "compose", "down"), tmp_path),
        (("docker", "compose", "up", "-d", "--build", "parser"), tmp_path),
    ]
