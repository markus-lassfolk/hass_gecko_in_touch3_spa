"""Tests for ``custom_components.gecko.rest_alerts``."""

from __future__ import annotations

from datetime import datetime

import pytest
from custom_components.gecko import rest_alerts


def test_as_list_variants() -> None:
    assert rest_alerts._as_list(None) == []
    assert rest_alerts._as_list([1, 2]) == [1, 2]
    assert rest_alerts._as_list({"items": [3]}) == [3]
    assert rest_alerts._as_list({"x": 1}) == [{"x": 1}]
    assert rest_alerts._as_list("nope") == []


def test_message_targets_vessel() -> None:
    msg = {"vesselId": "v1", "noise": "x"}
    assert rest_alerts._message_targets_vessel(msg, "v1", "m9")
    assert rest_alerts._message_targets_vessel(msg, "v2", "m9") is False
    meta_msg = {"metadata": {"monitor_id": "m1"}}
    assert rest_alerts._message_targets_vessel(meta_msg, "v", "m1")


def test_summarize_message_and_action() -> None:
    m = rest_alerts._summarize_message(
        {"title": "T", "body": "B" * 300, "messageId": 99}
    )
    assert m["id"] == "99"
    assert len(m["preview"]) <= 240
    a = rest_alerts._summarize_action({"title": "A", "status": "open", "id": "a1"})
    assert a["status"] == "open"


@pytest.mark.parametrize(
    ("status", "active"),
    [
        ("", True),
        ("completed", False),
        ("pending", True),
        ("unknown_status", True),
    ],
)
def test_action_is_active(status: str, active: bool) -> None:
    assert rest_alerts._action_is_active({"status": status}) is active


def test_parse_messages_unread_for_vessel_filters() -> None:
    payload = [
        {"vesselId": "v1", "title": "a"},
        "skip",
        {"vesselId": "v2"},
    ]
    out = rest_alerts.parse_messages_unread_for_vessel(payload, "v1", "m")
    assert len(out) == 1
    assert out[0]["title"] == "a"


def test_parse_vessel_actions_active() -> None:
    payload = [{"id": 1, "status": "done"}, {"id": 2, "status": "pending"}]
    out = rest_alerts.parse_vessel_actions_active(payload)
    assert len(out) == 1
    assert out[0]["id"] == "2"


def test_build_alerts_snapshot_merges() -> None:
    snap = rest_alerts.build_alerts_snapshot(
        messages_payload=[{"vesselId": "v", "title": "m"}],
        actions_payload=[{"status": "open", "name": "act"}],
        vessel_id="v",
        monitor_id="m",
    )
    assert snap["total"] == 2
    assert len(snap["messages"]) == 1
    assert len(snap["actions"]) == 1
    datetime.fromisoformat(snap["updated_at"].replace("Z", "+00:00"))


def test_as_list_messages_key() -> None:
    wrapped = {"messages": [{"vesselId": "v", "title": "x"}]}
    assert len(rest_alerts._as_list(wrapped)) == 1


def test_message_targets_vessel_spa_id() -> None:
    msg = {"spa_id": "s42"}
    assert rest_alerts._message_targets_vessel(msg, "v", "s42")


def test_build_alerts_snapshot_caps_lists() -> None:
    msgs = [{"vesselId": "v", "title": str(i)} for i in range(30)]
    acts = [{"status": "open", "name": str(i)} for i in range(30)]
    snap = rest_alerts.build_alerts_snapshot(
        messages_payload=msgs,
        actions_payload=acts,
        vessel_id="v",
        monitor_id="m",
    )
    assert snap["total"] == 60
    assert len(snap["messages"]) == 16
    assert len(snap["actions"]) == 16
