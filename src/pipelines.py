"""Scrapy item pipeline for Pydantic validation and PostgreSQL persistence."""

from typing import Any, Self

from psycopg2.extensions import connection as Connection
from pydantic import ValidationError
from scrapy.crawler import Crawler
from scrapy.exceptions import DropItem

from src.db import fetch_existing_timestamps, get_connection, init_db, upsert_apartment
from src.models import ApartmentModel


class PostgresPipeline:
    """Validate scraped apartment items with `ApartmentModel` and upsert them into PostgreSQL."""

    def __init__(self, crawler: Crawler) -> None:
        self.crawler = crawler
        self.conn: Connection | None = None

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        """Instantiate pipeline with access to the active Scrapy `Crawler`."""
        return cls(crawler)

    def open_spider(self) -> None:
        """Connect to PostgreSQL, initialize schema, and preload known listing timestamps."""
        self.conn = get_connection()
        init_db(self.conn)
        spider = self.crawler.spider
        if spider is not None:
            # Preload existing timestamps before SitemapSpider downloads and filters XML sitemaps
            spider.existing_timestamps = fetch_existing_timestamps(self.conn)
            spider.logger.info(
                "Loaded %d existing apartments from DB",
                len(spider.existing_timestamps),
            )

    def process_item(self, item: dict[str, Any]) -> dict[str, Any]:
        """Validate raw item dictionary via `ApartmentModel` and upsert to PostgreSQL."""
        try:
            validated = ApartmentModel(**item).model_dump()
        except ValidationError as exc:
            raise DropItem(f"Rejected apartment {item.get('id')}: {exc}") from None

        if self.conn is not None:
            upsert_apartment(self.conn, validated)
        return validated

    def close_spider(self) -> None:
        """Close the PostgreSQL connection cleanly when the crawl finishes."""
        if self.conn is not None and not self.conn.closed:
            self.conn.close()
