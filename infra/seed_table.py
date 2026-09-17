"""Fill the deployed DynamoDB table with the demo household.

The deployed stack starts with an empty table, and a household needs weeks of
airings before SASH will name an opening. This builds the same 120-day
household ``sash demo`` uses, through the same webhook receiver and poller,
into a local SQLite file, then writes that record into the stack's table
through :class:`sash.aws.dynamo_store.DynamoStore`, 25 items per batch and
sixteen batches in flight. The deployed product then opens on the household
the demo shows, at the same instant.

    python infra/seed_table.py --table sash-dev-household --profile otl --region us-east-1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / "src")]

from sash.aws.dynamo_store import DynamoStore  # noqa: E402
from sash.cli import DEMO_DAYS, DEMO_LIVE_MINUTES, DEMO_SEED, DEMO_TAPE_END, _progress_bar  # noqa: E402
from sash.config import CONFIG  # noqa: E402
from sash.store import Store  # noqa: E402
from simulator.seed import build_home, seed_store  # noqa: E402


class BatchingTable:
    """A DynamoDB table handle whose puts are buffered and flushed in parallel."""

    def __init__(self, table: Any):
        self.table = table
        self.pending: dict[tuple[str, str], dict[str, Any]] = {}
        self.written = 0

    def put_item(self, Item: dict[str, Any], ConditionExpression: str | None = None) -> None:  # noqa: N803
        self.pending[(Item["pk"], Item["sk"])] = Item

    def flush(self) -> None:
        items = list(self.pending.values())
        chunks = [items[i : i + 25] for i in range(0, len(items), 25)]

        def write(chunk: list[dict[str, Any]]) -> None:
            with self.table.batch_writer(overwrite_by_pkeys=["pk", "sk"]) as batch:
                for item in chunk:
                    batch.put_item(Item=item)

        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(write, chunks))
        self.written += len(items)
        self.pending.clear()


def build_local(path: Path) -> None:
    store = Store(str(path))
    home = build_home(days=DEMO_DAYS, seed=DEMO_SEED, end=DEMO_TAPE_END)
    until = DEMO_TAPE_END - DEMO_LIVE_MINUTES * 60.0
    print(f"Seeding {DEMO_DAYS:.0f} days through the webhook receiver and the poller")
    report = asyncio.run(
        seed_store(store, home, hmac_key=CONFIG.ring.hmac_key, until=until, progress=_progress_bar())
    )
    print()
    for key, value in report.as_dict().items():
        print(f"  {key.replace('_', ' '):.<42} {value}")
    store.set_meta("demo_cursor", until)
    store.close()


def copy(path: Path, target: DynamoStore, table: BatchingTable) -> None:
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row

    def rows(sql: str) -> list[sqlite3.Row]:
        return db.execute(sql).fetchall()

    for r in rows("SELECT * FROM vehicle_events"):
        target.add_vehicle_event(r["event_id"], r["camera_id"], r["event_time"], r["source"])
    for r in rows("SELECT * FROM contact_events"):
        target.add_contact_event(r["event_id"], r["device_id"], r["event_time"], r["state"])
    for r in rows("SELECT * FROM aq_threshold_events"):
        target.add_threshold_event(
            r["event_id"], r["device_id"], r["event_time"], r["event_type"], r["level"]
        )
    for r in rows("SELECT * FROM aq_samples"):
        target.add_aq_sample(
            r["device_id"], r["reported_at"], r["fetched_at"],
            r["pm25"], r["co"], r["temperature"], r["humidity"],
        )
    for r in rows("SELECT * FROM device_health"):
        target._put(
            {
                "pk": target.pk,
                "sk": f"HEALTH#{float(r['at']):015.3f}#{r['device_id']}",
                "device_id": r["device_id"],
                "online": bool(r["online"]),
                "at": r["at"],
            }
        )
    for r in rows("SELECT * FROM devices"):
        target._put(
            {
                "pk": target.pk,
                "sk": f"DEVICE#{r['device_id']}",
                "device_id": r["device_id"],
                "ring_name": r["ring_name"],
                "family": r["family"],
                "app_role": r["app_role"],
                "label": r["label"],
                "orientation": r["orientation"],
                "online": bool(r["online"]),
                "last_status_at": r["last_status_at"],
                "config_hash": r["config_hash"],
            }
        )
    for r in rows("SELECT key, value FROM meta"):
        target.set_meta(r["key"], json.loads(r["value"]))
    db.close()
    print(f"  {'items to write':.<42} {len(table.pending)}")
    table.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--table", required=True)
    parser.add_argument("--profile", default=None)
    parser.add_argument("--region", default=None)
    args = parser.parse_args()

    import boto3

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    table = BatchingTable(session.resource("dynamodb").Table(args.table))
    target = DynamoStore(table, now=time.time())

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "household.sqlite"
        build_local(path)
        started = time.time()
        copy(path, target, table)
    print(f"  {'items written':.<42} {table.written}")
    print(f"  {'seconds writing':.<42} {time.time() - started:.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
