"""Constants for the Gecko integration (Gecko Full Community fork; HA domain ``gecko``).

Privacy & portability (for maintainers)
--------------------------------------
Never hard-code or commit real **account_id**, **vessel_id**, **monitor_id**,
**Auth0 ``sub``**, **email**, **addresses**, **JWT / refresh tokens**, or other
PII in this package. Examples, tests, and docs must use obvious placeholders
(``12345``, ``monitor_example``, etc.). All user and device identity must come
from ``ConfigEntry`` data or live API responses at runtime.

**Auth0 public / native client identifiers:** The bundled ``OAUTH2_CLIENT_ID``
(community) and ``OAUTH2_APP_CLIENT_ID`` / ``OAUTH2_APP_REDIRECT_URI`` (Gecko
mobile native client) are *not* confidential client secrets; they are the same
class of identifiers shipped inside the Gecko app for PKCE login. Private
forks or policy experiments may override the app pair at process startup via
``HASS_GECKO_OAUTH2_APP_CLIENT_ID`` and ``HASS_GECKO_OAUTH2_APP_REDIRECT_URI``.

Feature parity (Gecko app vs this integration) is tracked at a high level in the
repository README under **Roadmap**; the app also uses REST surfaces that may
return **403** for consumer tokens—parity may require Gecko API scope changes,
not only Home Assistant code.

Runtime data plane: **AWS IoT MQTT (device shadow)** is the primary source for
live state and control; **Gecko REST** is for discovery, session bootstrap
(``thirdPartySession`` broker URL), and optional enrichment when MQTT lacks a
field. Cloud MQTT still requires a **one-time OAuth** token exchange; there is
no supported zero-login path on the official backend.
"""

from __future__ import annotations

import os

DOMAIN = "gecko"


def _oauth_public_id_from_env(env_key: str, default: str) -> str:
    """Return stripped env override, or ``default`` when unset/blank."""
    raw = os.environ.get(env_key)
    if raw is None:
        return default
    stripped = str(raw).strip()
    return stripped or default


# Config entry options (REST enrichment; IDs always from entry data at runtime)
CONF_CLOUD_REST_POLL_INTERVAL = "cloud_rest_poll_interval"
CONF_CLOUD_REST_ONLY_WHEN_MQTT_DOWN = "cloud_rest_only_when_mqtt_down"
DEFAULT_CLOUD_REST_POLL_INTERVAL = 300
DEFAULT_CLOUD_REST_ONLY_WHEN_MQTT_DOWN = False

# Optional REST poll for account unread messages + per-vessel actions (not history).
CONF_ALERTS_POLL_INTERVAL = "alerts_poll_interval"
DEFAULT_ALERTS_POLL_INTERVAL = 0

# Premium energy data polling (only active when app token is linked).
CONF_ENERGY_POLL_INTERVAL = "energy_poll_interval"
DEFAULT_ENERGY_POLL_INTERVAL = 3600  # 1 hour — energy data is slow-moving

# Legacy community client — basic access only (energy/premium endpoints return 403).
OAUTH2_CLIENT_ID = "L81oh6hgUsvMg40TgTGoz4lxNy8eViM0"

# Mobile-app client — unlocks energy, charts, activities, routines, and other premium
# endpoints.  Auth0 only allows the Capacitor native redirect URI for this client,
# so the config flow uses a manual paste-callback step instead of HA's OAuth popup.
_OAUTH2_APP_CLIENT_ID_DEFAULT = "IlbhNGMeYfb8ovs0gK43CjPybltA3ogH"
_OAUTH2_APP_REDIRECT_URI_DEFAULT = (
    "com.geckoportal.gecko://gecko-prod.us.auth0.com"
    "/capacitor/com.geckoportal.gecko/callback"
)
OAUTH2_APP_CLIENT_ID = _oauth_public_id_from_env(
    "HASS_GECKO_OAUTH2_APP_CLIENT_ID",
    _OAUTH2_APP_CLIENT_ID_DEFAULT,
)
OAUTH2_APP_REDIRECT_URI = _oauth_public_id_from_env(
    "HASS_GECKO_OAUTH2_APP_REDIRECT_URI",
    _OAUTH2_APP_REDIRECT_URI_DEFAULT,
)

OAUTH2_AUTHORIZE = "https://gecko-prod.us.auth0.com/authorize"
OAUTH2_TOKEN = "https://gecko-prod.us.auth0.com/oauth/token"
AUTH0_URL_BASE = "https://gecko-prod.us.auth0.com"

# API endpoints
API_BASE_URL = "https://api.geckowatermonitor.com"

# Client configuration
CONFIG_TIMEOUT = (
    # Upper bound for gecko_iot_client ``load_configuration`` (``config/get`` reply).
    # Slow HA OS / VM / Wi‑Fi can exceed ~45s on a healthy path; if nothing answers,
    # the client still waits this full duration (bounded wait, not an endless hang).
    90.0
)

# Home Assistant core rejects ``Sensor.native_value`` strings longer than this.
MAX_SENSOR_STATE_LENGTH = 255


def clamp_sensor_native_str(value: str, max_len: int = MAX_SENSOR_STATE_LENGTH) -> str:
    """Clamp string sensor state to a length Home Assistant accepts."""
    if len(value) <= max_len:
        return value
    if max_len <= 3:
        return value[:max_len]
    return value[: max_len - 3] + "..."
