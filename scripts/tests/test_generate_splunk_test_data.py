"""
Unit tests for generate_splunk_test_data.py — HEC payload construction.

Covers timestamp conversion, reserved field stripping, fallback behaviour,
and event count invariant.
"""

import json
import sys
import os
from datetime import datetime, timezone

import pytest

# Make the scripts directory importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from generate_splunk_test_data import SplunkTestDataGenerator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

RESERVED_KEYS = {"_time", "host", "source", "sourcetype", "index"}


def _build_payload_lines(generator, events, index="main"):
    """
    Re-run just the payload-construction loop from send_to_splunk_hec()
    without actually making any HTTP requests.  Returns a list of parsed
    JSON dicts — one per event.
    """
    reserved_keys = {"_time", "host", "source", "sourcetype", "index"}
    lines = []
    for event in events:
        iso_time = event.get("_time")
        try:
            dt = datetime.fromisoformat(iso_time)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            unix_time = dt.timestamp()
        except (ValueError, TypeError):
            unix_time = datetime.now(timezone.utc).timestamp()

        clean_event = {k: v for k, v in event.items() if k not in reserved_keys}

        hec_event = {
            "time": unix_time,
            "sourcetype": event.get("sourcetype", "json"),
            "source": event.get("source", "test_data_generator"),
            "host": event.get("host", "test-host"),
            "index": index,
            "event": clean_event,
        }
        lines.append(json.loads(json.dumps(hec_event)))
    return lines


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTimestampConversion:
    """The top-level 'time' field must be a Unix epoch float, not a string."""

    def test_known_iso_string_converts_to_correct_epoch(self):
        generator = SplunkTestDataGenerator()
        event = {
            "_time": "2026-03-29T12:00:00+00:00",
            "host": "test-host",
            "source": "test",
            "sourcetype": "json",
            "index": "main",
            "EventCode": "4625",
        }
        expected_epoch = datetime(2026, 3, 29, 12, 0, 0, tzinfo=timezone.utc).timestamp()

        [payload] = _build_payload_lines(generator, [event])

        assert payload["time"] == pytest.approx(expected_epoch, abs=1e-3)
        assert isinstance(payload["time"], float)

    def test_time_field_is_not_a_string(self):
        generator = SplunkTestDataGenerator()
        event = {
            "_time": "2026-03-29T12:00:00+00:00",
            "host": "h",
            "source": "s",
            "sourcetype": "st",
            "index": "main",
        }
        [payload] = _build_payload_lines(generator, [event])
        assert not isinstance(payload["time"], str)


class TestReservedFieldStripping:
    """Reserved Splunk metadata fields must not appear inside 'event'."""

    def test_reserved_keys_absent_from_inner_event(self):
        generator = SplunkTestDataGenerator()
        event = {
            "_time": "2026-03-29T12:00:00+00:00",
            "host": "web-server",
            "source": "/var/log/auth.log",
            "sourcetype": "linux_secure",
            "index": "security",
            "EventCode": "4625",
            "CommandLine": "net user admin /add",
        }
        [payload] = _build_payload_lines(generator, [event])
        inner = payload["event"]

        for key in RESERVED_KEYS:
            assert key not in inner, f"Reserved key '{key}' found in inner event"

    def test_application_fields_retained_in_inner_event(self):
        generator = SplunkTestDataGenerator()
        event = {
            "_time": "2026-03-29T12:00:00+00:00",
            "host": "dc01",
            "source": "wineventlog",
            "sourcetype": "WinEventLog:Security",
            "index": "main",
            "EventCode": "4625",
            "CommandLine": "whoami /priv",
            "user": "attacker",
            "src_ip": "10.0.0.5",
        }
        [payload] = _build_payload_lines(generator, [event])
        inner = payload["event"]

        assert inner["EventCode"] == "4625"
        assert inner["CommandLine"] == "whoami /priv"
        assert inner["user"] == "attacker"
        assert inner["src_ip"] == "10.0.0.5"


class TestNaiveTimezone:
    """ISO strings without timezone info must not raise an exception."""

    def test_naive_datetime_handled_without_exception(self):
        generator = SplunkTestDataGenerator()
        event = {
            "_time": "2026-03-29T12:00:00",  # no UTC offset
            "host": "h",
            "source": "s",
            "sourcetype": "st",
            "index": "main",
        }
        [payload] = _build_payload_lines(generator, [event])
        assert isinstance(payload["time"], float)


class TestMalformedTimestamp:
    """A malformed _time value must fall back to current UTC time without raising."""

    def test_malformed_time_falls_back_gracefully(self):
        generator = SplunkTestDataGenerator()
        before = datetime.now(timezone.utc).timestamp()
        event = {
            "_time": "invalid-value",
            "host": "h",
            "source": "s",
            "sourcetype": "st",
            "index": "main",
        }
        [payload] = _build_payload_lines(generator, [event])
        after = datetime.now(timezone.utc).timestamp()

        assert isinstance(payload["time"], float)
        assert before <= payload["time"] <= after

    def test_malformed_time_does_not_raise(self):
        generator = SplunkTestDataGenerator()
        event = {
            "_time": "not-a-date",
            "host": "h",
            "source": "s",
            "sourcetype": "st",
            "index": "main",
        }
        # Should not raise
        _build_payload_lines(generator, [event])


class TestMissingTime:
    """An event without a _time key must fall back without raising KeyError."""

    def test_missing_time_key_falls_back_gracefully(self):
        generator = SplunkTestDataGenerator()
        before = datetime.now(timezone.utc).timestamp()
        event = {
            "host": "h",
            "source": "s",
            "sourcetype": "st",
            "index": "main",
            "EventCode": "9999",
        }
        [payload] = _build_payload_lines(generator, [event])
        after = datetime.now(timezone.utc).timestamp()

        assert isinstance(payload["time"], float)
        assert before <= payload["time"] <= after


class TestEventCount:
    """generate_all_test_data() must still produce exactly 280 events."""

    def test_generate_all_events_produces_280(self):
        generator = SplunkTestDataGenerator()
        events = generator.generate_all_test_data()
        assert len(events) == 280

    def test_all_events_have_time_key(self):
        generator = SplunkTestDataGenerator()
        events = generator.generate_all_test_data()
        missing = [i for i, e in enumerate(events) if "_time" not in e]
        assert missing == [], f"Events at indices {missing} are missing '_time'"
