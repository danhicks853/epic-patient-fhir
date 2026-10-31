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
    """Run the browser sign-in and return a Session.

    `launcher` receives the authorize URL. Default opens the system browser.
    The redirect URI must match the app registration exactly.

    Production Epic requires an https redirect. For that, `redirect_uri` is a
    static https "bounce" page (docs/callback.html on GitHub Pages) that
    forwards the code and state to http://localhost:<port>/callback. The code
    is useless without the PKCE verifier, which never leaves this process.
    """
    fhir_base = fhir_base.rstrip("/")
    ep = discover(fhir_base)
    redirect_uri = redirect_uri or f"http://localhost:{port}{CALLBACK_PATH}"
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "aud": fhir_base,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    auth_url = ep.authorize + ("&" if "?" in ep.authorize else "?") + urllib.parse.urlencode(params)

    server = _CallbackServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = 1
    try:
        log("Opening MyChart sign-in in your browser. If it doesn't open, paste this URL:")
        log(auth_url)
        launcher(auth_url)
        deadline = time.monotonic() + timeout
        while server.result is None:
            if time.monotonic() > deadline:
                raise TimeoutError("Timed out waiting for sign-in.")
            server.handle_request()
        result = server.result
    finally:
        server.server_close()

    if "error" in result:
        raise RuntimeError(
            f"Authorization failed: {result.get('error')}: {result.get('error_description', '')}"
        )
    if result.get("state") != state:
        raise RuntimeError("State mismatch on callback. Aborting.")
    code = result.get("code")
    if not code:
        raise RuntimeError("No authorization code returned.")

    tok = post_form(
        ep.token,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    if "access_token" not in tok:
        raise RuntimeError(f"Token response missing access_token: {list(tok)}")
    patient = tok.get("patient")
    if not patient:
        raise RuntimeError("Token response has no patient ID. Is the app registered for patients?")
    return Session(
        fhir_base=fhir_base,
        access_token=tok["access_token"],
        patient_id=patient,
        expires_at=time.time() + float(tok.get("expires_in", 3600)),
        scope=tok.get("scope", ""),
        token_response=tok,
    )
