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
import base64
import hashlib
import importlib.util
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlparse

# ---------------------------------------------------------------------------
# Repo paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_PATH = REPO_ROOT / ".secrets" / "gecko.env"
DEFAULT_OAUTH_TOKEN_PATH = REPO_ROOT / ".secrets" / "gecko_oauth.json"
TOKEN_EXPIRY_SKEW_SEC = 120.0
# REST ``--dump-api`` map: avoid huge fan-out on accounts with many spas
REST_MAP_MONITOR_LIMIT = 15
# Path templates extracted from Gecko Android 1.9.0 (com.geckoportal.gecko) web bundle.
GECKO_APP_REST_PATH_CATALOG = REPO_ROOT / "scripts" / "gecko_paths_raw_app_1.9.0.txt"

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

# OAuth / API (same as custom_components/gecko)
AUTH0_AUTHORIZE_URL = "https://gecko-prod.us.auth0.com/authorize"
AUTH0_TOKEN_URL = "https://gecko-prod.us.auth0.com/oauth/token"
OAUTH2_CLIENT_ID = "L81oh6hgUsvMg40TgTGoz4lxNy8eViM0"
API_AUDIENCE = "https://api.geckowatermonitor.com"
API_BASE_URL = "https://api.geckowatermonitor.com"
CONFIG_TIMEOUT = 30.0

# Same redirect Home Assistant uses when the "My Home Assistant" / Cloud link is
# available. Gecko's Auth0 app allowlists this; random http://127.0.0.1:... URLs are
# not, which triggers Auth0's generic "Oops, something went wrong" page.
OAUTH_REDIRECT_MY_HOME_ASSISTANT = "https://my.home-assistant.io/redirect/oauth"

_LOG = logging.getLogger("verify_shadow_live")


def _log_oauth_stuck_after_login_hints(state: str) -> None:
    """Browser often hangs after Apple; user can still recover the callback URL from DevTools."""
    _LOG.info(
        "If Apple / Auth0 login **finishes** but the tab **never** lands on my.home-assistant.io "
        "(spinner, blank page, or stuck on appleid.apple.com):\n"
        "  1. DevTools (F12) → **Network** → check **Preserve log** → reload or restart login from the Authorize URL above.\n"
        "  2. After Apple completes, find a row for **my.home-assistant.io** (document or xhr); open it → **Headers** → "
        "copy **Request URL** (must contain **code=** and **state=** matching this session’s state **%s**).\n"
        "  3. Try **Safari** (often best for Apple ID) or **Chrome** incognito; pause ad-block / strict tracking protection for this tab.\n"
        "  4. Complete any extra Apple screen (Hide My Email, Trust this browser, 2FA); do not close the tab until a my.home-assistant.io URL appears or you copied it from Network.\n"
        "You can paste that URL (or only the **code** value) into the terminal when prompted.",
        state,
    )


