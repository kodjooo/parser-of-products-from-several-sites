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
        for site in self.context.sites:
            if self.writer:
                self.writer.prepare_site(site)

                def flush(chunk: list[ProductRecord], site_config=site, writer=self.writer):
                    writer.append_site_records(site_config, chunk)

                flush_callback = flush
            else:
                flush_callback = None
            crawler = SiteCrawler(
                self.context,
                site,
                flush_pages=self.context.flush_page_interval,
                flush_callback=flush_callback,
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
        return results
