"""Scrapy SitemapSpider for incremental scraping of Slovak apartment sales."""

import argparse
import logging
import sys
import time
from datetime import datetime
from typing import Any, Iterator, Self

from scrapy.crawler import Crawler, CrawlerProcess
from scrapy.downloadermiddlewares.retry import RetryMiddleware
from scrapy.http import Request, Response
from scrapy.spiders import SitemapSpider

from src.parser import parse_apartment_html

# Predefined concurrency and throttling profiles selectable via `--speed`
SPEED_PROFILES: dict[str, dict[str, Any]] = {
    "slow": {
        "CONCURRENT_REQUESTS": 4,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 4,
        "DOWNLOAD_DELAY": 0.8,
        "DOWNLOAD_DELAY_JITTER": 0.5,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": 0.8,
        "AUTOTHROTTLE_MAX_DELAY": 4.0,
        "AUTOTHROTTLE_TARGET_CONCURRENCY": 2.0,
    },
    "normal": {
        "CONCURRENT_REQUESTS": 6,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 6,
        "DOWNLOAD_DELAY": 0.4,
        "DOWNLOAD_DELAY_JITTER": 0.5,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": 0.4,
        "AUTOTHROTTLE_MAX_DELAY": 3.0,
        "AUTOTHROTTLE_TARGET_CONCURRENCY": 4.0,
    },
    "fast": {
        "CONCURRENT_REQUESTS": 12,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 12,
        "DOWNLOAD_DELAY": 0.15,
        "DOWNLOAD_DELAY_JITTER": 0.5,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": 0.2,
        "AUTOTHROTTLE_MAX_DELAY": 2.0,
        "AUTOTHROTTLE_TARGET_CONCURRENCY": 8.0,
    },
    "turbo": {
        "CONCURRENT_REQUESTS": 24,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 24,
        "DOWNLOAD_DELAY": 0.0,
        "AUTOTHROTTLE_ENABLED": False,
    },
}


class NetworkResilientRetryMiddleware(RetryMiddleware):
    """Pause the Scrapy engine for a progressive cooldown window on network/timeout errors."""

    OUTAGE_THRESHOLD = 3
    BASE_COOLDOWN_SEC = 30.0
    MAX_COOLDOWN_SEC = 240.0

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self._consecutive_errors = 0
        self._consecutive_pauses = 0
        self._paused = False
        self._pause_epoch = 0
        self._pause_started_at: float | None = None

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        mw = cls(crawler.settings)
        mw.crawler = crawler
        return mw

    def process_request(self, request: Request) -> None:
        """Stamp each request with the current pause epoch and set page timeout."""
        request.meta["_sent_epoch"] = self._pause_epoch
        if "/detail/" in request.url:
            request.meta.setdefault("download_timeout", 20)

    def process_response(
        self,
        request: Request,
        response: Response,
    ) -> Request | Response:
        if response.status < 500 and response.status not in self.retry_http_codes:
            self._consecutive_errors = 0
            self._consecutive_pauses = 0
        return super().process_response(request, response)

    def process_exception(
        self,
        request: Request,
        exception: Exception,
    ) -> Request | Response | None:
        if isinstance(exception, self.exceptions_to_retry):
            req_epoch = request.meta.get("_sent_epoch", self._pause_epoch)

            # Requests sent before the most recent pause belong to an older batch;
            # re-queue them without counting toward a new pause after resume.
            if self._paused or req_epoch < self._pause_epoch:
                retry_req = request.copy()
                retry_req.dont_filter = True
                return retry_req

            self._consecutive_errors += 1
            active_spider = self.crawler.spider if self.crawler else None
            is_sitemap_phase = getattr(active_spider, "queued_count", 0) == 0

            if self._consecutive_errors >= self.OUTAGE_THRESHOLD or is_sitemap_phase:
                self._paused = True
                self._pause_epoch += 1
                self._consecutive_pauses += 1
                self._pause_started_at = time.time()

                cooldown_sec = min(
                    self.BASE_COOLDOWN_SEC * (2 ** min(self._consecutive_pauses - 1, 3)),
                    self.MAX_COOLDOWN_SEC,
                )

                if self.crawler and self.crawler.engine:
                    self.crawler.engine.pause()
                log = (
                    active_spider.logger
                    if active_spider is not None
                    else logging.getLogger(ApartmentSitemapSpider.name)
                )
                log.warning(
                    "Network timeout/error (%s). Pausing spider for %.0fs cooldown...",
                    exception.__class__.__name__,
                    cooldown_sec,
                )
                from twisted.internet import reactor

                reactor.callLater(cooldown_sec, self._resume_crawl)

                retry_req = request.copy()
                retry_req.dont_filter = True
                return retry_req

        return super().process_exception(request, exception)

    def _resume_crawl(self) -> None:
        """Unpause the Scrapy engine after the cooldown period expires."""
        active_spider = self.crawler.spider if self.crawler else None
        if self._pause_started_at is not None and active_spider is not None:
            active_spider.paused_seconds = getattr(active_spider, "paused_seconds", 0.0) + (
                time.time() - self._pause_started_at
            )
        self._pause_started_at = None
        self._consecutive_errors = 0
        self._paused = False
        try:
            from scrapy.resolver import dnscache

            dnscache._cache.clear()
        except Exception:
            pass
        log = (
            active_spider.logger
            if active_spider is not None
            else logging.getLogger(ApartmentSitemapSpider.name)
        )
        if self.crawler and self.crawler.engine and self.crawler.engine.paused:
            self.crawler.engine.unpause()
            slot = getattr(self.crawler.engine, "_slot", None)
            if slot is not None and getattr(slot, "nextcall", None) is not None:
                slot.nextcall.schedule()
        log.info("Cooldown complete, resuming crawl")


