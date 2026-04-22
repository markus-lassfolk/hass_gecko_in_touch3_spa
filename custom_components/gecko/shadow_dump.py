"""Build JSON exports of Gecko MQTT / device shadow state for maintainers."""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .shadow_metrics import (
    extract_extension_booleans,
    extract_extension_metrics,
    extract_extension_strings,
    shadow_topology_summary,
)


def integration_version() -> str:
    """Read ``version`` from this integration's ``manifest.json``."""
    try:
        manifest = Path(__file__).resolve().parent / "manifest.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        return str(data.get("version", "unknown"))
    except (OSError, json.JSONDecodeError, TypeError):
        return "unknown"


_REDACTED = "<redacted>"
_PUBLIC_EXPORT_SALT = b"gecko_ha_integration|public_shadow_export|v1"

# Key segments (after splitting on ._- and lowercasing) that trigger full value redaction.
# Adjacent segment pairs (after splitting camelCase and separators), e.g. vessel + id.
_SENSITIVE_SEGMENT_PAIRS = frozenset(
    {
        ("vessel", "id"),
        ("monitor", "id"),
        ("account", "id"),
        ("user", "id"),
        ("owner", "id"),
    }
)

_SENSITIVE_SEGMENTS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "authtoken",
        "apikey",
        "api_key",
        "clientsecret",
        "client_secret",
        "authorization",
        "bearer",
        "credential",
        "credentials",
        "oauth",
        "jwt",
        "cookie",
        "sessionid",
        "session_id",
        "ssid",
        "bssid",
        "psk",
        "passphrase",
        "wpa",
        "wpakey",
        "wpa_key",
        "email",
        "mail",
        "phone",
        "mobile",
        "telephone",
        "fax",
        "latitude",
        "longitude",
        "geolat",
        "geolon",
        "street",
        "streetname",
        "addressline",
        "address1",
        "address2",
        "postalcode",
        "postal_code",
        "zipcode",
        "zip",
        "firstname",
        "lastname",
        "givenname",
        "familyname",
        "nickname",
        "middlename",
        "birthdate",
        "dob",
        "ssn",
        "iban",
        "routingnumber",
        "routing_number",
        "creditcard",
        "credit_card",
        "cardnumber",
        "card_number",
        "cvv",
        "imei",
        "serial",
        "serialnumber",
        "serial_number",
        "serialsuffix",
        "serial_suffix",
        "username",
        "useremail",
        "user_email",
        "mailaddress",
        "mail_address",
        "userid",
        "user_id",
        "accountid",
        "account_id",
        "ownerid",
        "owner_id",
        "gatewayurl",
        "gateway_url",
        "mqttusername",
        "mqtt_username",
        "mqttpassword",
        "mqtt_password",
        "privatekey",
        "private_key",
        "certificate",
        "cert",
        "pem",
    }
)

_EMAIL_RE = re.compile(
    r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b",
)
_JWT_RE = re.compile(
    r"\beyJ[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\b",
)
_MAC_RE = re.compile(
    r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b",
)
_IPV4_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}"
    r"(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b",
)
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_HEX_SECRET_RE = re.compile(r"\b[0-9a-f]{40,}\b", re.IGNORECASE)


def _key_segments(key: str) -> list[str]:
    spaced = re.sub(r"([a-z])([A-Z])", r"\1_\2", key)
    return [p for p in re.split(r"[._\s\-]+", spaced.lower()) if p]


def _segment_key_is_sensitive(key: str) -> bool:
    segs = _key_segments(key)
    if _SENSITIVE_SEGMENTS.intersection(segs):
        return True
    return any((a, b) in _SENSITIVE_SEGMENT_PAIRS for a, b in zip(segs, segs[1:]))


def _opaque_fingerprint(label: str, raw: str) -> str:
    digest = hashlib.sha256(
        _PUBLIC_EXPORT_SALT + f"|{label}|{raw}".encode()
    ).hexdigest()[:16]
    return f"anon_{label}_{digest}"


def _is_private_ipv4(text: str) -> bool:
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    return bool(
        ip.version == 4 and (ip.is_private or ip.is_loopback or ip.is_link_local)
    )


def _redact_string_scalars(text: str) -> str:
    """Best-effort scrub of common secrets embedded in arbitrary strings."""

    def _sub_ip(m: re.Match[str]) -> str:
        return _REDACTED if _is_private_ipv4(m.group(0)) else m.group(0)

    out = _EMAIL_RE.sub(_REDACTED, text)
    out = _JWT_RE.sub(_REDACTED, out)
    out = _MAC_RE.sub(_REDACTED, out)
    out = _UUID_RE.sub(_REDACTED, out)
    out = _HEX_SECRET_RE.sub(_REDACTED, out)
    out = _IPV4_RE.sub(_sub_ip, out)
    return out


def _deep_sanitize(obj: Any) -> Any:
    """Recursively redact sensitive keys and scrub string leaves."""

    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if _segment_key_is_sensitive(str(k)):
                out[k] = _REDACTED
                continue
            if isinstance(v, dict):
                out[k] = _deep_sanitize(v)
            elif isinstance(v, list):
                out[k] = [_deep_sanitize(i) for i in v]
            elif isinstance(v, str):
                out[k] = _redact_string_scalars(v)
            elif (
                isinstance(v, (int, float))
                and not isinstance(v, bool)
                and str(k).lower() in {
                    "latitude",
                    "longitude",
                    "geolat",
                    "geolon",
                }
            ):
                out[k] = None
            else:
                out[k] = v
        return out
    if isinstance(obj, list):
        return [_deep_sanitize(i) for i in obj]
    if isinstance(obj, str):
        return _redact_string_scalars(obj)
    return obj


