"""Scrapy SitemapSpider for incremental scraping of Slovak apartment sales."""

import argparse
import logging
import socket
from datetime import datetime
from typing import Any, Iterator, Self

from scrapy.crawler import Crawler, CrawlerProcess
from scrapy.downloadermiddlewares.retry import RetryMiddleware
from scrapy.http import Request, Response
from scrapy.spiders import SitemapSpider
from twisted.internet import reactor
from twisted.internet.task import deferLater
from twisted.internet.threads import deferToThread

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

# Internal Scrapy loggers that dump verbose startup/shutdown dictionaries at INFO level
NOISY_SCRAPY_LOGGERS = (
    "scrapy.addons",
    "scrapy.crawler",
    "scrapy.core.engine",
    "scrapy.extensions",
    "scrapy.middleware",
    "scrapy.statscollectors",
    "scrapy.utils.log",
)


def _is_host_reachable(
    host: str = "www.nehnutelnosti.sk",
    port: int = 443,
    timeout: float = 5.0,
) -> bool:
    """Check TCP connectivity to the target host from a background thread."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class NetworkResilientRetryMiddleware(RetryMiddleware):
    """Pause the Scrapy engine during internet outages so queued URLs are not lost."""

    OUTAGE_THRESHOLD = 3
    CHECK_INTERVAL_SEC = 20.0

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self.crawler: Crawler | None = None
        self._consecutive_errors = 0
        self._paused = False

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        mw = cls(crawler.settings)
        mw.crawler = crawler
        return mw

    def process_response(
        self,
        request: Request,
        response: Response,
        spider: Any = None,
    ) -> Request | Response:
        if response.status < 500 and response.status not in self.retry_http_codes:
            self._consecutive_errors = 0
        return super().process_response(request, response, spider)

    def process_exception(
        self,
        request: Request,
        exception: Exception,
        spider: Any = None,
    ) -> Request | Response | None:
        if isinstance(exception, self.exceptions_to_retry):
            self._consecutive_errors += 1
            active_spider = spider or (self.crawler.spider if self.crawler else None)
            is_sitemap_phase = getattr(active_spider, "queued_count", 0) == 0

            if (
                self._paused
                or self._consecutive_errors >= self.OUTAGE_THRESHOLD
                or is_sitemap_phase
            ):
                if not self._paused:
                    self._paused = True
                    if self.crawler and self.crawler.engine:
                        self.crawler.engine.pause()
                    log = (
                        active_spider.logger
                        if active_spider is not None
                        else logging.getLogger(ApartmentSitemapSpider.name)
                    )
                    log.warning(
                        "Network outage detected (%s). Pausing spider and waiting for connection...",
                        exception.__class__.__name__,
                    )
                    self._schedule_recovery_check()

                # Re-queue request without burning its retry counter while offline
                retry_req = request.copy()
                retry_req.dont_filter = True
                return retry_req

        return super().process_exception(request, exception, spider)

    def _schedule_recovery_check(self) -> None:
        """Schedule a non-blocking TCP connectivity check after `CHECK_INTERVAL_SEC`."""
        d = deferLater(reactor, self.CHECK_INTERVAL_SEC, deferToThread, _is_host_reachable)
        d.addCallback(self._on_check_complete)

    def _on_check_complete(self, reachable: bool) -> None:
        active_spider = self.crawler.spider if self.crawler else None
        log = (
            active_spider.logger
            if active_spider is not None
            else logging.getLogger(ApartmentSitemapSpider.name)
        )
        if reachable:
            self._consecutive_errors = 0
            self._paused = False
            if self.crawler and self.crawler.engine and self.crawler.engine.paused:
                self.crawler.engine.unpause()
            log.info("Network connection restored, resuming crawl")
        elif self._paused and self.crawler and self.crawler.engine and self.crawler.engine.running:
            self._schedule_recovery_check()


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
        "LOG_FORMAT": "%(asctime)s | %(levelname)-7s | %(message)s",
        "LOG_DATEFORMAT": "%Y-%m-%d %H:%M:%S",
        "LOG_FORMATTER": "src.pipelines.CleanLogFormatter",
        "RESPONSE_MAX_ACTIVE_SIZE": 0,
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
        "LOG_FORMAT": "%(asctime)s | %(levelname)-7s | %(message)s",
        "LOG_DATEFORMAT": "%Y-%m-%d %H:%M:%S",
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


def silence_noisy_loggers(log_level: str) -> None:
    """Suppress Scrapy's verbose startup and shutdown dictionary dumps unless DEBUG is enabled."""
    if log_level != "DEBUG":
        for name in NOISY_SCRAPY_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)


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
    scrapy_settings = build_scrapy_settings(
        speed=args.speed,
        workers=args.workers,
        log_level=args.log_level,
    )
    silence_noisy_loggers(args.log_level)
    process = CrawlerProcess(settings=scrapy_settings)
    logging.getLogger(ApartmentSitemapSpider.name).info(
        "Starting spider (speed=%s, workers=%d, limit=%s)",
        args.speed,
        scrapy_settings["CONCURRENT_REQUESTS"],
        args.limit,
    )
    process.crawl(ApartmentSitemapSpider, limit=args.limit)
    process.start()
