"""Health systems and client IDs.

Client IDs for a public (no secret) patient app are not secrets. They ship in
the repo so anyone can clone and run. Fill these in after registering the app
on fhir.epic.com (see README). Environment variables override them.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# Filled in after registration. Non-prod works only on Epic's sandbox;
# prod works only at real health systems after "Ready for Production".
NONPROD_CLIENT_ID = ""
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


# v1 ships a hand-picked list. A later release generates the full list from
# Epic's published endpoint directory (a snapshot, refreshed per release;
# Epic asks apps not to query that directory at runtime).
ORGS = {
    o.key: o
    for o in [
        Org(
            "sandbox",
            "Epic on FHIR sandbox (fake test patients)",
            "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4",
            sandbox=True,
        ),
        Org(
            "adventhealth",
            "AdventHealth",
            "https://mobile.adventhealth.com/oauth2-PRD/api/FHIR/R4",
        ),
    ]
}


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