def sanitize_export_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a deep-copied payload safe for public sharing (best-effort).

    Replaces monitor/vessel ids with stable one-way fingerprints, redacts
    values under keys that look like credentials, network identity, or contact
    fields, and strips common secret patterns from string leaves. This cannot
    guarantee zero residual identifying data in unknown vendor keys; users
    should still skim the file before posting.
    """

    out = copy.deepcopy(payload)
    mid = str(out.get("monitor_id", "") or "")
    vid = str(out.get("vessel_id", "") or "")

    out["sanitized_for_public_share"] = True
    out["sanitization_note"] = (
        "Identifiers are fingerprinted; sensitive key names and common secret "
        "patterns in strings are redacted. Unknown vendor-specific keys may "
        "still contain data you should remove manually before sharing."
    )

    out["monitor_id"] = _opaque_fingerprint("monitor", mid) if mid else _REDACTED
    out["vessel_id"] = _opaque_fingerprint("vessel", vid) if vid else _REDACTED

    tp = out.get("transporter")
    if isinstance(tp, dict) and "monitor_id" in tp:
        tp = copy.deepcopy(tp)
        tp["monitor_id"] = _opaque_fingerprint("monitor", mid) if mid else _REDACTED
        out["transporter"] = tp

    for field in ("device_shadow_state", "device_configuration", "zones_list"):
        if field in out and out[field] is not None:
            out[field] = _deep_sanitize(out[field])

    return out


def safe_export_filename(
    user_part: str | None, monitor_id: str, *, anonymous: bool = False
) -> str:
    """Single file component under ``gecko_shadow_dumps/`` (no path traversal)."""
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    if user_part:
        base = user_part
    elif anonymous:
        base = f"gecko_shadow_export_{ts}"
    else:
        base = f"gecko_shadow_{monitor_id}_{ts}"
    base = re.sub(r"[^a-zA-Z0-9_.-]+", "_", base).strip("._-")[:120]
    if not base.lower().endswith(".json"):
        base = f"{base}.json"
    if ".." in base or "/" in base or "\\" in base:
        sanitized_monitor_id = re.sub(r"[^a-zA-Z0-9_.-]+", "_", monitor_id).strip(
            "._-"
        )[:40]
        base = (
            "gecko_shadow_export.json"
            if anonymous
            else f"gecko_shadow_{sanitized_monitor_id}.json"
        )
    return base


def build_shadow_export_payload(
    *,
    monitor_id: str,
    vessel_id: str,
    gecko_client: Any,
    include_configuration: bool,
    include_derived: bool,
    mqtt_connected: bool | None,
    sanitize_for_public_share: bool = True,
) -> dict[str, Any]:
    """Assemble a JSON structure for maintainers.

    When ``sanitize_for_public_share`` is True (default), returns a deep-copied,
    redacted payload suitable for community sharing. When False, the returned
    dict is still deep-copied so file writes do not race live MQTT state.
    """
    state = getattr(gecko_client, "_state", None)
    configuration = (
        getattr(gecko_client, "_configuration", None) if include_configuration else None
    )
    transporter = getattr(gecko_client, "transporter", None)
    transporter_info: dict[str, Any] = {}
    if transporter is not None:
        transporter_info["class"] = type(transporter).__name__
        mid = getattr(transporter, "_monitor_id", None) or getattr(
            transporter, "monitor_id", None
        )
        if mid:
            transporter_info["monitor_id"] = str(mid)

    payload: dict[str, Any] = {
        "export_format": "gecko_ha_shadow_dump",
        "export_version": 1,
        "integration_version": integration_version(),
        "exported_at_utc": datetime.now(UTC).isoformat(),
        "monitor_id": str(monitor_id),
        "vessel_id": str(vessel_id),
        "mqtt_connected": mqtt_connected,
        "device_shadow_state": state,
        "device_configuration": configuration,
        "transporter": transporter_info or None,
    }

    if include_derived and isinstance(state, dict):
        payload["shadow_topology_summary"] = shadow_topology_summary(state)
        ext_num = extract_extension_metrics(state)
        payload["extension_numeric_paths_sample"] = {
            "count": len(ext_num),
            "paths": sorted(ext_num.keys())[:400],
        }
        ext_bool = extract_extension_booleans(state)
        payload["extension_boolean_paths_sample"] = {
            "count": len(ext_bool),
            "paths": sorted(ext_bool.keys())[:200],
        }
        ext_str = extract_extension_strings(state)
        payload["extension_string_paths_sample"] = {
            "count": len(ext_str),
            "paths": sorted(ext_str.keys())[:200],
        }

    zones_info: list[dict[str, Any]] = []
    try:
        if hasattr(gecko_client, "list_zones"):
            zones_info = gecko_client.list_zones()  # type: ignore[assignment]
    except Exception:
        zones_info = []
    payload["zones_list"] = zones_info

    if sanitize_for_public_share:
        return sanitize_export_payload(payload)
    payload["sanitized_for_public_share"] = False
    # Full raw dumps must not retain live client references: the write runs in
    # an executor while MQTT may still mutate _state / zones.
    return copy.deepcopy(payload)


def write_json_file(path: Path, data: dict[str, Any]) -> None:
    """Blocking write (run in executor)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, default=str)
    path.write_text(text, encoding="utf-8")
