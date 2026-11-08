"""Scrapy SitemapSpider for incremental scraping of Slovak apartment sales."""

import argparse
from datetime import datetime
from typing import Any, Iterator

from scrapy.crawler import CrawlerProcess
from scrapy.http import Response
from scrapy.spiders import SitemapSpider

from src.parser import parse_apartment_html

# Predefined concurrency and throttling profiles selectable via `--speed`
SPEED_PROFILES: dict[str, dict[str, Any]] = {
    "slow": {
        "CONCURRENT_REQUESTS": 4,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 4,
        "DOWNLOAD_DELAY": 0.8,
        "RANDOMIZE_DOWNLOAD_DELAY": True,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": 0.8,
        "AUTOTHROTTLE_MAX_DELAY": 4.0,
        "AUTOTHROTTLE_TARGET_CONCURRENCY": 2.0,
    },
    "normal": {
        "CONCURRENT_REQUESTS": 6,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 6,
        "DOWNLOAD_DELAY": 0.4,
        "RANDOMIZE_DOWNLOAD_DELAY": True,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": 0.4,
        "AUTOTHROTTLE_MAX_DELAY": 3.0,
        "AUTOTHROTTLE_TARGET_CONCURRENCY": 4.0,
    },
    "fast": {
        "CONCURRENT_REQUESTS": 12,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 12,
        "DOWNLOAD_DELAY": 0.15,
        "RANDOMIZE_DOWNLOAD_DELAY": True,
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
        "DOWNLOAD_TIMEOUT": 90,
        "RETRY_ENABLED": True,
        "RETRY_TIMES": 5,
        "RETRY_HTTP_CODES": [408, 429, 500, 502, 503, 504, 522, 524],
        "ITEM_PIPELINES": {
            "src.pipelines.PostgresPipeline": 300,
        },
    }

    def __init__(self, limit: int | None = None, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.limit = int(limit) if limit is not None else None
        # Populated by PostgresPipeline.open_spider() before sitemap parsing begins
        self.existing_timestamps: dict[str, datetime | None] = {}
        self._queued_count = 0

    def sitemap_filter(self, entries: Iterator[dict[str, Any]]) -> Iterator[dict[str, Any]]:
        """Yield only new or modified sitemap entries based on `<lastmod>` vs DB `updated_at`."""
        for entry in entries:
            if self.limit is not None and self._queued_count >= self.limit:
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
                            continue
                    except ValueError:
                        pass

            self._queued_count += 1
            yield entry

    def parse_apartment(self, response: Response) -> Iterator[dict[str, Any]]:
        """Extract raw apartment dictionary from detail page HTML."""
        data = parse_apartment_html(response.text, url=response.url)
        if data is not None:
            yield data


def build_scrapy_settings(speed: str, workers: int | None, log_level: str) -> dict[str, Any]:
    """Build Scrapy settings dictionary from the chosen speed profile and worker override."""
    settings: dict[str, Any] = {
        **SPEED_PROFILES[speed],
        "LOG_LEVEL": log_level,
    }
    if workers is not None and workers > 0:
        settings["CONCURRENT_REQUESTS"] = workers
        settings["CONCURRENT_REQUESTS_PER_DOMAIN"] = workers
        if settings.get("AUTOTHROTTLE_ENABLED"):
            settings["AUTOTHROTTLE_TARGET_CONCURRENCY"] = float(workers)
    return settings


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
    process = CrawlerProcess(settings=scrapy_settings)
    process.crawl(ApartmentSitemapSpider, limit=args.limit)
    process.start()
