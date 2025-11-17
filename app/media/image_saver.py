from __future__ import annotations

import hashlib
import os
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.config.models import NetworkConfig
from app.crawler.utils import pick_user_agent
from app.logger import get_logger

logger = get_logger(__name__)


class ImageSaver:
    """Отвечает за сохранение изображений товаров в локальную директорию."""

    def __init__(self, network: NetworkConfig, image_dir: Path):
        self.network = network
        self.image_dir = image_dir
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.client = httpx.Client(
            timeout=network.request_timeout_sec,
            follow_redirects=True,
        )

    def save(self, url: str, title: str | None, fallback_id: str) -> str | None:
        if not url:
            return None
        try:
            response = self.client.get(
                url,
                headers={"User-Agent": pick_user_agent(self.network)},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning(
                "Не удалось скачать изображение",
                extra={"url": url, "error": str(exc)},
            )
            return None

        extension = _guess_extension(url, response.headers.get("content-type"))
        slug_source = title or "product"
        slug = _slugify(slug_source) or hashlib.md5(fallback_id.encode(), usedforsecurity=False).hexdigest()
        filename = f"{slug}.{extension}"
        path = self.image_dir / filename

        if path.exists():
            suffix = hashlib.md5(url.encode(), usedforsecurity=False).hexdigest()[:6]
            path = self.image_dir / f"{slug}-{suffix}.{extension}"

        path.write_bytes(response.content)
        logger.info("Сохранено изображение товара", extra={"path": str(path)})
        return str(path)

    def close(self) -> None:
        self.client.close()


def _guess_extension(url: str, content_type: str | None) -> str:
    if content_type:
        if "png" in content_type:
            return "png"
        if "gif" in content_type:
            return "gif"
    parsed = urlparse(url)
    ext = os.path.splitext(parsed.path)[1].lower().strip(".")
    if ext in {"jpg", "jpeg", "png", "gif", "webp"}:
        return "jpg" if ext == "jpeg" else ext
    return "jpg"


def _slugify(value: str) -> str:
    from unidecode import unidecode

    ascii_value = unidecode(value)
    clean = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in ascii_value.lower())
    clean = "-".join(filter(None, clean.split("-")))
    return clean[:80]
