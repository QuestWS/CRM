from __future__ import annotations

import datetime as dt

import pytest

from crm.ingest.sangoma import (
    counterparty,
    infer_direction,
    normalize_webhook_payload,
    parse_recording_filename,
)
from crm.models import Direction, Recording


def test_parse_outbound_filename():
    got = parse_recording_filename("out-6135551234-201-20240115-143022-1705345822.14.wav")
    assert got["direction"] == Direction.outbound
    assert got["to_number"] == "6135551234"
    assert got["from_number"] == "201"
    assert got["call_started_at"] == dt.datetime(2024, 1, 15, 14, 30, 22)
    assert got["pbx_call_id"] == "1705345822.14"


def test_parse_inbound_filename():
    got = parse_recording_filename("in-201-6135551234-20240115-090500-1705312500.3.wav")
    assert got["direction"] == Direction.inbound
    assert got["to_number"] == "201"
    assert got["from_number"] == "6135551234"


def test_parse_external_filename():
    got = parse_recording_filename("external-6135551234-20240115-090500-1705312500.3.wav")
    assert got["direction"] == Direction.inbound
    assert got["from_number"] == "6135551234"


def test_parse_unrecognised_filename_is_not_an_error():
    got = parse_recording_filename("some-random-name.mp3")
    assert got["direction"] == Direction.unknown
    assert got["call_started_at"] is None
    assert got["from_number"] is None


def test_infer_direction_from_operator_numbers():
    assert infer_direction("+16135550100", "+16135559999") == Direction.outbound
    assert infer_direction("+16135559999", "+16135550100") == Direction.inbound
    assert infer_direction("+16135550100", "+16135550101") == Direction.internal
    assert infer_direction("+16135559999", "+16135558888") == Direction.unknown


def test_counterparty_picks_the_other_party():
    inbound = Recording(
        path="x", sha256="a", direction=Direction.inbound,
        from_number="+16135559999", to_number="+16135550100",
    )
    outbound = Recording(
        path="x", sha256="b", direction=Direction.outbound,
        from_number="+16135550100", to_number="+16135559999",
    )
    unknown = Recording(
        path="x", sha256="c", direction=Direction.unknown,
        from_number="+16135550100", to_number="+16135557777",
    )
    assert counterparty(inbound) == "+16135559999"
    assert counterparty(outbound) == "+16135559999"
    assert counterparty(unknown) == "+16135557777"


@pytest.mark.parametrize(
    "payload,expect_from,expect_dir",
    [
        ({"src": "6135551234", "dst": "201", "calltype": "inbound"},
         "6135551234", Direction.inbound),
        ({"from": "201", "to": "6135551234", "direction": "out"},
         "201", Direction.outbound),
        ({"callerid": "6135551234"}, "6135551234", Direction.unknown),
    ],
)
def test_webhook_field_aliases(payload, expect_from, expect_dir):
    got = normalize_webhook_payload(payload)
    assert got["from_number"] == expect_from
    assert got["direction"] == expect_dir


@pytest.mark.parametrize(
    "raw",
    ["2024-01-15T14:30:22Z", "2024-01-15 14:30:22", "1705328722"],
)
def test_webhook_timestamp_formats(raw):
    got = normalize_webhook_payload({"start_time": raw})
    assert got["call_started_at"] is not None
    assert got["call_started_at"].year == 2024


def test_webhook_bad_timestamp_does_not_raise():
    got = normalize_webhook_payload({"start_time": "sometime tuesday"})
    assert got["call_started_at"] is None


def test_webhook_consent_flag():
    assert normalize_webhook_payload({"consent_announced": "true"})["consent_announced"]
    assert not normalize_webhook_payload({})["consent_announced"]
