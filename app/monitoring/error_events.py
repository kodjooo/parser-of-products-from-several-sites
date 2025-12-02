from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


Action = str | Iterable[str]


@dataclass(slots=True)
class ErrorEvent:
    """Структурированное описание сетевой ошибки для дальнейшей обработки ИИ-агентом."""

    error_type: str
    error_source: str
    url: str | None = None
    proxy: str | None = None
    retry_index: int | None = None
    action_required: Action | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error_type": self.error_type,
            "error_source": self.error_source,
            "timestamp": _now_iso(),
        }
        if self.url:
            payload["url"] = self.url
        if self.proxy:
            payload["proxy"] = self.proxy
        if self.retry_index is not None:
            payload["retry_index"] = self.retry_index
        if self.action_required:
            if isinstance(self.action_required, str):
                payload["action_required"] = self.action_required
            else:
                payload["action_required"] = list(self.action_required)
        if self.metadata:
            payload["details"] = self.metadata
        return payload


def build_error_event(
    *,
    error_type: str,
    error_source: str,
    url: str | None = None,
    proxy: str | None = None,
    retry_index: int | None = None,
    action_required: Action | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Упрощённый фабричный метод для создания словаря события ошибки."""
    event = ErrorEvent(
        error_type=error_type,
        error_source=error_source,
        url=url,
        proxy=proxy,
        retry_index=retry_index,
        action_required=action_required,
        metadata=metadata or {},
    )
    return event.to_dict()
