"""Scrapy item pipeline for batched Bronze persistence and post-crawl Silver refresh."""

from __future__ import annotations

import logging
import time
from typing import Any, Self

from psycopg2.extensions import connection as Connection
from scrapy import Spider
from scrapy.crawler import Crawler
from scrapy.http import Response
from scrapy.logformatter import LogFormatter, LogFormatterResult
from twisted.python.failure import Failure

from src.db import (
    ensure_connection,
    fetch_raw_timestamps,
    get_connection,
    init_db,
    mark_missing_listings_inactive,
    touch_seen_raw_listings,
    upsert_raw_listings_batch,
)
from src.transform import run_transform


class CleanLogFormatter(LogFormatter):
    """Suppress multi-line raw dictionary dumps when items are dropped or scraped."""

    def dropped(
        self,
        item: Any,
        exception: BaseException,
        response: Response | Failure | None,
        spider: Spider,
    ) -> LogFormatterResult:
        return {
            "level": logging.DEBUG,
            "msg": "Skipped %(id)s (%(reason)s)",
            "args": {
                "id": item.get("id") if isinstance(item, dict) else "unknown",
                "reason": str(exception),
            },
        }


class BronzeRawPipeline:
    """Batch-upsert raw RSC payloads into Bronze `raw_listings`, then refresh Silver `apartments`."""

    BATCH_SIZE = 100
    PROGRESS_INTERVAL_SEC = 30.0

    def __init__(self, crawler: Crawler) -> None:
        self.crawler = crawler
        self.conn: Connection | None = None
        self.bronze_saved_count = 0
        self._buffer: list[dict[str, Any]] = []
        self.start_time = 0.0
        self._progress_task: Any = None

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        """Instantiate pipeline with access to the active Scrapy `Crawler`."""
        return cls(crawler)

    def open_spider(self) -> None:
        """Connect to PostgreSQL, initialize Medallion schema, preload Bronze timestamps."""
        # Import LoopingCall lazily after CrawlerProcess has installed AsyncioSelectorReactor
        from twisted.internet.task import LoopingCall

        self.start_time = time.monotonic()
        self.conn = get_connection()
        init_db(self.conn)
        spider = self.crawler.spider
        if spider is not None:
            existing = fetch_raw_timestamps(self.conn)
            spider.existing_timestamps = existing  # type: ignore[attr-defined]
            spider.logger.info(
                "Database ready: %d existing Bronze listings loaded",
                len(existing),
            )
            self._progress_task = LoopingCall(self._log_progress)
            self._progress_task.start(self.PROGRESS_INTERVAL_SEC, now=False)

    def _flush_buffer(self) -> None:
        """Flush buffered Bronze listing payloads into PostgreSQL using batched `execute_values`."""
        if not self._buffer:
            return
        self.conn = ensure_connection(self.conn)
        written = upsert_raw_listings_batch(self.conn, self._buffer)
        self.bronze_saved_count += written
        self._buffer.clear()

    def process_item(self, item: dict[str, Any]) -> dict[str, Any]:
        """Buffer a Bronze raw listing item and flush to PostgreSQL in batches."""
        self._buffer.append(item)
        if len(self._buffer) >= self.BATCH_SIZE:
            self._flush_buffer()
        return item

    def _log_progress(self) -> None:
        """Log a concise single-line progress summary every 30 seconds."""
        spider = self.crawler.spider
        if spider is None:
            return

        crawled = getattr(spider, "crawled_count", 0)
        total = getattr(spider, "queued_count", 0)
        errors_total = sum(getattr(spider, "extract_errors", {}).values())
        engine_paused = bool(self.crawler.engine and self.crawler.engine.paused)

        if engine_paused:
            spider.logger.warning(
                "Progress: PAUSED (waiting for network) | crawled: %d/%d | bronze_saved: %d | extract_issues: %d",
                crawled,
                total,
                self.bronze_saved_count + len(self._buffer),
                errors_total,
            )
            return

        crawl_start = getattr(spider, "crawl_start_time", None)
        paused_sec = getattr(spider, "paused_seconds", 0.0)
        elapsed_sec = (
            max(time.time() - crawl_start - paused_sec, 1.0)
            if crawl_start is not None
            else max(time.monotonic() - self.start_time - paused_sec, 1.0)
        )
        elapsed_min = max(elapsed_sec / 60.0, 1e-6)
        rate = crawled / elapsed_min

        if total > 0:
            pct = min(crawled / total * 100.0, 100.0)
            remaining = max(total - crawled, 0)
            eta_str = f"{int(round(remaining / rate))}m" if rate > 0 else "--"
            spider.logger.info(
                "Progress: %d/%d (%.1f%%) | bronze_saved: %d | extract_issues: %d | speed: %.0f pages/min | ETA: %s",
                crawled,
                total,
                pct,
                self.bronze_saved_count + len(self._buffer),
                errors_total,
                rate,
                eta_str,
            )
        else:
            spider.logger.info(
                "Progress: downloading sitemap... | bronze_saved: %d",
                self.bronze_saved_count + len(self._buffer),
            )

    def close_spider(self) -> None:
        """Flush Bronze buffer, reconcile sitemap lifecycle, refresh Silver, and close DB."""
        if self._progress_task is not None and self._progress_task.running:
            self._progress_task.stop()

        spider = self.crawler.spider
        try:
            self._flush_buffer()

            if spider is not None and self.conn is not None:
                self.conn = ensure_connection(self.conn)
                run_started_at = getattr(spider, "run_started_at", None)
                seen_unchanged = getattr(spider, "seen_unchanged_ids", [])

                touched = 0
                if seen_unchanged and run_started_at is not None:
                    touched = touch_seen_raw_listings(self.conn, seen_unchanged, run_started_at)

                # Lifecycle deactivation + Silver pruning only on healthy full crawls (no --limit)
                is_full_crawl = getattr(spider, "limit", None) is None
                sitemap_total = getattr(spider, "queued_count", 0) + getattr(
                    spider, "unchanged_count", 0
                )
                existing_total = len(getattr(spider, "existing_timestamps", {}))
                healthy_sitemap = sitemap_total > 0 and (
                    existing_total <= 100 or sitemap_total >= int(existing_total * 0.2)
                )

                deactivated = 0
                if is_full_crawl and run_started_at is not None:
                    if healthy_sitemap:
                        deactivated = mark_missing_listings_inactive(self.conn, run_started_at)
                    else:
                        spider.logger.warning(
                            "Skipping inactive deactivation & Silver prune: sitemap returned only %d entries vs %d existing Bronze rows",
                            sitemap_total,
                            existing_total,
                        )

                elapsed = int(round(time.monotonic() - self.start_time))
                minutes, seconds = divmod(elapsed, 60)
                crawled = getattr(spider, "crawled_count", 0)
                extract_errors = getattr(spider, "extract_errors", {})
                err_str = ", ".join(f"{k}={v}" for k, v in extract_errors.items())
                err_suffix = f" | extract_issues: ({err_str})" if err_str else ""

                spider.logger.info(
                    "Bronze crawl finished in %dm %02ds | crawled: %d | bronze_saved: %d | untouched_seen: %d | deactivated: %d%s",
                    minutes,
                    seconds,
                    crawled,
                    self.bronze_saved_count,
                    touched,
                    deactivated,
                    err_suffix,
                )

                if not getattr(spider, "bronze_only", False):
                    exclude_reserved = bool(getattr(spider, "exclude_reserved", False))
                    run_transform(
                        self.conn,
                        exclude_reserved=exclude_reserved,
                        prune=(is_full_crawl and healthy_sitemap),
                        log=spider.logger,
                    )
        finally:
            if self.conn is not None and not self.conn.closed:
                self.conn.close()


# Backwards-compatible alias for legacy Scrapy settings or scripts
PostgresPipeline = BronzeRawPipeline
