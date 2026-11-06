from typing import Any, Self
from pydantic import ValidationError
from psycopg2.extensions import connection as Connection
from scrapy.crawler import Crawler
from scrapy.exceptions import DropItem
from src.db import fetch_existing_timestamps, get_connection, init_db, upsert_apartment
from src.models import ApartmentModel


class PostgresPipeline:
    def __init__(self, crawler: Crawler) -> None:
        self.crawler = crawler
        self.conn: Connection | None = None

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        return cls(crawler)

    def open_spider(self) -> None:
        self.conn = get_connection()
        init_db(self.conn)
        spider = self.crawler.spider
        if spider is not None:
            spider.existing_timestamps = fetch_existing_timestamps(self.conn)
            spider.logger.info(
                "Loaded %d existing apartments from DB",
                len(spider.existing_timestamps),
            )

    def process_item(self, item: dict[str, Any]) -> dict[str, Any]:
        try:
            validated = ApartmentModel(**item).model_dump()
        except ValidationError as exc:
            raise DropItem(f"Rejected apartment {item.get('id')}: {exc}")
        if self.conn is not None:
            upsert_apartment(self.conn, validated)
        return validated

    def close_spider(self) -> None:
        if self.conn is not None and not self.conn.closed:
            self.conn.close()
