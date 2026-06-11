"""Seed the catch-all RESOURCE_MONITOR_EMAIL_TEMPLATE row (Option C, P7).

Inserts/updates ONE catch-all template row:

    (app=<email_app_name>, process="_", model="_", code="RESOURCE_MONITOR", subcode="_")

This is the broadest tier the 5-tier fallback (``RmsEmailTemplateRepository``)
matches for a default RMS alert (RMS always queries ``code="RESOURCE_MONITOR"``),
so any process/model without a specific template still renders an operator-visible
default that can be edited in WebManager.

The row's ``html``/``title`` are **imported** from ``src.alert.body_renderer``
(``DEFAULT_BODY``/``DEFAULT_TITLE``) so they are byte-identical to the renderer's
built-in fallback — no drift. WebManager owns the collection schema (CRUD + the
unique index), so this script does NOT create the collection or index; it only
upserts the one row (same read-mostly tenant posture as the runtime accessor).

Connection comes from ``MONITOR_MONGO_URI`` / ``MONITOR_MONGO_DB`` (your ``.env``
or environment). Point it at a LOCAL Mongo for a safe setup; pointing it at
production writes one real row into the shared EARS collection.

Idempotent: upsert on the full composite key, so re-running never duplicates and
refreshes ``html``/``title`` to the current code constants.

Usage (from repo root, venv active):
    python -m tools.seed_template_catchall          # dry-run: show target + the row
    python -m tools.seed_template_catchall --yes    # actually upsert
"""
from __future__ import annotations

import argparse
import asyncio
from urllib.parse import urlsplit, urlunsplit

from motor.motor_asyncio import AsyncIOMotorClient

from src.alert.body_renderer import DEFAULT_BODY, DEFAULT_TITLE
from src.config.constants import ALERT_CODE_RESOURCE_MONITOR, COLL_RMS_EMAIL_TEMPLATE
from src.config.settings import get_settings


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


def _catch_all_row(app: str) -> dict:
    """The catch-all template document. ``html``/``title`` mirror the renderer's
    built-in fallback constants (imported, never re-typed → zero drift)."""
    return {
        "app": app,
        "process": "_",
        "model": "_",
        "code": ALERT_CODE_RESOURCE_MONITOR,  # NOT "_": RMS always queries code=RESOURCE_MONITOR
        "subcode": "_",
        "title": DEFAULT_TITLE,
        "html": DEFAULT_BODY,
    }


async def _run(do_write: bool) -> None:
    settings = get_settings()
    uri = settings.mongo_uri.get_secret_value()
    row = _catch_all_row(settings.email_app_name)
    key = {k: row[k] for k in ("app", "process", "model", "code", "subcode")}

    print("── seed_template_catchall ──────────────────────────────────")
    print(f"  target Mongo : {_mask(uri)}")
    print(f"  database     : {settings.mongo_db}")
    print(f"  collection   : {COLL_RMS_EMAIL_TEMPLATE} (row upsert only — schema owned by WebManager)")
    print(f"  catch-all key: {key}")
    print(f"  title        : {row['title']!r}")
    print(f"  html bytes   : {len(row['html'].encode('utf-8'))} (mirrors body_renderer.DEFAULT_BODY)")
    print("────────────────────────────────────────────────────────────")

    if not do_write:
        print("DRY-RUN — nothing written. Re-run with --yes to upsert.")
        return

    client: AsyncIOMotorClient = AsyncIOMotorClient(uri)
    try:
        coll = client[settings.mongo_db][COLL_RMS_EMAIL_TEMPLATE]
        result = await coll.update_one(
            key, {"$set": {"title": row["title"], "html": row["html"]}}, upsert=True
        )
        if result.upserted_id is not None:
            print(f"inserted catch-all row: _id={result.upserted_id}")
        elif result.modified_count:
            print("updated existing catch-all row (refreshed title/html)")
        else:
            print("catch-all row already up to date (no change)")
        print("OK — operators can now edit it in WebManager (RMS Email Template).")
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed the catch-all RESOURCE_MONITOR_EMAIL_TEMPLATE row (Option C)."
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="actually upsert (default is a dry-run that only prints the plan)",
    )
    args = parser.parse_args()
    asyncio.run(_run(args.yes))


if __name__ == "__main__":
    main()
