"""Пакет мониторинга ошибок и вспомогательных инструментов."""

from .error_events import ErrorEvent, build_error_event

__all__ = ["ErrorEvent", "build_error_event"]
