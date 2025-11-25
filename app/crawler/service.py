from __future__ import annotations

from app.crawler.models import ProductRecord, SiteCrawlResult
from app.crawler.site_crawler import SiteCrawler
from app.logger import get_logger
from app.runtime import RuntimeContext
from app.sheets.writer import SheetsWriter

logger = get_logger(__name__)


class CrawlService:
    """Оркестровка обхода всех сайтов."""

    def __init__(self, context: RuntimeContext, writer: SheetsWriter | None = None):
        self.context = context
        self.writer = writer

    def collect(self) -> list[SiteCrawlResult]:
        results: list[SiteCrawlResult] = []
        logger.debug("CrawlService writer активен: %s", bool(self.writer))
        for site in self.context.sites:
            if self.context.product_limit_reached():
                logger.info(
                    "Достигнут глобальный лимит по товарам, дальнейший обход остановлен",
                    extra={"limit": self.context.config.runtime.global_stop.stop_after_products},
                )
                break
            if self.writer:
                self.writer.prepare_site(site)
                existing_urls = self.writer.get_existing_urls(site)

                def flush(
                    chunk: list[ProductRecord],
                    site_config=site,
                    writer=self.writer,
                ):
                    logger.debug(
                        "Передаём %s записей в SheetsWriter",
                        len(chunk),
                        extra={"site": site.name},
                    )
                    writer.append_site_records_with_retry(
                        site_config,
                        chunk,
                        max_attempts=2,
                        delay_sec=30.0,
                    )

                flush_callback = flush
                flush_every = 1
            else:
                flush_callback = None
                flush_every = self.context.flush_product_interval
                existing_urls = set()
            crawler = SiteCrawler(
                self.context,
                site,
                flush_products=flush_every,
                flush_callback=flush_callback,
                existing_product_urls=existing_urls,
            )
            result = crawler.crawl()
            logger.info(
                "Завершён обход сайта",
                extra={
                    "site": site.name,
                    "records": len(result.records),
                },
            )
            results.append(result)
            if self.context.product_limit_reached():
                break
        return results
