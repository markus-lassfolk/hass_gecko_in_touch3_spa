#!/usr/bin/env python3
"""Live test: Gecko Auth0 token → REST → MQTT → shadow metric helpers.

Uses the **same Auth0 application and PKCE parameters** as the Home Assistant Gecko
integration, so **Sign in with Apple** (and other Auth0 Universal Login methods) work
in the browser. No username/password is required unless you opt into the legacy
password grant.

Authentication (first match wins)
---------------------------------
1. **``GECKO_ACCESS_TOKEN``** in the environment or ``.secrets/gecko.env`` — a valid
   Gecko API bearer JWT (short-lived). Use this if you already have a token from
   another tool. This path does **not** update the OAuth token file.

2. **``GECKO_TOKEN_FILE``** or the default **``.secrets/gecko_oauth.json``** — saved
   **access_token** / **refresh_token** / **expires_at** from a previous browser login
   or password grant. If the access token is expired (or near expiry), the script
   refreshes via Auth0 **grant_type=refresh_token** and rewrites the file (unless
   **``--no-save-token``**).

3. **``GECKO_USERNAME``** + **``GECKO_PASSWORD``** — Auth0 *Resource Owner Password*
   grant (often disabled; not used for Apple).

4. Otherwise **Authorization Code + PKCE** (default **``my-home-assistant``**): Auth0
   redirects to **``https://my.home-assistant.io/redirect/oauth``** — a **public**
   Nabu Casa URL that Gecko's Auth0 app already allowlists. You do **not** need Home
   Assistant running on your LAN; after Apple (or other) login, copy the **full URL**
   from the address bar (it contains ``?code=…&state=…``) and paste it at the prompt.
   If the tab navigates away, use **Back** or **History** to recover that URL.

   **Apple login “hangs”** (spinner, never reaches my.home-assistant.io): this is almost
   always **browser / Auth0 / Apple / Nabu Casa** in the tab, not the script. Use
   DevTools → Network (Preserve log) to copy a **my.home-assistant.io** request URL that
   contains ``code=``, or try Safari or incognito. The script prints more detail when it
   opens the login URL.

   **``--oauth-redirect loopback``** uses ``http://127.0.0.1:…/callback``. Gecko's Auth0
   client **does not** allow that today, so Auth0 shows **"Oops, something went wrong"**
   before the login form. Loopback is only for future use if Gecko registers those URLs.

Requirements
------------
- **Python 3.12+** with **``aiohttp``** (and **``gecko-iot-client``**, **``awsiotsdk``** for MQTT).
- On Debian/Ubuntu, system Python is often **PEP 668** (“externally managed”) — do not
  ``pip install`` into it. Use a venv at the repo root::

    python3 -m venv .venv-gecko-live
    .venv-gecko-live/bin/pip install -r scripts/requirements-live.txt
    .venv-gecko-live/bin/python3 scripts/verify_shadow_live.py …

Copy ``scripts/gecko_live_test.env.example`` → ``.secrets/gecko.env`` (gitignored).

Usage::

    .venv-gecko-live/bin/python3 scripts/verify_shadow_live.py
    .venv-gecko-live/bin/python3 scripts/verify_shadow_live.py --dump-api --api-dump-out .secrets/gecko_api_snapshot.json --dump-api-only --api-probe-concurrency 8
    python3 scripts/verify_shadow_live.py --oauth-redirect loopback   # usually fails (Auth0 Oops)
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
import os
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

# ---------------------------------------------------------------------------
# Repo paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_PATH = REPO_ROOT / ".secrets" / "gecko.env"
# REST ``--dump-api`` map: avoid huge fan-out on accounts with many spas
REST_MAP_MONITOR_LIMIT = 15
# Path templates extracted from Gecko Android 1.9.0 (com.geckoportal.gecko) web bundle.
GECKO_APP_REST_PATH_CATALOG = REPO_ROOT / "scripts" / "gecko_paths_raw_app_1.9.0.txt"

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
import verify_shadow_live_oauth as _vsl_oauth  # noqa: E402

try:
    import aiohttp
except ImportError as exc:
    raise SystemExit(
        "Missing dependency 'aiohttp'. On Debian/Ubuntu, system Python is often "
        "PEP 668 (externally managed) — use a venv, for example:\n\n"
        f"  cd {REPO_ROOT}\n"
        "  python3 -m venv .venv-gecko-live\n"
        "  .venv-gecko-live/bin/pip install -r scripts/requirements-live.txt\n"
        "  .venv-gecko-live/bin/python3 scripts/verify_shadow_live.py …\n"
    ) from exc

# Gecko REST (OAuth + token flow lives in ``verify_shadow_live_oauth``).
API_BASE_URL = "https://api.geckowatermonitor.com"
CONFIG_TIMEOUT = 30.0

_LOG = logging.getLogger("verify_shadow_live")


def _install_homeassistant_stubs() -> None:
    """Minimal stubs so ``shadow_metrics`` imports without Home Assistant."""
    ha = ModuleType("homeassistant")
    const = ModuleType("homeassistant.const")

    class _UnitOfTemperature:
        CELSIUS = "°C"

    const.UnitOfTemperature = _UnitOfTemperature
    sys.modules.setdefault("homeassistant", ha)
    sys.modules.setdefault("homeassistant.const", const)


class _TokenGeckoApi:
    """Subset of ``gecko_iot_client.GeckoApiClient`` using a static bearer token."""

    def __init__(self, session: Any, access_token: str) -> None:
        self._session = session
        self._token = access_token
        self.api_url = API_BASE_URL
        self.auth0_url = "https://gecko-prod.us.auth0.com"

    async def async_get_access_token(self) -> str:
        return self._token

    async def async_get_user_id(self) -> str:
        headers = {"Authorization": f"Bearer {self._token}"}
        url = f"{self.auth0_url}/userinfo"
        async with self._session.get(url, headers=headers) as response:
            response.raise_for_status()
            payload = await response.json()
        sub = payload.get("sub")
        if not sub:
            raise ValueError(f"Auth0 userinfo missing 'sub': {payload!r}")
        return str(sub)

    async def async_request(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {self._token}"
        url = f"{self.api_url}{endpoint}"
        async with self._session.request(
            method, url, headers=headers, **kwargs
        ) as response:
            response.raise_for_status()
            return await response.json()

    async def async_get_user_info(self, user_id: str) -> dict[str, Any]:
        return await self.async_request("GET", f"/v2/user/{user_id}")

    async def async_get_vessels(self, account_id: str) -> list[dict[str, Any]]:
        data = await self.async_request("GET", f"/v4/accounts/{account_id}/vessels")
        if isinstance(data, dict):
            if "vessels" in data:
                return data["vessels"]
            if "data" in data and isinstance(data["data"], list):
                return data["data"]
            if "results" in data and isinstance(data["results"], list):
                return data["results"]
        return data if isinstance(data, list) else []

    async def async_get_monitor_livestream(self, monitor_id: str) -> dict[str, Any]:
        return await self.async_request(
            "GET", f"/v1/monitors/{monitor_id}/iot/thirdPartySession"
        )

    async def probe_get(self, name: str, absolute_url: str) -> dict[str, Any]:
        """GET an arbitrary URL with the Gecko bearer; no raise (for API mapping)."""
        headers = {"Authorization": f"Bearer {self._token}"}
        async with self._session.get(absolute_url, headers=headers) as resp:
            text = await resp.text()
            entry: dict[str, Any] = {
                "name": name,
                "method": "GET",
                "url": absolute_url,
                "status": resp.status,
                "ok": resp.status < 400,
            }
            ct = (resp.headers.get("Content-Type") or "").lower()
            if "application/json" in ct:
                try:
                    entry["json"] = json.loads(text)
                except json.JSONDecodeError:
                    entry["text"] = text[:8000]
            else:
                entry["text"] = text[:4000]
            return entry

    async def probe_gecko_api_path(self, name: str, path: str) -> dict[str, Any]:
        if not path.startswith("/"):
            path = "/" + path
        return await self.probe_get(name, f"{self.api_url}{path}")


def _dedupe_probe_specs(specs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """One row per path (first name wins)."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for name, path in specs:
        if path in seen:
            continue
        seen.add(path)
        out.append((name, path))
    return out