def _cmd_exe_start_url(cmd_path: str, url: str) -> bool:
    """Open ``url`` in the default Windows browser.

    ``cmd.exe`` treats ``&`` as a command separator unless the URL is quoted. Passing
    ``start "" https://...?a=1&client_id=...`` truncates at the first ``&``, which
    produces Auth0 **invalid_request: Missing required parameter: client_id**.
    """
    try:
        # One /c argument: JSON-quoted URL survives CMD parsing (handles " in URL).
        line = f'start "" {json.dumps(url)}'
        subprocess.Popen(
            [cmd_path, "/c", line],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        _LOG.debug("cmd.exe start URL failed: %s", exc)
        return False
    return True


def _running_under_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        proc = Path("/proc/version")
        if proc.is_file() and "microsoft" in proc.read_text(encoding="utf-8", errors="ignore").lower():
            return True
    except OSError:
        pass
    return False


def _open_gecko_login_in_browser(url: str) -> bool:
    """Open Auth0 Universal Login (Apple, Google, password, …).

    On **WSL**, ``webbrowser`` often invokes ``xdg-open`` with no Linux browser installed
    while reporting success. We open the **Windows** default browser via ``cmd.exe start``
    first so Sign in with Apple works in Edge/Chrome on the host.
    """
    cmd_candidates = (
        shutil.which("cmd.exe"),
        "/mnt/c/Windows/System32/cmd.exe",
        "/mnt/c/WINDOWS/System32/cmd.exe",
    )
    if _running_under_wsl():
        for cmd_path in cmd_candidates:
            if not cmd_path or not Path(cmd_path).exists():
                continue
            if _cmd_exe_start_url(cmd_path, url):
                _LOG.info(
                    "Launched login in Windows default browser (use Apple ID / iCloud if prompted)."
                )
                return True
        _LOG.warning("WSL detected but cmd.exe start failed; trying other methods…")

    if webbrowser.open(url):
        _LOG.info("Opened default browser for Gecko / Auth0 login.")
        return True

    _LOG.warning("webbrowser.open failed; trying more fallbacks…")
    for cmd_path in cmd_candidates:
        if not cmd_path or not Path(cmd_path).exists():
            continue
        if _cmd_exe_start_url(cmd_path, url):
            _LOG.info("Launched login URL via Windows (cmd.exe start).")
            return True

    for exe in ("wslview", "xdg-open"):
        binpath = shutil.which(exe)
        if not binpath:
            continue
        try:
            subprocess.Popen(
                [binpath, url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _LOG.info("Opened login URL via %s", exe)
            return True
        except OSError:
            continue

    _LOG.error("Could not open a browser. Open this URL manually:\n%s", url)
    return False


def _install_homeassistant_stubs() -> None:
    """Minimal stubs so ``shadow_metrics`` imports without Home Assistant."""
    ha = ModuleType("homeassistant")
    const = ModuleType("homeassistant.const")

    class _UnitOfTemperature:
        CELSIUS = "°C"

    const.UnitOfTemperature = _UnitOfTemperature
    sys.modules.setdefault("homeassistant", ha)
    sys.modules.setdefault("homeassistant.const", const)


def _load_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.is_file():
        return data
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key:
            data[key] = val
    return data


def _oauth_token_path(file_vars: dict[str, str]) -> Path:
    raw = (
        os.environ.get("GECKO_TOKEN_FILE")
        or file_vars.get("GECKO_TOKEN_FILE")
        or str(DEFAULT_OAUTH_TOKEN_PATH)
    ).strip()
    return Path(raw).expanduser()


def _token_now() -> float:
    return time.time()


def _normalize_auth0_token(body: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """Shape suitable for JSON on disk (no secrets beyond OAuth fields)."""
    t = _token_now() if now is None else now
    expires_in = body.get("expires_in")
    try:
        sec = float(expires_in) if expires_in is not None else 3600.0
    except (TypeError, ValueError):
        sec = 3600.0
    out: dict[str, Any] = {
        "access_token": body.get("access_token"),
        "token_type": body.get("token_type") or "Bearer",
        "expires_at": t + sec,
        "saved_at": t,
    }
    if body.get("refresh_token"):
        out["refresh_token"] = body["refresh_token"]
    if body.get("id_token"):
        out["id_token"] = body["id_token"]
    if body.get("scope"):
        out["scope"] = body["scope"]
    return out


def _merge_refresh_preserves_old_refresh(saved: dict[str, Any], new_body: dict[str, Any]) -> dict[str, Any]:
    rb = dict(new_body)
    if not rb.get("refresh_token") and saved.get("refresh_token"):
        rb["refresh_token"] = saved["refresh_token"]
    return _normalize_auth0_token(rb)


def _token_file_access_valid(data: dict[str, Any], *, now: float, skew: float = TOKEN_EXPIRY_SKEW_SEC) -> bool:
    at = data.get("access_token")
    if not at or not isinstance(at, str):
        return False
    exp = data.get("expires_at")
    try:
        exp_f = float(exp)
    except (TypeError, ValueError):
        return True
    return now + skew < exp_f


def _save_oauth_token(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2)
    path.write_text(text, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    _LOG.info("Wrote OAuth token cache %s (mode 600)", path)


def _load_oauth_token_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _LOG.warning("Could not read token file %s: %s", path, exc)
        return None
    return raw if isinstance(raw, dict) else None


async def _auth0_refresh_token(session: Any, refresh_token: str) -> dict[str, Any]:
    payload = {
        "grant_type": "refresh_token",
        "client_id": OAUTH2_CLIENT_ID,
        "refresh_token": refresh_token,
        "audience": API_AUDIENCE,
    }
    body = urlencode(payload)
    async with session.post(
        AUTH0_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    ) as resp:
        text = await resp.text()
        if resp.status >= 400:
            raise RuntimeError(f"Auth0 refresh_token failed HTTP {resp.status}: {text[:800]}")
    return json.loads(text)


async def _try_load_or_refresh_stored_token(
    session: Any,
    path: Path,
) -> tuple[str | None, dict[str, Any] | None]:
    """Return (access_token, normalized_dict_to_save) or (None, None)."""
    data = _load_oauth_token_file(path)
    if not data:
        return None, None
    now = _token_now()
    refresh = data.get("refresh_token")
    if _token_file_access_valid(data, now=now):
        at = data.get("access_token")
        return (str(at) if at else None), None

    if not refresh or not isinstance(refresh, str):
        _LOG.info("Stored token at %s is expired and has no refresh_token; need new login.", path)
        return None, None

    _LOG.info("Refreshing Auth0 token (stored access token expired or missing)…")
    refreshed = await _auth0_refresh_token(session, refresh)
    norm = _merge_refresh_preserves_old_refresh(data, refreshed)
    if not norm.get("access_token"):
        raise RuntimeError(f"refresh_token exchange: no access_token: keys={list(refreshed.keys())}")
    return str(norm["access_token"]), norm


def _pkce_verifier_and_challenge() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for S256 PKCE."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _authorize_url(redirect_uri: str, state: str, code_challenge: str) -> str:
    q = {
        "response_type": "code",
        "client_id": OAUTH2_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": "openid profile email offline_access",
        "audience": API_AUDIENCE,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTH0_AUTHORIZE_URL}?{urlencode(q)}"


def _unwrap_my_home_assistant_paste(raw: str) -> str:
    """Turn ``/redirect/_change/?redirect=oauth%2F%3Fcode%3D...`` into a normal oauth query URL.

    My Home Assistant sometimes shows the configure-instance page (``/redirect/_change/``)
    with the real OAuth query nested inside the ``redirect=`` parameter instead of a
    bare ``/redirect/oauth?`` URL.
    """
    u = raw.strip()
    try:
        pu = urlparse(u)
    except ValueError:
        return raw
    if "my.home-assistant.io" not in (pu.netloc or "").lower():
        return u
    q = parse_qs(pu.query)
    inner = (q.get("redirect") or [None])[0]
    if not inner:
        return u
    inner_decoded = unquote(inner)
    if "code=" not in inner_decoded:
        return u
    if inner_decoded.startswith("oauth/") and "?" in inner_decoded:
        _, qstr = inner_decoded.split("?", 1)
    elif inner_decoded.startswith("oauth?"):
        _, qstr = inner_decoded.split("?", 1)
    else:
        return u
    qstr = unquote(qstr)
    return f"https://my.home-assistant.io/redirect/oauth?{qstr}"


def _oauth_state_matches(received: str | None, expected: str) -> bool:
    """Compare Auth0 ``state``; My Home Assistant may prefix with ``my_``."""
    if not received or not expected:
        return False
    if received == expected:
        return True
    if received == f"my_{expected}":
        return True
    if expected == f"my_{received}":
        return True
    if received.startswith("my_") and received[3:] == expected:
        return True
    if expected.startswith("my_") and expected[3:] == received:
        return True
    return False


def _input_paste_line(prompt: str, hint_interval: float = 45.0) -> str:
    """Read one line from stdin; print hints periodically (Apple 2FA can take a long time).

    Uses a **background thread** for ``readline()`` instead of ``select()`` on stdin.
    ``select()`` is unreliable on some WSL / IDE pseudo-TTYs (timeouts even after paste+Enter),
    which made it look like the URL was ignored.
    """
    if not sys.stdin.isatty():
        try:
            return input(prompt).strip()
        except EOFError:
            return ""

    done = threading.Event()
    out: dict[str, str] = {}

    def _reader() -> None:
        try:
            line = sys.stdin.readline()
            out["line"] = line if line is not None else ""
        except EOFError:
            out["line"] = ""
        finally:
            done.set()

    sys.stdout.write(prompt)
    sys.stdout.flush()
    threading.Thread(target=_reader, daemon=True).start()
    while not done.wait(timeout=hint_interval):
        sys.stdout.write(
            "\nStill waiting — after Apple verification: paste the **full URL** from the "
            "address bar if it shows ?code= and state=.\n"
            "If the URL never appears, press F12 → Network → filter \"my.home\" or \"oauth\" "
            "→ click the redirect to my.home-assistant.io → copy **Request URL**, "
            "or copy only the long **code** value from that URL and paste **one line** here.\n"
            + prompt
        )
        sys.stdout.flush()
    return (out.get("line") or "").strip()


def _parse_oauth_paste(raw: str, expected_state: str) -> tuple[str | None, str | None]:
    """Parse full callback URL, ``/_change/`` wrapper, or a bare authorization ``code`` line."""
    u = raw.strip()
    if not u:
        return None, "empty paste"
    unwrapped = _unwrap_my_home_assistant_paste(u)
    low = unwrapped.lower()
    if "code=" in low or "://" in unwrapped or (
        unwrapped.startswith("/") and "?" in unwrapped
    ):
        return _parse_auth_redirect(unwrapped, expected_state)
    if any(c in u for c in "\n\t\r "):
        return None, "paste one line only (full URL or authorization code)"
    code = u.strip('"').strip("'")
    if len(code) < 12:
        return None, "authorization code looks too short; paste the full URL or the code from DevTools"
    return code, None


def _parse_auth_redirect(
    url_or_path: str, expected_state: str
) -> tuple[str | None, str | None]:
    """Return (authorization_code, error_message)."""
    url_or_path = _unwrap_my_home_assistant_paste(url_or_path)
    if "://" not in url_or_path and url_or_path.startswith("/"):
        parsed = urlparse(url_or_path)
    else:
        parsed = urlparse(url_or_path)
    qs = parse_qs(parsed.query)
    st = (qs.get("state") or [None])[0]
    if not _oauth_state_matches(st, expected_state):
        return None, "state mismatch (expected OAuth session to match)"
    if qs.get("error"):
        err = (qs.get("error_description") or qs.get("error") or ["unknown"])[0]
        return None, err
    code = (qs.get("code") or [None])[0]
    if not code:
        return None, "no code= in callback URL"
    return code, None


def _oauth_pkce_remote_redirect(
    redirect_uri: str,
    challenge: str,
    state: str,
) -> tuple[str | None, str | None, str]:
    """Use an Auth0-allowed HTTPS redirect (e.g. My Home Assistant); user pastes result URL."""
    auth_url = _authorize_url(redirect_uri, state, challenge)
    _LOG.info(
        "Opening Auth0 (Apple / social login). Redirect URI:\n  %s\n"
        "You do not need Home Assistant on this PC or network — only this public HTTPS callback.\n"
        "After Apple / sign-in, return here: paste the **full URL** (?code= and state=), "
        "a ``/_change/?redirect=…`` URL, **or** only the authorization code from DevTools → Network.\n"
        "Hints repeat every ~45s while waiting.",
        redirect_uri,
    )
    _LOG.info("Authorize URL (open manually if the browser did not start):\n%s", auth_url)
    _open_gecko_login_in_browser(auth_url)
    _log_oauth_stuck_after_login_hints(state)
    pasted = _input_paste_line("Paste URL or authorization code> ")
    if not pasted:
        return None, "no URL pasted", redirect_uri
    code, err = _parse_oauth_paste(pasted, state)
    return code, err, redirect_uri


def _oauth_pkce_loopback(
    challenge: str,
    state: str,
    prefer_port: int | None,
    wait_s: float,
) -> tuple[str | None, str | None, str]:
    """Bind loopback HTTP server, open Auth0 (Apple) login, return (code, err, redirect_uri)."""
    outcome: dict[str, str] = {}
    stop = threading.Event()

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/favicon.ico":
                self.send_response(404)
                self.end_headers()
                return
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            code, err = _parse_auth_redirect(self.path, state)
            if code:
                outcome["code"] = code
            if err:
                outcome["err"] = err
            self.send_response(200)
            self.end_headers()
            self.wfile.write(
                b"<html><body>Gecko login OK. You can close this tab and return to the terminal.</body></html>"
            )
            stop.set()

    httpd: HTTPServer | None = None
    port: int = 0
    if prefer_port:
        try:
            httpd = HTTPServer(("127.0.0.1", prefer_port), _Handler)
            port = prefer_port
        except OSError:
            httpd = None
    if httpd is None:
        try:
            httpd = HTTPServer(("127.0.0.1", 0), _Handler)
            port = httpd.server_address[1]
        except OSError as exc:
            return None, f"cannot bind OAuth callback on loopback: {exc}", ""

    redirect_uri = f"http://127.0.0.1:{port}/callback"
    auth_url = _authorize_url(redirect_uri, state, challenge)

    def _worker() -> None:
        assert httpd is not None
        httpd.timeout = 1.0
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline and not stop.is_set():
            httpd.handle_request()
            if outcome.get("code") or outcome.get("err"):
                break
        try:
            httpd.server_close()
        except OSError:
            pass

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    time.sleep(0.35)
    _LOG.info("Starting browser OAuth (PKCE). If login does not start, open:\n%s", auth_url)
    _open_gecko_login_in_browser(auth_url)
    th.join(timeout=wait_s + 15.0)

    if outcome.get("code"):
        return outcome["code"], None, redirect_uri

    _LOG.info(
        "If the browser showed a connection error but the address bar contains "
        "?code=…, paste the full URL or authorization code (see DevTools → Network)."
    )
    pasted = _input_paste_line("Callback URL or authorization code> ")
    if pasted:
        code, perr = _parse_oauth_paste(pasted, state)
        return code, perr, redirect_uri

    return None, outcome.get("err") or "OAuth timeout or no code", redirect_uri


async def _auth0_exchange_code(
    session: Any, code: str, redirect_uri: str, code_verifier: str
) -> dict[str, Any]:
    payload = {
        "grant_type": "authorization_code",
        "client_id": OAUTH2_CLIENT_ID,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    body = urlencode(payload)
    async with session.post(
        AUTH0_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    ) as resp:
        text = await resp.text()
        if resp.status >= 400:
            raise RuntimeError(
                f"Auth0 code exchange failed HTTP {resp.status}: {text[:800]}"
            )
    return json.loads(text)


async def _auth0_password_token(
    session: Any, username: str, password: str
) -> dict[str, Any]:
    payload = {
        "grant_type": "password",
        "username": username,
        "password": password,
        "audience": API_AUDIENCE,
        "client_id": OAUTH2_CLIENT_ID,
        "scope": "openid profile email offline_access",
    }
    body = urlencode(payload)
    async with session.post(
        AUTH0_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    ) as resp:
        raw = await resp.text()
        if resp.status >= 400:
            raise RuntimeError(
                f"Auth0 password grant failed HTTP {resp.status}: {raw[:500]}"
            )
    return json.loads(raw)


async def _resolve_access_token(
    session: Any,
    file_vars: dict[str, str],
    args: argparse.Namespace,
    token_path: Path,
) -> tuple[str, dict[str, Any] | None]:
    """Return (access_token, oauth_record_to_save_or_none).

    ``oauth_record_to_save`` is written to ``token_path`` when non-None and saving is
    enabled: refreshed tokens, password grant, or authorization-code exchange.
    """
    access = (
        os.environ.get("GECKO_ACCESS_TOKEN")
        or file_vars.get("GECKO_ACCESS_TOKEN")
        or ""
    ).strip()
    if access:
        return access, None

    acc2, refresh_save = await _try_load_or_refresh_stored_token(session, token_path)
    if acc2:
        return acc2, refresh_save

    user = (
        os.environ.get("GECKO_USERNAME")
        or file_vars.get("GECKO_USERNAME")
        or ""
    ).strip()
    password = os.environ.get("GECKO_PASSWORD") or file_vars.get("GECKO_PASSWORD") or ""
    if user and password:
        tok = await _auth0_password_token(session, user, password)
        norm = _normalize_auth0_token(tok)
        at = norm.get("access_token")
        if not at:
            raise RuntimeError(f"password grant: no access_token: {list(tok.keys())}")
        return str(at), norm

    prefer_raw = getattr(args, "oauth_port", None) or os.environ.get("GECKO_OAUTH_PORT")
    prefer_port: int | None = int(prefer_raw) if prefer_raw else None
    verifier, challenge = _pkce_verifier_and_challenge()
    state = secrets.token_urlsafe(16)

    custom_uri = (
        (getattr(args, "oauth_redirect_uri", None) or "").strip()
        or (os.environ.get("GECKO_OAUTH_REDIRECT_URI") or "").strip()
    )
    mode = (os.environ.get("GECKO_OAUTH_REDIRECT") or getattr(args, "oauth_redirect", "") or "").strip()
    if mode not in ("my-home-assistant", "loopback"):
        mode = "my-home-assistant"

    if custom_uri:
        code, err, redirect_uri = await asyncio.to_thread(
            _oauth_pkce_remote_redirect,
            custom_uri,
            challenge,
            state,
        )
    elif mode == "loopback":
        code, err, redirect_uri = await asyncio.to_thread(
            _oauth_pkce_loopback,
            challenge,
            state,
            prefer_port,
            float(args.oauth_timeout),
        )
    else:
        code, err, redirect_uri = await asyncio.to_thread(
            _oauth_pkce_remote_redirect,
            OAUTH_REDIRECT_MY_HOME_ASSISTANT,
            challenge,
            state,
        )
    if err or not code:
        raise RuntimeError(err or "OAuth failed")

    tok = await _auth0_exchange_code(session, code, redirect_uri, verifier)
    norm = _normalize_auth0_token(tok)
    at = norm.get("access_token")
    if not at:
        raise RuntimeError(f"code exchange: no access_token: {list(tok.keys())}")
    return str(at), norm


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
        async with self._session.request(method, url, headers=headers, **kwargs) as response:
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
        mid = v.get("monitorId") or v.get("monitor_id") or v.get("vesselId") or v.get("vessel_id")
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
                    {"template": tpl, "placeholders": sorted(ph), "unknown": sorted(unknown)}
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
            skipped_templates.append({"template": tpl, "reason": "unresolved_placeholder"})
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
        "forbidden_403_count": sum(1 for x in probes if int(x.get("status") or 0) == 403),
        "not_found_404_count": sum(1 for x in probes if int(x.get("status") or 0) == 404),
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
        await api.probe_gecko_api_path("gecko_vessels_v4", f"/v4/accounts/{account_id}/vessels"),
    ]
    probes.extend(await _gather_probes_bounded(api, specs, concurrency))

    account = user_info.get("account") if isinstance(user_info, dict) else {}

    return {
        "generated_at": _token_now(),
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
            "user_info_top_keys": sorted(user_info.keys()) if isinstance(user_info, dict) else [],
            "user_account_top_keys": sorted(account.keys()) if isinstance(account, dict) else [],
            "first_vessel_keys": sorted(first_vessel.keys()),
        },
        "summary": _summarize_probe_results(probes),
        "probes": probes,
    }


def _oauth_save_enabled(args: argparse.Namespace, file_vars: dict[str, str]) -> bool:
    if getattr(args, "no_save_token", False):
        return False
    flag = (
        os.environ.get("GECKO_NO_SAVE_TOKEN")
        or file_vars.get("GECKO_NO_SAVE_TOKEN")
        or ""
    ).strip().lower()
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
    file_vars = _load_env_file(env_path)
    monitor_opt = (
        os.environ.get("GECKO_MONITOR_ID")
        or file_vars.get("GECKO_MONITOR_ID")
        or ""
    ).strip() or None

    token_path = _oauth_token_path(file_vars)

    async with aiohttp.ClientSession() as session:
        try:
            access, to_save = await _resolve_access_token(
                session, file_vars, args, token_path
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.error("%s", exc)
            return 1

        if to_save and _oauth_save_enabled(args, file_vars):
            _save_oauth_token(token_path, to_save)

        api = _TokenGeckoApi(session, access)
        user_id = await api.async_get_user_id()
        user_info = await api.async_get_user_info(user_id)
        account = user_info.get("account") or {}
        account_id = str(account.get("accountId") or account.get("id") or "")
        if not account_id:
            _LOG.error("Could not resolve accountId from user info keys: %s", user_info.keys())
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
        print(
            f"{path} = {metrics[path]!r}  "
            f"[device_class={dc!r} unit={unit!r} default_enabled={enabled}]"
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
