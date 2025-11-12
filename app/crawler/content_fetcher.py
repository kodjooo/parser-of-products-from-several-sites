from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from unidecode import unidecode

from app.config.models import NetworkConfig
from app.crawler.utils import pick_user_agent
from app.logger import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class ProductContent:
    text_content: str | None = None
    image_url: str | None = None
    image_path: str | None = None


class ProductContentFetcher:
    """Загружает страницу товара, извлекает текст и сохраняет главное изображение."""

    def __init__(self, network: NetworkConfig, image_dir: Path):
        self.network = network
        self.image_dir = image_dir
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.client = httpx.Client(
            timeout=network.request_timeout_sec,
            follow_redirects=True,
        )

    def fetch(self, product_url: str, image_selector: str | None = None) -> ProductContent:
        try:
            response = self.client.get(
                product_url,
                headers={"User-Agent": pick_user_agent(self.network)},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning(
                "Не удалось загрузить страницу товара",
                extra={"url": product_url, "error": str(exc)},
            )
            return ProductContent()

        html = response.text
        soup = BeautifulSoup(html, "lxml")
        text_content = _extract_text_content(soup)
        image_url = None
        if image_selector:
            node = soup.select_one(image_selector)
            if node:
                src = node.get("src") or node.get("data-src")
                if src:
                    image_url = urljoin(product_url, src)
        if not image_url:
            image_url = _extract_main_image_url(soup, product_url)
        title = _extract_title(soup)

        image_path = None
        if image_url:
            image_path = self._download_image(image_url, title or "product", product_url)

        return ProductContent(
            text_content=text_content,
            image_url=image_url,
            image_path=image_path,
        )

    def _download_image(
        self,
        url: str,
        title: str,
        fallback_id: str,
    ) -> str | None:
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
        slug = _slugify(title) or hashlib.md5(fallback_id.encode(), usedforsecurity=False).hexdigest()
        filename = f"{slug}.{extension}"
        path = self.image_dir / filename

        # Если файл уже существует, добавим хвост
        if path.exists():
            suffix = hashlib.md5(url.encode(), usedforsecurity=False).hexdigest()[:6]
            path = self.image_dir / f"{slug}-{suffix}.{extension}"

        path.write_bytes(response.content)
        logger.info("Сохранено изображение товара", extra={"path": str(path)})
        return str(path)

    def close(self) -> None:
        self.client.close()


def _extract_text_content(soup: BeautifulSoup) -> str | None:
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    text = " ".join(soup.get_text(separator=" ", strip=True).split())
    return text or None


def _extract_title(soup: BeautifulSoup) -> str | None:
    meta = soup.find("meta", attrs={"property": "og:title"})
    if meta and meta.get("content"):
        return meta["content"].strip()
    title_tag = soup.find("title")
    if title_tag and title_tag.text:
        return title_tag.text.strip()
    h1 = soup.find("h1")
    if h1 and h1.text:
        return h1.text.strip()
    return None


def _extract_main_image_url(soup: BeautifulSoup, base_url: str) -> str | None:
    # 1) og:image
    meta_og = soup.find("meta", attrs={"property": "og:image"})
    if meta_og and meta_og.get("content"):
        return urljoin(base_url, meta_og["content"])

    # 2) data-zoom/data-large attributes
    for attr in ("data-zoom-image", "data-large_image", "data-src", "data-large-src"):
        node = soup.find(attrs={attr: True})
        if node:
            return urljoin(base_url, node.get(attr))

    # 3) srcset с максимальным дескриптором
    for img in soup.find_all("img"):
        srcset = img.get("srcset")
        if srcset:
            candidates = [
                part.strip().split(" ")
                for part in srcset.split(",")
                if part.strip()
            ]
            best = None
            best_width = -1
            for candidate in candidates:
                url_part = candidate[0]
                width = 0
                if len(candidate) > 1 and candidate[1].endswith("w"):
                    try:
                        width = int(candidate[1][:-1])
                    except ValueError:
                        width = 0
                if width > best_width:
                    best_width = width
                    best = url_part
            if best:
                return urljoin(base_url, best)

    # 4) первый img с src
    img = soup.find("img", src=True)
    if img:
        return urljoin(base_url, img["src"])

    return None


def _guess_extension(url: str, content_type: Optional[str]) -> str:
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
    ascii_value = unidecode(value)
    clean = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in ascii_value.lower())
    clean = "-".join(filter(None, clean.split("-")))
    return clean[:80]
