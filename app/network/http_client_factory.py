from __future__ import annotations

from typing import Any, Dict

import httpx


class HttpClientFactory:
    """Кеширует httpx.Client по значению прокси."""

    def __init__(self, *, base_kwargs: Dict[str, Any] | None = None, **kwargs: Any) -> None:
        if base_kwargs is not None and kwargs:
            raise ValueError("Используйте либо base_kwargs, либо именованные параметры, но не оба")
        if base_kwargs is not None:
            self._base_kwargs = dict(base_kwargs)
        else:
            self._base_kwargs = dict(kwargs)
        self._clients: dict[str, httpx.Client] = {}

    def get(self, proxy: str | None) -> httpx.Client:
        key = proxy or "__direct__"
        client = self._clients.get(key)
        if client is None:
            kwargs = dict(self._base_kwargs)
            if proxy:
                kwargs["proxies"] = proxy
            client = httpx.Client(**kwargs)
            self._clients[key] = client
        return client

    def close(self) -> None:
        for client in self._clients.values():
            client.close()
        self._clients.clear()
