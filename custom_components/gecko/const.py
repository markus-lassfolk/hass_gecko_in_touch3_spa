"""Constants for the Gecko integration (Gecko Full Community fork; HA domain ``gecko``).

Privacy & portability (for maintainers)
--------------------------------------
Never hard-code or commit real **account_id**, **vessel_id**, **monitor_id**,
**Auth0 ``sub``**, **email**, **addresses**, **JWT / refresh tokens**, or other
PII in this package. Examples, tests, and docs must use obvious placeholders
(``12345``, ``monitor_example``, etc.). All user and device identity must come
from ``ConfigEntry`` data or live API responses at runtime.

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

DOMAIN = "gecko"

# Config entry options (REST enrichment; IDs always from entry data at runtime)
CONF_CLOUD_REST_POLL_INTERVAL = "cloud_rest_poll_interval"
CONF_CLOUD_REST_ONLY_WHEN_MQTT_DOWN = "cloud_rest_only_when_mqtt_down"
DEFAULT_CLOUD_REST_POLL_INTERVAL = 0
DEFAULT_CLOUD_REST_ONLY_WHEN_MQTT_DOWN = True

# Optional REST poll for account unread messages + per-vessel actions (not history).
CONF_ALERTS_POLL_INTERVAL = "alerts_poll_interval"
DEFAULT_ALERTS_POLL_INTERVAL = 0

OAUTH2_CLIENT_ID = "L81oh6hgUsvMg40TgTGoz4lxNy8eViM0"
OAUTH2_AUTHORIZE = "https://gecko-prod.us.auth0.com/authorize"
OAUTH2_TOKEN = "https://gecko-prod.us.auth0.com/oauth/token"
AUTH0_URL_BASE = "https://gecko-prod.us.auth0.com"

# API endpoints
API_BASE_URL = "https://api.geckowatermonitor.com"

# Client configuration
CONFIG_TIMEOUT = 10.0  # Default timeout for GeckoIotClient configuration loading in seconds
