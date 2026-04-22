"""Normalize Gecko REST payloads for active alerts.

Uses account/vessel/monitor identifiers passed at runtime only and avoids
introducing additional identifiers. Some returned message summaries include
server-provided text, which may contain sensitive or location-specific content.
Response shapes vary by API version; parsing is defensive.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def _as_list(val: Any) -> list[Any]:
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, dict):
        for key in ("items", "data", "messages", "results", "actions", "content"):
            inner = val.get(key)
            if isinstance(inner, list):
                return inner
        return [val]
    return []


def _message_targets_vessel(msg: dict[str, Any], vessel_id: str, monitor_id: str) -> bool:
    vid = str(vessel_id)
    mid = str(monitor_id)
    candidates: list[str] = []
    for key in (
        "vesselId",
        "vessel_id",
        "monitorId",
        "monitor_id",
        "spaId",
        "spa_id",
    ):
        v = msg.get(key)
        if v is not None:
            candidates.append(str(v))
    meta = msg.get("metadata")
    if isinstance(meta, dict):
        for key in ("vesselId", "monitorId", "vessel_id", "monitor_id"):
            v = meta.get(key)
            if v is not None:
                candidates.append(str(v))
    return vid in candidates or mid in candidates


def _summarize_message(msg: dict[str, Any]) -> dict[str, Any]:
    title = msg.get("title") or msg.get("subject") or msg.get("type") or "message"
    body = msg.get("body") or msg.get("message") or msg.get("text") or ""
    mid = msg.get("messageId") or msg.get("id") or msg.get("message_id")
    return {
        "id": str(mid) if mid is not None else "",
        "title": str(title)[:200],
        "preview": str(body)[:240],
    }


def _summarize_action(act: dict[str, Any]) -> dict[str, Any]:
    label = (
        act.get("title")
        or act.get("name")
        or act.get("type")
        or act.get("actionType")
        or "action"
    )
    st = act.get("status") or act.get("state") or act.get("completionStatus")
    aid = act.get("id") or act.get("completionId") or act.get("actionId")
    return {
        "id": str(aid) if aid is not None else "",
        "title": str(label)[:200],
        "status": str(st)[:80] if st is not None else "",
    }


def _action_is_active(act: dict[str, Any]) -> bool:
    """Heuristic: treat missing status as active; exclude obvious completed."""
    st = str(act.get("status") or act.get("state") or "").lower()
    if not st:
        return True
    if any(x in st for x in ("complete", "done", "dismiss", "cancel", "closed")):
        return False
    if any(x in st for x in ("open", "pending", "active", "new", "snooz")):
        return True
    return True


def parse_messages_unread_for_vessel(
    payload: Any,
    vessel_id: str,
    monitor_id: str,
) -> list[dict[str, Any]]:
    """Return summarized unread messages likely tied to this vessel/monitor."""
    out: list[dict[str, Any]] = []
    for msg in _as_list(payload):
        if not isinstance(msg, dict):
            continue
        if _message_targets_vessel(msg, vessel_id, monitor_id):
            out.append(_summarize_message(msg))
    return out


def parse_vessel_actions_active(payload: Any) -> list[dict[str, Any]]:
    """Return summarized vessel actions considered active/open."""
    out: list[dict[str, Any]] = []
    for act in _as_list(payload):
        if not isinstance(act, dict):
            continue
        if _action_is_active(act):
            out.append(_summarize_action(act))
    return out


def build_alerts_snapshot(
    *,
    messages_payload: Any | None,
    actions_payload: Any | None,
    vessel_id: str,
    monitor_id: str,
) -> dict[str, Any]:
    """Merge REST sources into one coordinator-friendly structure."""
    msgs = (
        parse_messages_unread_for_vessel(messages_payload, vessel_id, monitor_id)
        if messages_payload is not None
        else []
    )
    acts = (
        parse_vessel_actions_active(actions_payload)
        if actions_payload is not None
        else []
    )
    total = len(msgs) + len(acts)
    return {
        "total": total,
        "messages": msgs[:16],
        "actions": acts[:16],
        "updated_at": datetime.now(UTC).isoformat(),
    }
