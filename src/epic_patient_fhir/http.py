"""Minimal HTTP helpers. Standard library only."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__

USER_AGENT = f"epic-patient-fhir/{__version__}"
RETRY_STATUSES = {429, 500, 502, 503, 504}


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: str):
        self.status = status
        self.url = url
        self.body = body
        super().__init__(f"HTTP {status} for {url}: {body[:300]!r}")


def request(url, *, method="GET", headers=None, data=None, timeout=60, retries=2):
    """Return (body_bytes, content_type). Retries transient failures."""
    hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
    attempt = 0
    while True:
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read(), resp.headers.get("Content-Type", "") or ""
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code in RETRY_STATUSES and attempt < retries:
                attempt += 1
                time.sleep(2 ** attempt)
                continue
            raise HttpError(e.code, url, body) from None
        except urllib.error.URLError:
            if attempt < retries:
                attempt += 1
                time.sleep(2 ** attempt)
                continue
            raise


def get_json(url, *, token=None, accept="application/fhir+json"):
    headers = {"Accept": accept}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body, _ = request(url, headers=headers)
    return json.loads(body.decode("utf-8"))


def post_form(url, form):
    body, _ = request(
        url,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        data=urllib.parse.urlencode(form).encode(),
        retries=0,  # never replay an auth code
    )
    return json.loads(body.decode("utf-8"))