def _monitor_ids_from_vessels(vessels: list[dict[str, Any]]) -> list[str]:
    monitors: list[str] = []
    for v in vessels:
        if not isinstance(v, dict):
            continue
        mid = (
            v.get("monitorId")
            or v.get("monitor_id")
            or v.get("vesselId")
            or v.get("vessel_id")
        )
        if mid:
            monitors.append(str(mid))
    return list(dict.fromkeys(monitors))


def _catalog_probe_specs_from_file(
    account_id: str,
    vessel_id: str,
    monitor_id: str,
    user_id: str,
    *,
    use_standins: bool,
) -> tuple[list[tuple[str, str]], dict[str, Any]]:
    """Instantiate GET paths from ``GECKO_APP_REST_PATH_CATALOG`` (Gecko app 1.9.0 templates).

    Tier **A** (default): only ``{accountId|vesselId|monitorId|userId}``.

    Tier **B** (``use_standins``): fills remaining ``{…}`` with conservative literals so
    you can discover **404 vs 403 vs 400** for more surface (still GET-only skips).

    ``customActionsVersion`` is appended as **0** (integer) for OpenAPI vessel list/detail
    routes under ``/v2``…``/v9`` matching the app client.
    """
    stats: dict[str, Any] = {
        "lines_total": 0,
        "lines_used": 0,
        "lines_skipped_placeholders": 0,
        "lines_skipped_policy": 0,
        "standin_mode": use_standins,
    }
    skipped_templates: list[dict[str, Any]] = []
    if not GECKO_APP_REST_PATH_CATALOG.is_file():
        return [], stats

    allow_ph = frozenset({"accountId", "vesselId", "monitorId", "userId"})
    skip_substrings = (
        "/engineering/",
        "/stripe/",
        "/v0/cmd",
        "/chemical/dose",
        "/v2/track",
        "/v2/logout",
    )
    skip_exact_templates = frozenset(
        (
            "/v2/user/{userId}",
            "/v4/accounts/{accountId}/vessels",
        )
    )
    # GET-probe unsafe or non-API noise from bundle
    skip_prefixes = ("/v1/stripe/", "/v2/engineering/", "/v1/engineering/")
    standins: dict[str, str] = {
        "messageId": "0",
        "accountChemicalId": "0",
        "actionType": "survey",
        "completionId": "0",
        "sessionId": "0",
        "encryptedAccountInfo": "0",
        "stripePriceLookupKey": "premium",
        "smartRoutineId": "0",
        "waterReportId": "0",
        "testStripScanId": "0",
        "manufacturer": "generic",
        "sku": "0",
        "uploadId": "0",
        "appId": "gecko",
        "mobileDeviceUuid": "00000000-0000-0000-0000-000000000000",
        "uuid": "00000000-0000-0000-0000-000000000000",
        "token": "0",
        "ticketId": "0",
        "serialNumber": "0",
        "spaMacAddress": "00:00:00:00:00:00",
        "accessoryId": "0",
        "chartDuration": "P30D",
        "latitude": "0",
        "longitude": "0",
        "platform": "android",
        "version": "1.9.0",
        "readingSource": "waterlab",
        "readingType": "ph",
        "chemicalUse": "chlorine",
    }

    specs: list[tuple[str, str]] = []
    raw_lines = GECKO_APP_REST_PATH_CATALOG.read_text(encoding="utf-8").splitlines()
    stats["lines_total"] = len(raw_lines)

    for idx, raw in enumerate(raw_lines):
        tpl = raw.strip()
        if not tpl.startswith("/"):
            continue
        if tpl in skip_exact_templates:
            stats["lines_skipped_policy"] += 1
            continue
        if any(s in tpl for s in skip_substrings) or tpl.startswith(skip_prefixes):
            stats["lines_skipped_policy"] += 1
            continue
        ph = set(re.findall(r"\{(\w+)\}", tpl))
        if not use_standins and ph and not ph.issubset(allow_ph):
            stats["lines_skipped_placeholders"] += 1
            skipped_templates.append({"template": tpl, "placeholders": sorted(ph)})
            continue
        if use_standins:
            unknown = ph - allow_ph - frozenset(standins.keys())
            if unknown:
                stats["lines_skipped_placeholders"] += 1
                skipped_templates.append(
                    {
                        "template": tpl,
                        "placeholders": sorted(ph),
                        "unknown": sorted(unknown),
                    }
                )
                continue

        if "vesselId" in ph and not vessel_id:
            stats["lines_skipped_policy"] += 1
            continue
        if "monitorId" in ph and not monitor_id:
            stats["lines_skipped_policy"] += 1
            continue
        if "userId" in ph and not user_id:
            stats["lines_skipped_policy"] += 1
            continue

        path = tpl
        path = path.replace("{accountId}", str(account_id))
        path = path.replace("{vesselId}", str(vessel_id))
        path = path.replace("{monitorId}", str(monitor_id))
        path = path.replace("{userId}", user_id)
        if use_standins:
            for key, val in standins.items():
                path = path.replace("{" + key + "}", val)

        if "{" in path:
            stats["lines_skipped_placeholders"] += 1
            skipped_templates.append(
                {"template": tpl, "reason": "unresolved_placeholder"}
            )
            continue

        if "?" not in path:
            if re.search(r"/v[2-9]/accounts/\d+/vessels/\d+$", path):
                path = f"{path}?customActionsVersion=0"
            elif re.search(r"/v[2-9]/accounts/\d+/vessels$", path):
                path = f"{path}?customActionsVersion=0"

        short = re.sub(r"[^a-zA-Z0-9]+", "_", tpl)[:72]
        specs.append((f"cat_{idx:03d}_{short}", path))
        stats["lines_used"] += 1

    stats["skipped_templates_sample"] = skipped_templates[:80]
    stats["skipped_templates_total"] = len(skipped_templates)
    return _dedupe_probe_specs(specs), stats


