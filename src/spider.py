import argparse
from datetime import datetime
from typing import Any, Iterator

from scrapy.crawler import CrawlerProcess
from scrapy.http import Response
from scrapy.spiders import SitemapSpider

from src.parser import parse_apartment_html


class ApartmentSitemapSpider(SitemapSpider):
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
        "ROBOTSTXT_OBEY": False,
        "CONCURRENT_REQUESTS": 8,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 4,
        "DOWNLOAD_DELAY": 0.5,
        "DOWNLOAD_DELAY_JITTER": 0.5,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": 0.5,
        "AUTOTHROTTLE_MAX_DELAY": 10.0,
        "AUTOTHROTTLE_TARGET_CONCURRENCY": 2.0,
        "RETRY_ENABLED": True,
        "RETRY_TIMES": 3,
        "ITEM_PIPELINES": {
            "src.pipelines.PostgresPipeline": 300,
        },
        "LOG_LEVEL": "INFO",
    }

    def __init__(self, limit: int | None = None, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.limit = int(limit) if limit is not None else None
        self.existing_timestamps: dict[str, datetime | None] = {}
        self._queued_count = 0

    def sitemap_filter(self, entries: Iterator[dict[str, Any]]) -> Iterator[dict[str, Any]]:
        for entry in entries:
            if self.limit is not None and self._queued_count >= self.limit:
                break

            loc = entry.get("loc", "")
            ad_id = extract_ad_id_from_url(loc)
            lastmod_raw = entry.get("lastmod")

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
        data = parse_apartment_html(response.text, url=response.url)
        if data is not None:
            yield data


def parse_args() -> argparse.Namespace:
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
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Scrapy logging level (default: INFO).",
    )
    return parser.parse_args()


def extract_ad_id_from_url(url: str) -> str | None:
    if "/detail/" not in url:
        return None
    part = url.split("/detail/", 1)[1]
    ad_id = part.split("/", 1)[0].strip()
    return ad_id or None


if __name__ == "__main__":
    args = parse_args()
    process = CrawlerProcess(settings={"LOG_LEVEL": args.log_level})
    process.crawl(ApartmentSitemapSpider, limit=args.limit)
    process.start()
