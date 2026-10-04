"""Structured logging must never be able to crash the caller.

``logging`` rejects an ``extra`` key that collides with a reserved
``LogRecord`` attribute, so a context field as ordinary as ``name=`` used to
raise ``KeyError`` inside the worker. The JSON shape of the output is part of the
log contract, so it is asserted here too.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from scoutforge.config.logging import (
    CONTEXT_PREFIX,
    KeyValueFormatter,
    _collision_safe,
    log_event,
)


class TestCollisionSafeFields:
    @pytest.mark.parametrize(
        "key", ["name", "message", "args", "msg", "module", "filename", "levelname", "exc_info"]
    )
    def test_reserved_keys_are_renamed_not_dropped(self, key: str) -> None:
        safe = _collision_safe({key: "value"})

        assert safe == {f"{CONTEXT_PREFIX}{key}": "value"}
        assert key not in safe

    def test_ordinary_keys_pass_through_untouched(self) -> None:
        assert _collision_safe({"universe": 1, "level": "HOT"}) == {
            "universe": 1,
            "level": "HOT",
        }


class TestLogEventNeverRaises:
    @pytest.mark.parametrize("reserved", ["name", "message", "args", "module", "lineno"])
    def test_reserved_field_is_emitted_with_prefix(
        self, caplog: pytest.LogCaptureFixture, reserved: str
    ) -> None:
        logger = logging.getLogger("test.collision")

        with caplog.at_level(logging.INFO, logger="test.collision"):
            log_event(logger, logging.INFO, "TAG", **{reserved: "keepme"})

        record = caplog.records[-1]
        assert getattr(record, f"{CONTEXT_PREFIX}{reserved}") == "keepme"
        assert record.getMessage() == "[TAG]"

    def test_reserved_field_keeps_the_real_logger_name(self) -> None:
        logger = logging.getLogger("scoutforge.test.real")
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Capture()
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            log_event(logger, logging.INFO, "GUILD_JOIN", name="My Server")
        finally:
            logger.removeHandler(handler)

        assert len(records) == 1
        assert records[0].name == "scoutforge.test.real"
        assert getattr(records[0], f"{CONTEXT_PREFIX}name") == "My Server"


class TestFormatterOutput:
    def _record(self, **extra: Any) -> logging.LogRecord:
        record = logging.LogRecord(
            name="scoutforge.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="[%s]",
            args=("SOURCES_READY",),
            exc_info=None,
        )
        for key, value in extra.items():
            setattr(record, key, value)
        return record

    def test_json_shape_is_unchanged(self) -> None:
        payload = json.loads(
            KeyValueFormatter(use_json=True).format(
                self._record(source_name="roblox_explore", count=2, universe=123)
            )
        )

        assert payload["level"] == "INFO"
        assert payload["logger"] == "scoutforge.test"
        assert payload["message"] == "[SOURCES_READY]"
        assert payload["source_name"] == "roblox_explore"
        assert payload["count"] == 2
        assert payload["universe"] == 123
        assert payload["ts"]

    def test_key_value_shape_is_unchanged(self) -> None:
        line = KeyValueFormatter(use_json=False).format(self._record(count=2, universe=123))

        assert line.startswith("[INFO] scoutforge.test: [SOURCES_READY]")
        assert "count=2" in line
        assert "universe=123" in line

    def test_secrets_are_still_masked(self) -> None:
        line = KeyValueFormatter(use_json=False).format(self._record(discord_token="super-secret"))

        assert "super-secret" not in line
        assert "***" in line
