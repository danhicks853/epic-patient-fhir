"""Command line: list orgs, sign in, export."""
from __future__ import annotations

import argparse
import webbrowser
from datetime import datetime
from pathlib import Path

from .auth import login
from .client import FhirClient
from .export import export_all
from .orgs import ORGS, Org, client_id_for, redirect_for


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="epic-patient-fhir",
        description="Download your own medical record from an Epic (MyChart) health system.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("orgs", help="List supported health systems")

    e = sub.add_parser("export", help="Sign in with MyChart and export your record")
    e.add_argument("--org", default="sandbox", help="Health system key (see 'orgs'). Default: sandbox")
    e.add_argument("--base", help="Custom FHIR R4 base URL (overrides --org)")
    e.add_argument("--client-id", help="Override the client ID")
    e.add_argument("--port", type=int, default=8765, help="Local callback port. Must match the registered redirect URI. Default 8765")
    e.add_argument("--out", help="Output folder. Default: exports/<org>-<timestamp>")
    e.add_argument("--no-browser", action="store_true", help="Print the sign-in URL instead of opening a browser")
    e.add_argument("--redirect-uri", help="Override the registered redirect URI")

    a = p.parse_args(argv)

    if a.cmd == "orgs":
        for o in ORGS.values():
            print(f"{o.key:16} {o.name}\n{'':16} {o.fhir_base}")
        return 0

    if a.base:
        org = Org("custom", "Custom endpoint", a.base.rstrip("/"))
    else:
        if a.org not in ORGS:
            raise SystemExit(f"Unknown org '{a.org}'. Run: epic-patient-fhir orgs")
        org = ORGS[a.org]
    cid = client_id_for(org, a.client_id)

    out = Path(a.out or Path("exports") / f"{org.key}-{datetime.now():%Y%m%d-%H%M%S}")
    print(f"Health system: {org.name}")
    session = login(
        org.fhir_base,
        cid,
        port=a.port,
        redirect_uri=redirect_for(org, a.redirect_uri),
        launcher=(lambda url: None) if a.no_browser else webbrowser.open,
    )
    print(f"Signed in. Pulling records to {out.resolve()}")
    summary = export_all(FhirClient(session), out)

    print("\nDone.")
    print(f"  Notes saved:   {summary['files'].get('notes', 0)}")
    print(f"  Reports saved: {summary['files'].get('reports', 0)}")
    if summary["errors"]:
        print("  Some sections failed (details in summary.json):")
        for k, v in summary["errors"].items():
            print(f"    {k}: {v}")
    print(f"  Folder: {out.resolve()}")
    return 0
