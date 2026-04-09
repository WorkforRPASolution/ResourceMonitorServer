"""Two-phase structured logging configuration.

Phase 1 — `setup_logging_minimal()`:
    Installed before settings are loaded so early-startup errors (settings
    validation, env parsing) are still captured. Writes JSON-ish lines to stderr.

Phase 2 — `setup_logging(settings)`:
    Full structlog + uvicorn + kazoo integration. Output format and level are
    governed by `AppSettings.log_format` and `AppSettings.log_level`.
"""
from __future__ import annotations

import logging
import sys

import structlog

from src.config.settings import AppSettings

_UVICORN_LOGGERS = ("uvicorn", "uvicorn.access", "uvicorn.error")
_THIRD_PARTY_LOGGERS = ("kazoo", "apscheduler", "httpx", "httpcore", "elasticsearch")


def setup_logging_minimal() -> None:
    """Bootstrap logger used before settings are loaded.

    Writes to stderr so it never collides with stdout (which the full
    structlog pipeline will own). Safe to call multiple times.
    """
    root = logging.getLogger()
    # Clear any handlers the interpreter may have attached.
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter('{"event":"%(message)s","level":"%(levelname)s"}')
    )
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def setup_logging(settings: AppSettings) -> None:
    """Full structlog initialization — called once settings are loaded.

    - stdout is owned by structlog (JSON or pretty console)
    - uvicorn and noisy third-party loggers are routed through the same pipeline
    - log level is taken from settings
    """
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: structlog.types.Processor
    if settings.log_format == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=False)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Replace whatever setup_logging_minimal installed.
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())

    # Route framework / library loggers through the same handler so we get
    # a single JSON stream. Setting propagate=False prevents duplicates.
    for name in (*_UVICORN_LOGGERS, *_THIRD_PARTY_LOGGERS):
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            lg.removeHandler(h)
        lg.addHandler(handler)
        lg.propagate = False