class ApartmentSitemapSpider(SitemapSpider):
    """Crawl apartment sale listings from Nehnutelnosti.sk XML sitemaps incrementally."""

    name = "nehnutelnosti_apartments"
    allowed_domains = ["nehnutelnosti.sk"]

    sitemap_urls = [
        "https://www.nehnutelnosti.sk/sitemap-listings-byty-predaj-slovensko.xml",
    ]
    sitemap_rules = [
        (r"/detail/", "parse_apartment"),
    ]

    custom_settings = {
        "USER_AGENT": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "DEFAULT_REQUEST_HEADERS": {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "sk-SK,sk;q=0.9,en-US;q=0.8,en;q=0.7",
        },
        "ROBOTSTXT_OBEY": False,
        "COOKIES_ENABLED": False,
        "TELNETCONSOLE_ENABLED": False,
        "LOGSTATS_INTERVAL": 0,
        "LOG_FORMATTER": "src.pipelines.CleanLogFormatter",
        "RESPONSE_MAX_ACTIVE_SIZE": 0,
        "DOWNLOAD_FAIL_ON_DATALOSS": False,
        "DOWNLOAD_TIMEOUT": 90,
        "RETRY_ENABLED": True,
        "RETRY_TIMES": 5,
        "RETRY_HTTP_CODES": [408, 429, 500, 502, 503, 504, 522, 524],
        "DOWNLOADER_MIDDLEWARES": {
            "scrapy.downloadermiddlewares.retry.RetryMiddleware": None,
            "src.spider.NetworkResilientRetryMiddleware": 550,
        },
        "ITEM_PIPELINES": {
            "src.pipelines.PostgresPipeline": 300,
        },
    }

    def __init__(self, limit: int | None = None, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.limit = int(limit) if limit is not None else None
        # Populated by PostgresPipeline.open_spider() before sitemap parsing begins
        self.existing_timestamps: dict[str, datetime | None] = {}
        self.queued_count = 0
        self.unchanged_count = 0
        self.crawled_count = 0
        self.crawl_start_time: float | None = None
        self.paused_seconds: float = 0.0

    def sitemap_filter(self, entries: Iterator[dict[str, Any]]) -> Iterator[dict[str, Any]]:
        """Yield only new or modified sitemap entries based on `<lastmod>` vs DB `updated_at`."""
        for entry in entries:
            if self.limit is not None and self.queued_count >= self.limit:
                break

            loc = entry.get("loc", "")
            ad_id = extract_ad_id_from_url(loc)
            lastmod_raw = entry.get("lastmod")

            # Skip HTTP download if listing is already in DB with an equal or newer timestamp
            if ad_id and ad_id in self.existing_timestamps and lastmod_raw:
                db_dt = self.existing_timestamps[ad_id]
                if db_dt is not None:
                    try:
                        sitemap_dt = datetime.fromisoformat(lastmod_raw)
                        if int(sitemap_dt.timestamp()) <= int(db_dt.timestamp()):
                            self.unchanged_count += 1
                            continue
                    except ValueError:
                        pass

            self.queued_count += 1
            yield entry

        self.crawl_start_time = datetime.now().timestamp()
        self.logger.info(
            "Sitemap filtered: %d new/updated URLs queued (%d unchanged skipped)",
            self.queued_count,
            self.unchanged_count,
        )

    def parse_apartment(self, response: Response) -> list[dict[str, Any]]:
        """Extract raw apartment dictionary from detail page HTML."""
        self.crawled_count += 1
        data = parse_apartment_html(response.text, url=response.url)
        return [data] if data is not None else []


def build_scrapy_settings(speed: str, workers: int | None, log_level: str) -> dict[str, Any]:
    """Build Scrapy settings dictionary from the chosen speed profile and worker override."""
    settings: dict[str, Any] = {
        **SPEED_PROFILES[speed],
        "LOG_LEVEL": log_level,
        "LOG_INSTALL_ROOT_HANDLER": False,
        "LOG_FORMATTER": "src.pipelines.CleanLogFormatter",
        "TELNETCONSOLE_ENABLED": False,
        "LOGSTATS_INTERVAL": 0,
    }
    if workers is not None and workers > 0:
        settings["CONCURRENT_REQUESTS"] = workers
        settings["CONCURRENT_REQUESTS_PER_DOMAIN"] = workers
        if settings.get("AUTOTHROTTLE_ENABLED"):
            settings["AUTOTHROTTLE_TARGET_CONCURRENCY"] = float(workers)
    return settings


def setup_clean_logging(log_level: str) -> None:
    """Configure a single root stream handler that filters out Scrapy's internal INFO dumps."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-7s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    if log_level != "DEBUG":
        handler.addFilter(
            lambda record: not (
                record.name.startswith("scrapy") and record.levelno < logging.WARNING
            )
        )
    logging.root.handlers.clear()
    logging.root.addHandler(handler)
    logging.root.setLevel(getattr(logging, log_level, logging.INFO))


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for crawl limit, concurrency speed profile, and logging verbosity."""
    parser = argparse.ArgumentParser(
        description="Scrape Slovak apartment sale listings from Nehnutelnosti.sk into PostgreSQL."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of new/updated apartments to scrape (default: no limit).",
    )
    parser.add_argument(
        "--speed",
        choices=["slow", "normal", "fast", "turbo"],
        default="normal",
        help="Concurrency preset: slow (4), normal (6, default), fast (12), turbo (24, no delay).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Override number of concurrent requests (e.g. --workers 24).",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Scrapy logging level (default: INFO).",
    )
    return parser.parse_args()


def extract_ad_id_from_url(url: str) -> str | None:
    """Extract the unique listing ID (`Ju...`) from a `/detail/<id>/<slug>` URL."""
    if "/detail/" not in url:
        return None
    part = url.split("/detail/", 1)[1]
    ad_id = part.split("/", 1)[0].strip()
    return ad_id or None


if __name__ == "__main__":
    args = parse_args()
    setup_clean_logging(args.log_level)
    scrapy_settings = build_scrapy_settings(
        speed=args.speed,
        workers=args.workers,
        log_level=args.log_level,
    )
    process = CrawlerProcess(settings=scrapy_settings)
    logging.getLogger(ApartmentSitemapSpider.name).info(
        "Starting spider (speed=%s, workers=%d, limit=%s)",
        args.speed,
        scrapy_settings["CONCURRENT_REQUESTS"],
        args.limit,
    )
    process.crawl(ApartmentSitemapSpider, limit=args.limit)
    process.start()
