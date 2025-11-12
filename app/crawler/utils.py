from __future__ import annotations

import hashlib
import random
import time
from fnmatch import fnmatch
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from app.config.models import DedupeConfig, NetworkConfig


def normalize_url(
    raw_url: str,
    base_url: str | None,
    strip_params: Iterable[str],
) -> tuple[str, str]:
    """Возвращает нормализованный URL и md5-хэш."""
    absolute = urljoin(base_url or "", raw_url)
    parsed = urlparse(absolute)
    filtered_qs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not any(fnmatch(key, pattern) for pattern in strip_params)
    ]
    normalized = urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(filtered_qs),
            "",
        )
    )
    product_hash = hashlib.md5(normalized.encode("utf-8"), usedforsecurity=False).hexdigest()
    return normalized, product_hash


def pick_user_agent(network: NetworkConfig) -> str:
    return random.choice(network.user_agents)


def jitter_sleep(min_delay: float = 0.05, max_delay: float = 0.3) -> None:
    delay = random.uniform(min_delay, max_delay)
    time.sleep(delay)
