"""OAuth2 / PKCE / token file helpers for ``verify_shadow_live``."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OAUTH_TOKEN_PATH = REPO_ROOT / ".secrets" / "gecko_oauth.json"
TOKEN_EXPIRY_SKEW_SEC = 120.0

AUTH0_AUTHORIZE_URL = "https://gecko-prod.us.auth0.com/authorize"
AUTH0_TOKEN_URL = "https://gecko-prod.us.auth0.com/oauth/token"
OAUTH2_CLIENT_ID = "L81oh6hgUsvMg40TgTGoz4lxNy8eViM0"
API_AUDIENCE = "https://api.geckowatermonitor.com"

OAUTH_REDIRECT_MY_HOME_ASSISTANT = "https://my.home-assistant.io/redirect/oauth"

_LOG = logging.getLogger("verify_shadow_live")


def _json_auth_response(text: str, *, context: str) -> dict[str, Any]:
    """Parse Auth0 JSON body; raise clear errors on HTML or other non-JSON replies."""
    try:
        val = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{context}: expected JSON, got {exc}; body starts with {text[:240]!r}"
        ) from exc
    if not isinstance(val, dict):
        raise RuntimeError(f"{context}: expected JSON object, got {type(val).__name__}")
    return val


def _parse_oauth_listen_port(
    args: argparse.Namespace,
) -> int | None:
    """Loopback port from CLI or ``GECKO_OAUTH_PORT``; ``None`` if unset or invalid."""
    prefer_raw = getattr(args, "oauth_port", None) or os.environ.get("GECKO_OAUTH_PORT")
    if prefer_raw is None or prefer_raw == "":
        return None
    try:
        return int(str(prefer_raw).strip(), 10)
    except ValueError as exc:
        raise RuntimeError(
            f"Invalid oauth port {prefer_raw!r} "
            "(use a valid TCP port integer, or omit for ephemeral port)"
        ) from exc


def _log_oauth_stuck_after_login_hints(state: str) -> None:
    """Browser often hangs after Apple; user can still recover the callback URL from DevTools."""
    _LOG.info(
        "If Apple / Auth0 login **finishes** but the tab **never** lands on "
        "my.home-assistant.io (spinner, blank page, or stuck on appleid.apple.com):\n"
        "  1. DevTools (F12) → **Network** → check **Preserve log** → reload or restart login "
        "from the Authorize URL above.\n"
        "  2. After Apple completes, find a row for **my.home-assistant.io** (document or xhr); "
        "open it → **Headers** → copy **Request URL** (must contain **code=** and **state=** "
        "matching this session’s state **%s**).\n"
        "  3. Try **Safari** (often best for Apple ID) or **Chrome** incognito; pause ad-block / "
        "strict tracking protection for this tab.\n"
        "  4. Complete any extra Apple screen (Hide My Email, Trust this browser, 2FA); do not "
        "close the tab until a my.home-assistant.io URL appears or you copied it from Network.\n"
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
        subprocess.Popen(  # pylint: disable=consider-using-with
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
        if (
            proc.is_file()
            and "microsoft" in proc.read_text(encoding="utf-8", errors="ignore").lower()
        ):
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
            subprocess.Popen(  # pylint: disable=consider-using-with
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


def _normalize_auth0_token(
    body: dict[str, Any], *, now: float | None = None
) -> dict[str, Any]:
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


def _merge_refresh_preserves_old_refresh(
    saved: dict[str, Any], new_body: dict[str, Any]
) -> dict[str, Any]:
    rb = dict(new_body)
    if not rb.get("refresh_token") and saved.get("refresh_token"):
        rb["refresh_token"] = saved["refresh_token"]
    return _normalize_auth0_token(rb)


def _token_file_access_valid(
    data: dict[str, Any], *, now: float, skew: float = TOKEN_EXPIRY_SKEW_SEC
) -> bool:
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
            raise RuntimeError(
                f"Auth0 refresh_token failed HTTP {resp.status}: {text[:800]}"
            )
    return _json_auth_response(text, context="Auth0 refresh_token response")


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
        _LOG.info(
            "Stored token at %s is expired and has no refresh_token; need new login.",
            path,
        )
        return None, None

    _LOG.info("Refreshing Auth0 token (stored access token expired or missing)…")
    refreshed = await _auth0_refresh_token(session, refresh)
    norm = _merge_refresh_preserves_old_refresh(data, refreshed)
    if not norm.get("access_token"):
        raise RuntimeError(
            f"refresh_token exchange: no access_token: keys={list(refreshed.keys())}"
        )
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
    pu = urlparse(u)
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

    def _strip_my(value: str) -> str:
        return value[3:] if value.startswith("my_") else value

    return (
        _strip_my(received) == _strip_my(expected)
        or received == f"my_{expected}"
        or expected == f"my_{received}"
    )


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
            'If the URL never appears, press F12 → Network → filter "my.home" or "oauth" '
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
    if (
        "code=" in low
        or "://" in unwrapped
        or (unwrapped.startswith("/") and "?" in unwrapped)
    ):
        return _parse_auth_redirect(unwrapped, expected_state)
    if any(c in u for c in "\n\t\r "):
        return None, "paste one line only (full URL or authorization code)"
    code = u.strip('"').strip("'")
    if len(code) < 12:
        return (
            None,
            "authorization code looks too short; paste the full URL or the code from DevTools",
        )
    return code, None


def _parse_auth_redirect(
    url_or_path: str, expected_state: str
) -> tuple[str | None, str | None]:
    """Return (authorization_code, error_message)."""
    url_or_path = _unwrap_my_home_assistant_paste(url_or_path)
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
        "a ``/_change/?redirect=…`` URL, **or** only the authorization code from "
        "DevTools → Network.\n"
        "Hints repeat every ~45s while waiting.",
        redirect_uri,
    )
    _LOG.info(
        "Authorize URL (open manually if the browser did not start):\n%s", auth_url
    )
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
    # pylint: disable=too-many-locals,too-many-statements
    outcome: dict[str, str] = {}
    stop = threading.Event()

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            """Silence stderr request logging from ``BaseHTTPRequestHandler``."""

        def do_GET(self) -> None:  # pylint: disable=invalid-name
            """Stdlib hook: handle OAuth redirect on ``/callback`` (PKCE loopback)."""
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
                b"<html><body>Gecko login OK. You can close this tab "
                b"and return to the terminal.</body></html>"
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
    _LOG.info(
        "Starting browser OAuth (PKCE). If login does not start, open:\n%s", auth_url
    )
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
    return _json_auth_response(text, context="Auth0 authorization_code response")


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
    return _json_auth_response(raw, context="Auth0 password grant response")


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
    # pylint: disable=too-many-locals
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
        os.environ.get("GECKO_USERNAME") or file_vars.get("GECKO_USERNAME") or ""
    ).strip()
    password = os.environ.get("GECKO_PASSWORD") or file_vars.get("GECKO_PASSWORD") or ""
    if user and password:
        tok = await _auth0_password_token(session, user, password)
        norm = _normalize_auth0_token(tok)
        at = norm.get("access_token")
        if not at:
            raise RuntimeError(f"password grant: no access_token: {list(tok.keys())}")
        return str(at), norm

    prefer_port = _parse_oauth_listen_port(args)
    verifier, challenge = _pkce_verifier_and_challenge()
    state = secrets.token_urlsafe(16)

    custom_uri = (getattr(args, "oauth_redirect_uri", None) or "").strip() or (
        os.environ.get("GECKO_OAUTH_REDIRECT_URI") or ""
    ).strip()
    mode = (
        os.environ.get("GECKO_OAUTH_REDIRECT")
        or getattr(args, "oauth_redirect", "")
        or ""
    ).strip()
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
