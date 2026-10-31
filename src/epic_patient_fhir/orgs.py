"""Health systems and client IDs.

Client IDs for a public (no secret) patient app are not secrets. They ship in
the repo so anyone can clone and run. Fill these in after registering the app
on fhir.epic.com (see README). Environment variables override them.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from importlib import resources

# Filled in after registration. Non-prod works only on Epic's sandbox;
# prod works only at real health systems after "Ready for Production".
NONPROD_CLIENT_ID = "772b56c6-c854-4929-ac96-4b5707e583d2"
PROD_CLIENT_ID = ""

# Epic requires an https redirect URI in production. This is the GitHub Pages
# copy of docs/callback.html, which forwards to http://localhost:8765/callback.
# Example: https://<github-user>.github.io/epic-patient-fhir/callback.html
PROD_REDIRECT_URI = "https://danhicks853.github.io/epic-patient-fhir/callback.html"
SANDBOX_REDIRECT_URI = "http://localhost:8765/callback"


@dataclass(frozen=True)
class Org:
    key: str
    name: str
    fhir_base: str
    sandbox: bool = False


SANDBOX = Org(
    "sandbox",
    "Epic on FHIR sandbox (fake test patients)",
    "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4",
    sandbox=True,
)


def _load_orgs() -> dict[str, Org]:
    """Sandbox plus the bundled snapshot of Epic's published endpoint directory.

    Epic asks apps not to query that directory at runtime, so it ships with the
    package and is refreshed per release by scripts/update_orgs.py.
    """
    orgs = {SANDBOX.key: SANDBOX}
    try:
        data = json.loads(resources.files(__package__).joinpath("orgs_data.json").read_text(encoding="utf-8"))
        for o in data.get("orgs", []):
            orgs[o["key"]] = Org(o["key"], o["name"], o["fhir_base"])
    except (FileNotFoundError, ModuleNotFoundError, ValueError):
        pass
    return orgs


ORGS = _load_orgs()


def find_orgs(query: str | None, limit: int = 25) -> list[Org]:
    """Health systems whose name or key contains every word of `query`."""
    if not query:
        return list(ORGS.values())[:limit]
    words = query.lower().split()
    hits = [o for o in ORGS.values() if all(w in f"{o.name} {o.key}".lower() for w in words)]
    hits.sort(key=lambda o: (not o.name.lower().startswith(words[0]), len(o.name)))
    return hits[:limit]


def resolve_org(key_or_name: str) -> Org | None:
    """Exact key, exact name (any case), or the single search match."""
    if key_or_name in ORGS:
        return ORGS[key_or_name]
    low = key_or_name.lower()
    for o in ORGS.values():
        if o.name.lower() == low:
            return o
    hits = find_orgs(key_or_name, limit=2)
    return hits[0] if len(hits) == 1 else None


def redirect_for(org: Org, override: str | None = None) -> str | None:
    """Sandbox uses localhost directly; production uses the https bounce page."""
    if override:
        return override
    if org.sandbox:
        return SANDBOX_REDIRECT_URI
    uri = os.environ.get("EPIC_REDIRECT_URI") or PROD_REDIRECT_URI
    if not uri:
        raise SystemExit(
            "No https redirect URI set for production. Publish docs/callback.html on "
            "GitHub Pages and set PROD_REDIRECT_URI in orgs.py (or EPIC_REDIRECT_URI)."
        )
    return uri


def client_id_for(org: Org, override: str | None = None) -> str:
    if override:
        return override
    if org.sandbox:
        cid = os.environ.get("EPIC_CLIENT_ID_NONPROD") or NONPROD_CLIENT_ID
        which = "EPIC_CLIENT_ID_NONPROD"
    else:
        cid = os.environ.get("EPIC_CLIENT_ID_PROD") or PROD_CLIENT_ID
        which = "EPIC_CLIENT_ID_PROD"
    if not cid:
        raise SystemExit(
            f"No client ID for '{org.key}'. Pass --client-id, set {which}, "
            "or fill it in at the top of orgs.py."
        )
    return cid
