"""OAuth 2.0 sign-in for a QuickBooks Online sandbox company.

Run `ap-agent qbo-connect`. It asks for the sandbox app's client id and secret
with a hidden prompt (nothing is echoed or written to shell history), opens the
Intuit consent page in your browser, catches the redirect on localhost, and saves
the tokens to a file in your home folder, outside this repository.

Nobody else, including an AI assistant, needs to see the secret or the tokens.
"""

from __future__ import annotations

import base64
import contextlib
import getpass
import json
import os
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
SCOPE = "com.intuit.quickbooks.accounting"
REDIRECT_URI = "http://localhost:8765/callback"
TOKEN_FILE = Path(os.environ.get("AP_AGENT_QBO_TOKENS", Path.home() / ".ap-agent-mcp" / "qbo.json"))


def _basic(client_id: str, client_secret: str) -> str:
    return "Basic " + base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()


def save_tokens(data: dict[str, Any], path: Path = TOKEN_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def load_tokens(path: Path = TOKEN_FILE) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"No QuickBooks sandbox tokens at {path}. Run `ap-agent qbo-connect` first."
        )
    return json.loads(path.read_text(encoding="utf-8"))


class TokenStore:
    """Hands out access tokens and refreshes them when they expire or are rejected."""

    def __init__(self, path: Path = TOKEN_FILE, transport: httpx.BaseTransport | None = None):
        self.path = path
        self.data = load_tokens(path)
        self._transport = transport

    @property
    def realm_id(self) -> str:
        return str(self.data["realm_id"])

    def access_token(self, force_refresh: bool = False) -> str:
        if force_refresh or time.time() > float(self.data.get("expires_at", 0)) - 60:
            self.refresh()
        return str(self.data["access_token"])

    def refresh(self) -> None:
        with httpx.Client(transport=self._transport, timeout=30) as http:
            resp = http.post(
                TOKEN_URL,
                headers={
                    "Authorization": _basic(self.data["client_id"], self.data["client_secret"]),
                    "Accept": "application/json",
                },
                data={"grant_type": "refresh_token", "refresh_token": self.data["refresh_token"]},
            )
            resp.raise_for_status()
            tok = resp.json()
        self.data.update(
            access_token=tok["access_token"],
            refresh_token=tok.get("refresh_token", self.data["refresh_token"]),
            expires_at=time.time() + int(tok.get("expires_in", 3600)),
        )
        save_tokens(self.data, self.path)


def connect() -> None:  # pragma: no cover - interactive, needs a browser and a person
    print("QuickBooks Online SANDBOX sign-in. Use the Development keys of your Intuit app.")
    print(f"The app's redirect URI must include {REDIRECT_URI}")
    client_id = getpass.getpass("Sandbox client id (hidden): ").strip()
    client_secret = getpass.getpass("Sandbox client secret (hidden): ").strip()
    state = secrets.token_urlsafe(24)
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            qs = parse_qs(urlparse(self.path).query)
            result.update({k: v[0] for k, v in qs.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Signed in. You can close this tab and return to the terminal.")

        def log_message(self, *args: Any) -> None:
            return

    url = (
        AUTH_URL
        + "?"
        + urlencode(
            {
                "client_id": client_id,
                "response_type": "code",
                "scope": SCOPE,
                "redirect_uri": REDIRECT_URI,
                "state": state,
            }
        )
    )
    server = HTTPServer(("127.0.0.1", 8765), Handler)
    print("Opening the Intuit consent page in your browser. Pick your SANDBOX company.")
    webbrowser.open(url)
    while "code" not in result and "error" not in result:
        server.handle_request()
    server.server_close()
    if result.get("state") != state:
        raise SystemExit("State mismatch. Sign-in aborted.")
    if "error" in result:
        raise SystemExit(f"Intuit returned an error: {result['error']}")

    with httpx.Client(timeout=30) as http:
        resp = http.post(
            TOKEN_URL,
            headers={
                "Authorization": _basic(client_id, client_secret),
                "Accept": "application/json",
            },
            data={
                "grant_type": "authorization_code",
                "code": result["code"],
                "redirect_uri": REDIRECT_URI,
            },
        )
        resp.raise_for_status()
        tok = resp.json()
    save_tokens(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "realm_id": result["realmId"],
            "access_token": tok["access_token"],
            "refresh_token": tok["refresh_token"],
            "expires_at": time.time() + int(tok.get("expires_in", 3600)),
        }
    )
    print(f"Connected to sandbox company {result['realmId']}. Tokens saved to {TOKEN_FILE}.")
