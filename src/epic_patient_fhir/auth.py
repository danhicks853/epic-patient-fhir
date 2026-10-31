"""SMART on FHIR standalone patient launch: public client, PKCE (S256).

The user signs in with their own MyChart credentials in their own browser.
This code never sees the password. It receives an authorization code on a
localhost callback and trades it for a short-lived access token.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import secrets
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field

from .http import HttpError, get_json, post_form

DEFAULT_SCOPE = "launch/patient openid fhirUser patient/*.read"
CALLBACK_PATH = "/callback"


@dataclass
class SmartEndpoints:
    authorize: str
    token: str


@dataclass
class Session:
    fhir_base: str
    access_token: str
    patient_id: str
    expires_at: float
    scope: str = ""
    token_response: dict = field(default_factory=dict, repr=False)


def discover(fhir_base: str) -> SmartEndpoints:
    """Find OAuth endpoints: .well-known first, CapabilityStatement fallback."""
    base = fhir_base.rstrip("/")
    try:
        cfg = get_json(base + "/.well-known/smart-configuration", accept="application/json")
        return SmartEndpoints(cfg["authorization_endpoint"], cfg["token_endpoint"])
    except (HttpError, KeyError, ValueError):
        pass
    meta = get_json(base + "/metadata")
    for rest in meta.get("rest", []):
        for ext in rest.get("security", {}).get("extension", []):
            if ext.get("url", "").endswith("oauth-uris"):
                uris = {e.get("url"): e.get("valueUri") for e in ext.get("extension", [])}
                if uris.get("authorize") and uris.get("token"):
                    return SmartEndpoints(uris["authorize"], uris["token"])
    raise RuntimeError(f"Could not discover SMART endpoints for {base}")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def pkce_pair() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(64))  # 86 chars, within 43..128
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


class _CallbackServer(http.server.HTTPServer):
    result: dict | None = None


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            return
        q = urllib.parse.parse_qs(parsed.query)
        self.server.result = {k: v[0] for k, v in q.items()}
        ok = "error" not in self.server.result
        msg = (
            "Signed in. You can close this tab and go back to the terminal."
            if ok
            else "Sign-in failed. Check the terminal for details."
        )
        body = f"<!doctype html><meta charset=utf-8><title>epic-patient-fhir</title><p style='font:18px sans-serif'>{msg}</p>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence request logging
        pass


class LoginFlow:
    """One browser sign-in attempt.

    Binds the localhost callback, builds the authorize URL, then `wait()`
    blocks until the browser comes back and trades the code for a token.
    Split from `login()` so callers (the MCP server) can hand the URL to the
    user, return immediately, and finish the exchange in the background.

    Production Epic requires an https redirect. For that, `redirect_uri` is a
    static https "bounce" page (docs/callback.html on GitHub Pages) that
    forwards the code and state to http://localhost:<port>/callback. The code
    is useless without the PKCE verifier, which never leaves this process.
    """

    def __init__(self, fhir_base, client_id, *, port=8765, scope=DEFAULT_SCOPE, redirect_uri=None):
        self.fhir_base = fhir_base.rstrip("/")
        self.client_id = client_id
        self.endpoints = discover(self.fhir_base)
        self.redirect_uri = redirect_uri or f"http://localhost:{port}{CALLBACK_PATH}"
        self._verifier, challenge = pkce_pair()
        self._state = secrets.token_urlsafe(24)
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": self.redirect_uri,
            "scope": scope,
            "state": self._state,
            "aud": self.fhir_base,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        sep = "&" if "?" in self.endpoints.authorize else "?"
        self.auth_url = self.endpoints.authorize + sep + urllib.parse.urlencode(params)
        try:
            self._server = _CallbackServer(("127.0.0.1", port), _CallbackHandler)
        except OSError as e:
            raise RuntimeError(
                f"Port {port} is busy. Another sign-in may still be waiting; finish or cancel it and retry."
            ) from e
        self._server.timeout = 1
        self._closed = False

    def close(self):
        if not self._closed:
            self._closed = True
            self._server.server_close()

    def wait(self, timeout: float = 300) -> Session:
        try:
            deadline = time.monotonic() + timeout
            while self._server.result is None:
                if self._closed:
                    raise RuntimeError("Sign-in was cancelled.")
                if time.monotonic() > deadline:
                    raise TimeoutError("Timed out waiting for sign-in.")
                self._server.handle_request()
            result = self._server.result
        finally:
            self.close()
        return self._exchange(result)

    def _exchange(self, result: dict) -> Session:
        if "error" in result:
            raise RuntimeError(
                f"Authorization failed: {result.get('error')}: {result.get('error_description', '')}"
            )
        if result.get("state") != self._state:
            raise RuntimeError("State mismatch on callback. Aborting.")
        code = result.get("code")
        if not code:
            raise RuntimeError("No authorization code returned.")
        tok = post_form(
            self.endpoints.token,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "client_id": self.client_id,
                "code_verifier": self._verifier,
            },
        )
        if "access_token" not in tok:
            raise RuntimeError(f"Token response missing access_token: {list(tok)}")
        patient = tok.get("patient")
        if not patient:
            raise RuntimeError("Token response has no patient ID. Is the app registered for patients?")
        return Session(
            fhir_base=self.fhir_base,
            access_token=tok["access_token"],
            patient_id=patient,
            expires_at=time.time() + float(tok.get("expires_in", 3600)),
            scope=tok.get("scope", ""),
            token_response=tok,
        )


def login(
    fhir_base: str,
    client_id: str,
    *,
    port: int = 8765,
    scope: str = DEFAULT_SCOPE,
    launcher=webbrowser.open,
    timeout: int = 300,
    log=print,
    redirect_uri: str | None = None,
) -> Session:
    """Blocking sign-in for the CLI. `launcher` receives the authorize URL."""
    flow = LoginFlow(fhir_base, client_id, port=port, scope=scope, redirect_uri=redirect_uri)
    log("Opening MyChart sign-in in your browser. If it doesn't open, paste this URL:")
    log(flow.auth_url)
    launcher(flow.auth_url)
    return flow.wait(timeout)
