"""Главная точка входа CLI."""

from app.cli import entrypoint


def main() -> None:
    """Делегирует выполнение Typer-приложению."""
    entrypoint()


if __name__ == "__main__":
    main()