def _summarize_probe_results(probes: list[dict[str, Any]]) -> dict[str, Any]:
    by_status: dict[int, int] = {}
    successful: list[dict[str, Any]] = []
    failed_sample: list[dict[str, Any]] = []
    forbidden_403: list[dict[str, Any]] = []
    for p in probes:
        try:
            st = int(p.get("status") or 0)
        except (TypeError, ValueError):
            st = 0
        by_status[st] = by_status.get(st, 0) + 1
        if p.get("ok"):
            shape: dict[str, Any] | None = None
            jl = p.get("json")
            if isinstance(jl, dict):
                keys = sorted(jl.keys())
                shape = {"type": "object", "keys": keys[:60], "key_count": len(keys)}
            elif isinstance(jl, list):
                shape = {"type": "array", "length": len(jl)}
            elif jl is not None:
                shape = {"type": type(jl).__name__}
            successful.append(
                {
                    "name": p.get("name"),
                    "path": p.get("url", "").replace(API_BASE_URL, ""),
                    "url": p.get("url"),
                    "status": st,
                    "json_shape": shape,
                }
            )
        elif st == 403 and len(forbidden_403) < 120:
            jl = p.get("json")
            msg = ""
            if isinstance(jl, dict):
                msg = str(jl.get("message") or jl.get("error") or "")[:300]
            forbidden_403.append(
                {
                    "name": p.get("name"),
                    "path": (p.get("url") or "").replace(API_BASE_URL, ""),
                    "message": msg,
                }
            )
        elif len(failed_sample) < 45:
            jl = p.get("json")
            prev = (p.get("text") or "")[:240]
            if isinstance(jl, dict) and jl.get("message"):
                prev = str(jl.get("message"))[:240]
            failed_sample.append(
                {
                    "name": p.get("name"),
                    "path": (p.get("url") or "").replace(API_BASE_URL, ""),
                    "status": st,
                    "body_preview": prev,
                }
            )
    return {
        "counts_by_http_status": by_status,
        "successful_count": sum(1 for x in probes if x.get("ok")),
        "failed_count": sum(1 for x in probes if not x.get("ok")),
        "forbidden_403_count": sum(
            1 for x in probes if int(x.get("status") or 0) == 403
        ),
        "not_found_404_count": sum(
            1 for x in probes if int(x.get("status") or 0) == 404
        ),
        "successful_endpoints": successful,
        "forbidden_403_endpoints": forbidden_403,
        "failed_sample": failed_sample,
    }


