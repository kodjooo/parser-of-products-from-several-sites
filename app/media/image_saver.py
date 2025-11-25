from __future__ import annotations

import hashlib
import os
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.config.models import NetworkConfig
from app.crawler.engines import ProxyPool, ProxyExhaustedError
from app.crawler.utils import pick_user_agent
from app.logger import get_logger

logger = get_logger(__name__)


class ImageSaver:
    """Отвечает за сохранение изображений товаров в локальную директорию."""

    def __init__(self, network: NetworkConfig, image_dir: Path, proxy_pool: ProxyPool | None = None):
        self.network = network
        self.image_dir = image_dir
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.client = httpx.Client(
            timeout=network.request_timeout_sec,
            follow_redirects=True,
        )
        self._proxy_pool = proxy_pool

    def save(self, url: str, title: str | None, fallback_id: str, proxy: str | None = None) -> str | None:
        if not url:
            return None
        try:
            proxy_to_use = proxy
            if proxy_to_use is None and self._proxy_pool:
                try:
                    proxy_to_use = self._proxy_pool.pick()
                except ProxyExhaustedError:
                    logger.error("Прокси-пул исчерпан для загрузки изображения", extra={"url": url})
                    proxy_to_use = None
            response = self.client.get(
                url,
                headers={"User-Agent": pick_user_agent(self.network)},
                proxies=proxy_to_use,
            )
            response.raise_for_status()
            logger.debug("Image download via httpx url=%s proxy=%s", url, proxy_to_use)
        except httpx.HTTPError as exc:
            logger.warning(
                "Не удалось скачать изображение",
                extra={"url": url, "error": str(exc)},
            )
            return None

        return self._write_file(
            url=url,
            title=title,
            fallback_id=fallback_id,
            content=response.content,
            content_type=response.headers.get("content-type"),
        )

    def save_from_content(
        self,
        url: str,
        title: str | None,
        fallback_id: str,
        content: bytes,
        content_type: str | None = None,
    ) -> str | None:
        if not content:
            return None
        logger.debug("Image download via Playwright url=%s", url)
        return self._write_file(
            url=url,
            title=title,
            fallback_id=fallback_id,
            content=content,
            content_type=content_type,
        )

    def close(self) -> None:
        self.client.close()

    def _write_file(
        self,
        *,
        url: str,
        title: str | None,
        fallback_id: str,
        content: bytes,
        content_type: str | None,
    ) -> str | None:
        extension = _guess_extension(url, content_type)
        slug_source = title or "product"
        slug = _slugify(slug_source) or hashlib.md5(fallback_id.encode(), usedforsecurity=False).hexdigest()
        filename = f"{slug}.{extension}"
        path = self.image_dir / filename

        if path.exists():
            suffix = hashlib.md5(url.encode(), usedforsecurity=False).hexdigest()[:6]
            path = self.image_dir / f"{slug}-{suffix}.{extension}"

        path.write_bytes(content)
        logger.info("Сохранено изображение товара", extra={"path": str(path)})
        return str(path)

def _guess_extension(url: str, content_type: str | None) -> str:
    if content_type:
        mime = content_type.split(";")[0].strip().lower()
        mapping = {
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/jpg": "jpg",
            "image/gif": "gif",
            "image/webp": "webp",
            "image/avif": "avif",
            "image/svg+xml": "svg",
        }
        if mime in mapping:
            return mapping[mime]
    parsed = urlparse(url)
    ext = os.path.splitext(parsed.path)[1].lower().strip(".")
    if ext in {"jpg", "jpeg", "png", "gif", "webp", "avif", "svg"}:
        return "jpg" if ext == "jpeg" else ext
    return "jpg"


def _slugify(value: str) -> str:
    from unidecode import unidecode

    ascii_value = unidecode(value)
    clean = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in ascii_value.lower())
    clean = "-".join(filter(None, clean.split("-")))
    return clean[:80]
