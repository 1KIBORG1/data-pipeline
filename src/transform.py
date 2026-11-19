"""Bronze (`raw_listings`) to Silver (`apartments`) transformation CLI.

Replays active RSC payloads from `raw_listings` through `parse_apartment_desc_dict`
and `ApartmentModel`, batch-upserting valid rows into `apartments`.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from psycopg2.extensions import connection as Connection
from pydantic import ValidationError

from src.db import (
    ensure_connection,
    get_connection,
    init_db,
    iter_active_raw_listings,
    prune_apartments_not_in,
    upsert_apartments_batch,
)
from src.models import ApartmentModel
from src.parser import parse_apartment_desc_dict

logger = logging.getLogger(__name__)


@dataclass
class TransformStats:
    """Summary metrics returned by a Bronze -> Silver transformation run."""

    total_raw: int = 0
    saved_silver: int = 0
    skipped_parser: int = 0
    skipped_validation: int = 0
    pruned_silver: int = 0
    skipped_reasons: Counter[str] = field(default_factory=Counter)
    elapsed_sec: float = 0.0


def extract_reject_reason(exc: ValidationError) -> str:
    """Summarize a Pydantic `ValidationError` into a short grouped reason label."""
    for err in exc.errors():
        msg = err.get("msg", "")
        if "rejected stem:" in msg:
            return f"stem:{msg.rsplit('rejected stem:', 1)[-1].strip()}"
        if "outside Slovakia" in msg:
            return "gps_outside_slovakia"
        if "marked inactive" in msg:
            return "inactive"
        if "monthly rental" in msg or "rental or swap" in msg:
            return "rental_or_swap"
        if "commercial non-residential" in msg:
            return "commercial_space"
        if "multi-unit bundle" in msg:
            return "multi_unit_bundle"
        if "house or commercial building" in msg:
            return "house_or_building"
        if "buyer-wanted DOPYT" in msg:
            return "dopyt_or_non_apartment_desc"
        if "townhouse or house with private land" in msg or "row house" in msg:
            return "townhouse_or_row_house"
        if "spoofs Bratislava" in msg:
            return "bratislava_spoofing"
        if "outside the 79 official districts" in msg:
            return "foreign_district"
        if "neither price_eur nor area_sqm" in msg:
            return "empty_shell"
        if "QA test placeholder" in msg:
            return "qa_placeholder"
        loc = err.get("loc")
        if loc:
            return str(loc[0])
    return "invalid"


def run_transform(
    conn: Connection,
    *,
    exclude_reserved: bool = False,
    dry_run: bool = False,
    prune: bool = True,
    allow_empty_prune: bool = False,
    batch_size: int = 200,
    log: logging.Logger | logging.LoggerAdapter[Any] | None = None,
) -> TransformStats:
    """Transform active Bronze `raw_listings` into validated Silver `apartments` rows."""
    active_log = log or logger
    started = time.monotonic()
    stats = TransformStats()

    valid_ids: set[str] = set()
    buffer: list[dict[str, Any]] = []

    conn = ensure_connection(conn)
    for _ad_id, url, payload in iter_active_raw_listings(conn, batch_size=1000):
        stats.total_raw += 1
        if not isinstance(payload, dict):
            stats.skipped_parser += 1
            stats.skipped_reasons["invalid_payload"] += 1
            continue

        raw_item = parse_apartment_desc_dict(payload, url=url)
        if raw_item is None:
            stats.skipped_parser += 1
            stats.skipped_reasons["non_apartment_or_foreign"] += 1
            continue

        try:
            validated = ApartmentModel(**raw_item, exclude_reserved=exclude_reserved).model_dump()
        except ValidationError as exc:
            reason = extract_reject_reason(exc)
            stats.skipped_validation += 1
            stats.skipped_reasons[reason] += 1
            continue

        valid_ids.add(validated["id"])
        buffer.append(validated)

        if len(buffer) >= batch_size:
            if not dry_run:
                conn = ensure_connection(conn)
                upsert_apartments_batch(conn, buffer)
            stats.saved_silver += len(buffer)
            buffer.clear()

    if buffer:
        if not dry_run:
            conn = ensure_connection(conn)
            upsert_apartments_batch(conn, buffer)
        stats.saved_silver += len(buffer)
        buffer.clear()

    if prune and not dry_run:
        if stats.total_raw == 0 and not allow_empty_prune:
            active_log.warning(
                "Skipping Silver prune because Bronze `raw_listings` has 0 active rows "
                "(pass --allow-empty-prune to force)."
            )
        else:
            conn = ensure_connection(conn)
            stats.pruned_silver = prune_apartments_not_in(conn, valid_ids)

    stats.elapsed_sec = round(time.monotonic() - started, 2)
    top_reasons = ", ".join(f"{k}={v}" for k, v in stats.skipped_reasons.most_common(10))
    reasons_suffix = f" ({top_reasons})" if top_reasons else ""
    mode_tag = " [DRY-RUN]" if dry_run else ""

    active_log.info(
        "Silver transform%s finished in %.2fs | raw_active=%d | saved=%d | pruned=%d | skipped=%d%s",
        mode_tag,
        stats.elapsed_sec,
        stats.total_raw,
        stats.saved_silver,
        stats.pruned_silver,
        stats.skipped_parser + stats.skipped_validation,
        reasons_suffix,
    )
    return stats


def _setup_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-7s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logging.root.handlers.clear()
    logging.root.addHandler(handler)
    logging.root.setLevel(getattr(logging, level, logging.INFO))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reprocess Bronze `raw_listings` into Silver `apartments` offline."
    )
    parser.add_argument(
        "--exclude-reserved",
        action="store_true",
        default=False,
        help="Exclude reserved ('rezervované') listings during Silver rebuild.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Run validation and print stats without writing to `apartments`.",
    )
    parser.add_argument(
        "--no-prune",
        action="store_true",
        default=False,
        help="Do not delete obsolete or newly-rejected rows from `apartments`.",
    )
    parser.add_argument(
        "--allow-empty-prune",
        action="store_true",
        default=False,
        help="Allow pruning `apartments` even if `raw_listings` has 0 active rows.",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Logging verbosity (default: INFO).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    _setup_logging(args.log_level)
    conn = get_connection()
    try:
        init_db(conn)
        run_transform(
            conn,
            exclude_reserved=args.exclude_reserved,
            dry_run=args.dry_run,
            prune=not args.no_prune,
            allow_empty_prune=args.allow_empty_prune,
        )
    finally:
        if not conn.closed:
            conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