async def _gather_probes_bounded(
    api: _TokenGeckoApi,
    specs: list[tuple[str, str]],
    concurrency: int,
) -> list[dict[str, Any]]:
    sem = asyncio.Semaphore(max(1, min(concurrency, 32)))

    async def one(name: str, path: str) -> dict[str, Any]:
        async with sem:
            return await api.probe_gecko_api_path(name, path)

    return list(await asyncio.gather(*(one(n, p) for n, p in specs)))


async def _build_rest_api_snapshot(
    api: _TokenGeckoApi,
    user_id: str,
    account_id: str,
    vessels: list[dict[str, Any]],
    user_info: dict[str, Any],
    concurrency: int,
    *,
    catalog_use_standins: bool,
) -> dict[str, Any]:
    """Auth0 userinfo + ``/v2/user`` + vessels + broad Gecko GET probe grid (token reuse)."""
    monitors_all = _monitor_ids_from_vessels(vessels)
    monitors = monitors_all[:REST_MAP_MONITOR_LIMIT]
    if len(monitors_all) > len(monitors):
        _LOG.warning(
            "REST map: probing %d/%d monitor ids (cap REST_MAP_MONITOR_LIMIT=%d)",
            len(monitors),
            len(monitors_all),
            REST_MAP_MONITOR_LIMIT,
        )

    first_vessel = vessels[0] if vessels and isinstance(vessels[0], dict) else {}
    vessel_id_str = str(first_vessel.get("vesselId") or "")
    monitor_primary = monitors[0] if monitors else ""
    if not vessel_id_str and vessels:
        _LOG.warning(
            "REST map: first vessel has no vesselId — catalog paths that need vesselId are skipped."
        )

    specs, catalog_stats = _catalog_probe_specs_from_file(
        account_id,
        vessel_id_str,
        monitor_primary,
        user_id,
        use_standins=catalog_use_standins,
    )

    probes: list[dict[str, Any]] = [
        await api.probe_get("auth0_userinfo", f"{api.auth0_url}/userinfo"),
        await api.probe_gecko_api_path("gecko_user_v2", f"/v2/user/{user_id}"),
        await api.probe_gecko_api_path(
            "gecko_vessels_v4", f"/v4/accounts/{account_id}/vessels"
        ),
    ]
    probes.extend(await _gather_probes_bounded(api, specs, concurrency))

    account = user_info.get("account") if isinstance(user_info, dict) else {}

    return {
        "generated_at": _vsl_oauth._token_now(),
        "api_base_url": API_BASE_URL,
        "auth0_base_url": api.auth0_url,
        "user_id": user_id,
        "account_id": account_id,
        "monitor_ids_probed": monitors,
        "monitor_ids_all": monitors_all,
        "monitor_ids_omitted_from_probe_grid": monitors_all[len(monitors) :],
        "vessel_count": len(vessels),
        "vessel_id_used_for_catalog": vessel_id_str or None,
        "probe_concurrency": concurrency,
        "gecko_path_probes_planned": len(specs),
        "rest_path_catalog_file": str(GECKO_APP_REST_PATH_CATALOG),
        "catalog_stats": catalog_stats,
        "discovery_hints": {
            "user_info_top_keys": sorted(user_info.keys())
            if isinstance(user_info, dict)
            else [],
            "user_account_top_keys": sorted(account.keys())
            if isinstance(account, dict)
            else [],
            "first_vessel_keys": sorted(first_vessel.keys()),
        },
        "summary": _summarize_probe_results(probes),
        "probes": probes,
    }


