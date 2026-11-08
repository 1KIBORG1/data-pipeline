"""Scrapy item pipeline for Pydantic validation, progress reporting, and PostgreSQL persistence."""

import logging
import time
from collections import Counter
from typing import Any, Self

from psycopg2.extensions import connection as Connection
from pydantic import ValidationError
from scrapy import Spider
from scrapy.crawler import Crawler
from scrapy.exceptions import DropItem
from scrapy.http import Response
from scrapy.logformatter import LogFormatter
from twisted.internet.task import LoopingCall

from src.db import fetch_existing_timestamps, get_connection, init_db, upsert_apartment
from src.models import ApartmentModel


def _extract_reject_reason(exc: ValidationError) -> str:
    """Extract a short human-readable rejection key (e.g. `'rezervovan'`) from a ValidationError."""
    for err in exc.errors():
        msg = err.get("msg", "")
        if "rejected stem:" in msg:
            return msg.rsplit("rejected stem:", 1)[-1].strip()
        loc = err.get("loc")
        if loc:
            return str(loc[0])
    return "invalid"


class CleanLogFormatter(LogFormatter):
    """Suppress multi-line raw dictionary dumps when items are dropped or scraped."""

    def dropped(
        self,
        item: Any,
        exception: BaseException,
        response: Response,
        spider: Spider,
    ) -> dict[str, Any]:
        return {
            "level": logging.DEBUG,
            "msg": "Skipped %(id)s (%(reason)s)",
            "args": {
                "id": item.get("id") if isinstance(item, dict) else "unknown",
                "reason": str(exception),
            },
        }


class PostgresPipeline:
    """Validate scraped apartment items with `ApartmentModel` and upsert them into PostgreSQL."""

    PROGRESS_INTERVAL_SEC = 30.0

    def __init__(self, crawler: Crawler) -> None:
        self.crawler = crawler
        self.conn: Connection | None = None
        self.saved_count = 0
        self.skipped_count = 0
        self.skipped_reasons: Counter[str] = Counter()
        self.start_time = 0.0
        self._progress_task: LoopingCall | None = None

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        """Instantiate pipeline with access to the active Scrapy `Crawler`."""
        return cls(crawler)

    def open_spider(self) -> None:
        """Connect to PostgreSQL, initialize schema, preload timestamps, and start progress timer."""
        self.start_time = time.monotonic()
        self.conn = get_connection()
        init_db(self.conn)
        spider = self.crawler.spider
        if spider is not None:
            spider.existing_timestamps = fetch_existing_timestamps(self.conn)
            spider.logger.info(
                "Database ready: %d existing apartments loaded",
                len(spider.existing_timestamps),
            )
            self._progress_task = LoopingCall(self._log_progress)
            self._progress_task.start(self.PROGRESS_INTERVAL_SEC, now=False)

    def process_item(self, item: dict[str, Any]) -> dict[str, Any]:
        """Validate raw item dictionary via `ApartmentModel` and upsert to PostgreSQL."""
        try:
            validated = ApartmentModel(**item).model_dump()
        except ValidationError as exc:
            reason = _extract_reject_reason(exc)
            self.skipped_count += 1
            self.skipped_reasons[reason] += 1
            raise DropItem(reason) from None

        if self.conn is not None:
            upsert_apartment(self.conn, validated)
        self.saved_count += 1
        return validated

    def _log_progress(self) -> None:
        """Log a concise single-line progress summary every 30 seconds."""
        spider = self.crawler.spider
        if spider is None:
            return

        crawled = getattr(spider, "crawled_count", 0)
        total = getattr(spider, "queued_count", 0)
        engine_paused = bool(self.crawler.engine and self.crawler.engine.paused)

        if engine_paused:
            spider.logger.warning(
                "Progress: PAUSED (waiting for network) | crawled: %d/%d | saved: %d | skipped: %d",
                crawled,
                total,
                self.saved_count,
                self.skipped_count,
            )
            return

        crawl_start = getattr(spider, "crawl_start_time", None)
        elapsed_sec = (
            time.time() - crawl_start
            if crawl_start is not None
            else time.monotonic() - self.start_time
        )
        elapsed_min = max(elapsed_sec / 60.0, 1e-6)
        rate = crawled / elapsed_min

        if total > 0:
            pct = min(crawled / total * 100.0, 100.0)
            remaining = max(total - crawled, 0)
            eta_str = f"{int(round(remaining / rate))}m" if rate > 0 else "--"
            spider.logger.info(
                "Progress: %d/%d (%.1f%%) | saved: %d | skipped: %d | speed: %.0f pages/min | ETA: %s",
                crawled,
                total,
                pct,
                self.saved_count,
                self.skipped_count,
                rate,
                eta_str,
            )
        else:
            spider.logger.info(
                "Progress: downloading sitemap... | saved: %d | skipped: %d",
                self.saved_count,
                self.skipped_count,
            )

    def close_spider(self) -> None:
        """Stop progress timer, log final run statistics, and close PostgreSQL connection."""
        if self._progress_task is not None and self._progress_task.running:
            self._progress_task.stop()

        spider = self.crawler.spider
        if spider is not None:
            elapsed = int(round(time.monotonic() - self.start_time))
            minutes, seconds = divmod(elapsed, 60)
            crawled = getattr(spider, "crawled_count", 0)
            reasons_str = ", ".join(
                f"{k}={v}" for k, v in self.skipped_reasons.most_common()
            )
            reasons_suffix = f" ({reasons_str})" if reasons_str else ""
            spider.logger.info(
                "Crawl finished in %dm %02ds | crawled: %d | saved: %d | skipped: %d%s",
                minutes,
                seconds,
                crawled,
                self.saved_count,
                self.skipped_count,
                reasons_suffix,
            )

        if self.conn is not None and not self.conn.closed:
            self.conn.close()
