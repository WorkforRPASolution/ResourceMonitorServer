"""Tests for src.logging_config (two-phase logging initialization)."""
import json
import logging

import pytest
import structlog

from src.config.settings import AppSettings
from src.logging_config import setup_logging, setup_logging_minimal


@pytest.fixture(autouse=True)
def reset_logging():
    """Restore root logger state between tests to avoid handler pollution."""
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    yield
    root.handlers = original_handlers
    root.setLevel(original_level)
    structlog.reset_defaults()


@pytest.mark.unit
class TestSetupLoggingMinimal:
    def test_minimal_installs_a_handler(self):
        root = logging.getLogger()
        root.handlers = []
        setup_logging_minimal()
        assert len(root.handlers) >= 1

    def test_minimal_emits_before_settings(self, capsys):
        """Must work before any settings are loaded (used for early startup errors)."""
        setup_logging_minimal()
        logger = logging.getLogger("boot")
        logger.info("pre_settings_event")
        captured = capsys.readouterr()
        # Minimal logger writes to stderr in JSON-ish format
        assert "pre_settings_event" in (captured.err + captured.out)


@pytest.mark.unit
class TestSetupLogging:
    def test_full_setup_with_json_format(self):
        settings = AppSettings(log_format="json", log_level="INFO")
        setup_logging(settings)
        # structlog is configured — retrieving a logger should not fail
        log = structlog.get_logger("test")
        log.info("hello_structured", key="value")

    def test_full_setup_with_console_format(self):
        settings = AppSettings(log_format="console", log_level="DEBUG")
        setup_logging(settings)
        log = structlog.get_logger("test")
        log.debug("hello_console")

    def test_root_level_matches_settings(self):
        settings = AppSettings(log_level="WARNING")
        setup_logging(settings)
        assert logging.getLogger().level == logging.WARNING

    def test_uvicorn_loggers_are_integrated(self):
        settings = AppSettings()
        setup_logging(settings)
        for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
            lg = logging.getLogger(name)
            assert lg.propagate is False
            assert len(lg.handlers) >= 1

    def test_kazoo_logger_is_integrated(self):
        """kazoo emits its own logs via stdlib logging — must flow through structlog."""
        settings = AppSettings()
        setup_logging(settings)
        kazoo_lg = logging.getLogger("kazoo")
        assert kazoo_lg.propagate is False
        assert len(kazoo_lg.handlers) >= 1

    def test_json_output_is_parseable(self, capsys):
        settings = AppSettings(log_format="json", log_level="INFO")
        setup_logging(settings)
        log = structlog.get_logger("test")
        log.info("parseable_event", foo="bar", count=3)
        captured = capsys.readouterr()
        stdout = captured.out.strip().split("\n")
        # At least one line should be valid JSON containing our event
        for line in stdout:
            if "parseable_event" in line:
                data = json.loads(line)
                assert data["event"] == "parseable_event"
                assert data["foo"] == "bar"
                assert data["count"] == 3
                return
        pytest.fail(f"parseable_event not found in output: {captured.out}")
