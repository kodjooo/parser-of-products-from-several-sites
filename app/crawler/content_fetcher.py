from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from app.config.models import NetworkConfig
from app.crawler.utils import pick_user_agent
from app.media.image_saver import ImageSaver
from app.logger import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class ProductContent:
    text_content: str | None = None
    image_url: str | None = None
    image_path: str | None = None
    title: str | None = None
    name_en: str | None = None
    name_ru: str | None = None
    price_without_discount: str | None = None
    price_with_discount: str | None = None


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
        self.image_saver = ImageSaver(network, image_dir)

    def fetch(
        self,
        product_url: str,
        image_selector: str | None = None,
        drop_after_selectors: Sequence[str] | None = None,
        *,
        download_image: bool = True,
        name_en_selector: str | None = None,
        name_ru_selector: str | None = None,
        price_without_discount_selector: str | None = None,
        price_with_discount_selector: str | Sequence[str] | None = None,
    ) -> ProductContent:
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
        text_content = _extract_text_content(soup, drop_after_selectors)
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
        name_en = _extract_text_by_selector(soup, name_en_selector)
        name_ru = _extract_text_by_selector(soup, name_ru_selector)
        price_wo = _extract_text_by_selector(soup, price_without_discount_selector)
        price_w = _extract_text_by_selector(soup, price_with_discount_selector)

        image_path = None
        if download_image and image_url:
            image_path = self.image_saver.save(image_url, title or "product", product_url)

        return ProductContent(
            text_content=text_content,
            image_url=image_url,
            image_path=image_path,
            title=title,
            name_en=name_en,
            name_ru=name_ru,
            price_without_discount=price_wo,
            price_with_discount=price_w,
        )

    def close(self) -> None:
        self.client.close()
        self.image_saver.close()


def _extract_text_content(
    soup: BeautifulSoup, drop_after_selectors: Sequence[str] | None = None
) -> str | None:
    if drop_after_selectors:
        _strip_after_selectors(soup, drop_after_selectors)
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    text = " ".join(soup.get_text(separator=" ", strip=True).split())
    return text or None


def _strip_after_selectors(soup: BeautifulSoup, selectors: Sequence[str]) -> None:
    for selector in selectors:
        if not selector:
            continue
        node = soup.select_one(selector)
        if not node:
            continue
        to_remove = [node] + list(node.find_all_next())
        for target in to_remove:
            target.decompose()


def _extract_text_by_selector(
    soup: BeautifulSoup, selector: str | Sequence[str] | None
) -> str | None:
    if not selector:
        return None
    if isinstance(selector, str):
        selectors = [selector]
    else:
        selectors = [item for item in selector if item]
    for css in selectors:
        node = soup.select_one(css)
        if not node:
            continue
        text = node.get_text(" ", strip=True)
        if text:
            return text
    return None


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