def _oauth_save_enabled(args: argparse.Namespace, file_vars: dict[str, str]) -> bool:
    if getattr(args, "no_save_token", False):
        return False
    flag = (
        (
            os.environ.get("GECKO_NO_SAVE_TOKEN")
            or file_vars.get("GECKO_NO_SAVE_TOKEN")
            or ""
        )
        .strip()
        .lower()
    )
    if flag in ("1", "true", "yes", "on"):
        return False
    return True


def _pick_monitor_id(vessels: list[dict[str, Any]], explicit: str | None) -> str:
    if explicit:
        return explicit
    for v in vessels:
        mid = v.get("monitorId") or v.get("vesselId") or v.get("monitor_id")
        if mid:
            return str(mid)
    raise RuntimeError("No monitor id on vessels; set GECKO_MONITOR_ID in env file.")


def _sync_mqtt_probe(broker_url: str, monitor_id: str) -> dict[str, Any] | None:
    """Connect, load configuration + shadow state, return raw state dict for metrics."""
    from gecko_iot_client import GeckoIotClient
    from gecko_iot_client.transporters.mqtt import MqttTransporter

    transporter = MqttTransporter(
        broker_url=broker_url,
        monitor_id=monitor_id,
        token_refresh_callback=None,
    )
    client = GeckoIotClient(monitor_id, transporter, config_timeout=CONFIG_TIMEOUT)
    try:
        client.connect()
        state = getattr(client, "_state", None)
        if isinstance(state, dict):
            return state
        _LOG.warning("Client has no dict _state after connect (got %r)", type(state))
        return None
    finally:
        try:
            client.disconnect()
        except Exception as exc:  # noqa: BLE001
            _LOG.debug("disconnect: %s", exc)


