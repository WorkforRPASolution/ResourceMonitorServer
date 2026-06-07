"""Create the EMPTY RESOURCE_MONITOR_PROFILE collection (+ uniq_scope index).

Mirrors exactly what the server does at non-debug startup (``init_repos``):
create the collection if absent and ensure the unique index on
``(scope.process, scope.eqpModel, scope.eqpId)``. It does NOT insert any
profile — data is added manually (JSON) afterward.

Why this exists: in ``MONITOR_DEBUG_READ_ONLY=true`` the server skips all
schema mutation, so it will never create the collection. Run this once
(deliberately) to prepare the collection on a Mongo you control.

Connection comes from ``MONITOR_MONGO_URI`` / ``MONITOR_MONGO_DB`` (your
``.env`` or environment). Point it at a LOCAL Mongo for a prod-safe setup;
pointing it at production writes real schema (the collection + index).

Safety: dry-run by default. Pass ``--yes`` to actually create.
Only ever touches the ``RESOURCE_MONITOR_PROFILE`` collection.

Usage (from repo root, venv active):
    python -m tools.create_collection          # dry-run: show target + plan
    python -m tools.create_collection --yes    # actually create
"""
from __future__ import annotations

import argparse
import asyncio
from urllib.parse import urlsplit, urlunsplit

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING

from src.config.constants import COLL_PROFILE
from src.config.settings import get_settings

_INDEX_NAME = "uniq_scope"
_INDEX_KEYS = [
    ("scope.process", ASCENDING),
    ("scope.eqpModel", ASCENDING),
    ("scope.eqpId", ASCENDING),
]


def _mask(uri: str) -> str:
    """Hide credentials in a Mongo URI before printing."""
    try:
        parts = urlsplit(uri)
        if parts.username or parts.password:
            host = parts.hostname or ""
            if parts.port:
                host = f"{host}:{parts.port}"
            netloc = f"***:***@{host}"
            return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        pass
    return uri


async def _run(do_write: bool) -> None:
    settings = get_settings()
    uri = settings.mongo_uri.get_secret_value()

    print("── create_collection ───────────────────────────────────────")
    print(f"  target Mongo : {_mask(uri)}")
    print(f"  database     : {settings.mongo_db}")
    print(f"  collection   : {COLL_PROFILE} (created EMPTY — no profile seeded)")
    print(f"  index        : {_INDEX_NAME} unique on "
          "(scope.process, scope.eqpModel, scope.eqpId)")
    print("────────────────────────────────────────────────────────────")

    if not do_write:
        print("DRY-RUN — nothing written. Re-run with --yes to create.")
        return

    client: AsyncIOMotorClient = AsyncIOMotorClient(uri)
    try:
        db = client[settings.mongo_db]
        existing = await db.list_collection_names()
        if COLL_PROFILE in existing:
            print(f"collection already exists: {settings.mongo_db}.{COLL_PROFILE}")
        else:
            await db.create_collection(COLL_PROFILE)
            print(f"created empty collection: {settings.mongo_db}.{COLL_PROFILE}")
        await db[COLL_PROFILE].create_index(_INDEX_KEYS, unique=True, name=_INDEX_NAME)
        print(f"ensured unique index: {_INDEX_NAME}")
        print("OK — ready for manual JSON profile inserts.")
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create the empty RESOURCE_MONITOR_PROFILE collection (+ uniq_scope index)."
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="actually create (default is a dry-run that only prints the plan)",
    )
    args = parser.parse_args()
    asyncio.run(_run(args.yes))


if __name__ == "__main__":
    main()