async def async_main(env_path: Path, args: argparse.Namespace) -> int:
    file_vars = _vsl_oauth._load_env_file(env_path)
    monitor_opt = (
        os.environ.get("GECKO_MONITOR_ID") or file_vars.get("GECKO_MONITOR_ID") or ""
    ).strip() or None

    token_path = _vsl_oauth._oauth_token_path(file_vars)

    async with aiohttp.ClientSession() as session:
        try:
            access, to_save = await _vsl_oauth._resolve_access_token(
                session, file_vars, args, token_path
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.error("%s", exc)
            return 1

        if to_save and _oauth_save_enabled(args, file_vars):
            _vsl_oauth._save_oauth_token(token_path, to_save)

        api = _TokenGeckoApi(session, access)
        user_id = await api.async_get_user_id()
        user_info = await api.async_get_user_info(user_id)
        account = user_info.get("account") or {}
        account_id = str(account.get("accountId") or account.get("id") or "")
        if not account_id:
            _LOG.error(
                "Could not resolve accountId from user info keys: %s", user_info.keys()
            )
            return 1

        vessels = await api.async_get_vessels(account_id)

        if getattr(args, "dump_api", False):
            conc = max(1, min(32, int(getattr(args, "api_probe_concurrency", 6) or 6)))
            snapshot = await _build_rest_api_snapshot(
                api,
                user_id,
                account_id,
                vessels,
                user_info,
                conc,
                catalog_use_standins=bool(getattr(args, "dump_api_standins", False)),
            )
            out_path = getattr(args, "api_dump_out", None)
            text = json.dumps(snapshot, indent=2, default=str)
            if out_path:
                out_path = Path(out_path)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(text, encoding="utf-8")
                try:
                    out_path.chmod(0o600)
                except OSError:
                    pass
                _LOG.info("Wrote REST API snapshot to %s", out_path)
            else:
                print("=== gecko_rest_api_snapshot ===")
                print(text)

        if getattr(args, "dump_api_only", False):
            return 0

        monitor_id = _pick_monitor_id(vessels, monitor_opt)
        _LOG.info("Using monitor_id=%s", monitor_id)

        live = await api.async_get_monitor_livestream(monitor_id)
        broker_url = live.get("brokerUrl")
        if not broker_url:
            _LOG.error("Livestream response missing brokerUrl: %s", live)
            return 1

    _LOG.info("Connecting MQTT / loading shadow (this can take ~30s)...")
    state = await asyncio.to_thread(_sync_mqtt_probe, broker_url, monitor_id)
    if not state:
        _LOG.error("No shadow state retrieved.")
        return 1

    _install_homeassistant_stubs()
    sm_path = REPO_ROOT / "custom_components" / "gecko" / "shadow_metrics.py"
    spec = importlib.util.spec_from_file_location("_gecko_shadow_metrics_live", sm_path)
    if spec is None or spec.loader is None:
        _LOG.error("Could not load shadow_metrics from %s", sm_path)
        return 1
    shadow_metrics = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shadow_metrics)

    topo = shadow_metrics.shadow_topology_summary(state)
    metrics = shadow_metrics.extract_extension_metrics(state)
    cap = getattr(shadow_metrics, "_MAX_SENSORS", 64)

    print("=== shadow_topology_summary ===")
    print(json.dumps(topo, indent=2, default=str))
    print()
    print(f"=== extract_extension_metrics ({len(metrics)} paths, cap {cap}) ===")
    for path in sorted(metrics):
        dc, unit = shadow_metrics.infer_sensor_metadata(path)
        enabled = shadow_metrics.chemistry_metric_enabled_by_default(path)
        dc_s = getattr(dc, "value", dc) if dc is not None else None
        print(
            f"{path} = {metrics[path]!r}  "
            f"[device_class={dc_s!r} unit={unit!r} default_enabled={enabled}]"
        )
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env",
        type=Path,
        default=Path(os.environ.get("GECKO_ENV_FILE", DEFAULT_ENV_PATH)),
        help=f"Env file (default: {DEFAULT_ENV_PATH})",
    )
    parser.add_argument(
        "--oauth-redirect",
        choices=("my-home-assistant", "loopback"),
        default="my-home-assistant",
        help="my-home-assistant = public my.home-assistant.io redirect + paste URL (default; no HA on LAN required). loopback = 127.0.0.1 (Gecko Auth0 rejects this today → Oops page).",
    )
    parser.add_argument(
        "--oauth-redirect-uri",
        default="",
        metavar="URL",
        help="Override redirect URI (e.g. https://homeassistant.local:8123/auth/external/callback); then paste the full URL from the browser",
    )
    parser.add_argument(
        "--oauth-port",
        type=int,
        default=None,
        metavar="PORT",
        help="With --oauth-redirect loopback: fixed or ephemeral port (default: ephemeral)",
    )
    parser.add_argument(
        "--oauth-timeout",
        type=float,
        default=300.0,
        help="Seconds to wait for OAuth callback (default: 300)",
    )
    parser.add_argument(
        "--no-save-token",
        action="store_true",
        help="Do not write .secrets/gecko_oauth.json (or GECKO_TOKEN_FILE) after login/refresh.",
    )
    parser.add_argument(
        "--dump-api",
        action="store_true",
        help="After auth, GET Auth0 userinfo + core REST + paths from scripts/gecko_paths_raw_app_1.9.0.txt (see --api-dump-out).",
    )
    parser.add_argument(
        "--dump-api-standins",
        action="store_true",
        help="With --dump-api: also probe catalog paths that need extra ids (messageId, actionType, …) using fixed stand-in values (noisy; many 400/404).",
    )
    parser.add_argument(
        "--api-dump-out",
        type=Path,
        default=None,
        metavar="PATH",
        help="With --dump-api: write snapshot JSON to this file (600 perms) instead of stdout.",
    )
    parser.add_argument(
        "--dump-api-only",
        action="store_true",
        help="With --dump-api: exit after writing the REST snapshot (skip MQTT / shadow metrics).",
    )
    parser.add_argument(
        "--api-probe-concurrency",
        type=int,
        default=6,
        metavar="N",
        help="Parallel GET probes for --dump-api (default: 6, max: 32).",
    )

    args = parser.parse_args()
    if getattr(args, "dump_api_only", False):
        args.dump_api = True
    try:
        sys.exit(asyncio.run(async_main(args.env, args)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
