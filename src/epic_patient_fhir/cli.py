"""Command line: find health systems, export, set up Claude, self-check."""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

from .auth import discover, pkce_pair
from .client import FhirClient
from .export import export_all
from .orgs import Org, client_id_for, find_orgs, redirect_for, resolve_org

SERVER_NAME = "epic-patient-fhir"


def _org_from_args(a) -> Org:
    if getattr(a, "base", None):
        return Org("custom", "Custom endpoint", a.base.rstrip("/"))
    org = resolve_org(a.org)
    if not org:
        hits = find_orgs(a.org, limit=10)
        msg = f"No single health system matches '{a.org}'."
        if hits:
            msg += " Did you mean:\n" + "\n".join(f"  {o.key:40} {o.name}" for o in hits)
        raise SystemExit(msg)
    return org


# ---------------------------------------------------------------- orgs

def cmd_orgs(a) -> int:
    hits = find_orgs(a.search, limit=a.limit)
    if not hits:
        print(f"No health systems match '{a.search}'.")
        return 1
    for o in hits:
        print(f"{o.key:40} {o.name}")
    return 0


# ---------------------------------------------------------------- export

def cmd_export(a) -> int:
    from .auth import login

    org = _org_from_args(a)
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


# ---------------------------------------------------------------- install-claude

def claude_config_path() -> Path:
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Claude" / "claude_desktop_config.json"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def server_entry() -> dict:
    # Absolute interpreter path: works even when Scripts/ isn't on PATH (common on Windows).
    return {"command": sys.executable, "args": ["-m", "epic_patient_fhir.mcp_server"]}


def install_claude(config_path: Path) -> tuple[Path, Path | None]:
    """Add this server to the Claude desktop config. Returns (config, backup)."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    data: dict = {}
    if config_path.exists():
        raw = config_path.read_text(encoding="utf-8")
        if raw.strip():
            try:
                data = json.loads(raw)
            except ValueError:
                raise SystemExit(
                    f"{config_path} isn't valid JSON, so I won't touch it. Fix or remove it, then retry."
                )
        backup = config_path.with_name(f"{config_path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(config_path, backup)
    if not isinstance(data, dict):
        raise SystemExit(f"{config_path} doesn't contain a JSON object. Not changing it.")
    data.setdefault("mcpServers", {})[SERVER_NAME] = server_entry()
    config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return config_path, backup


def cmd_install_claude(a) -> int:
    path = Path(a.config) if a.config else claude_config_path()
    config, backup = install_claude(path)
    print(f"Added '{SERVER_NAME}' to {config}")
    if backup:
        print(f"Your previous config was backed up to {backup}")
    print("\nNext: fully quit the Claude desktop app (from the system tray / menu bar) and reopen it.")
    print("Then ask Claude: \"Connect to the Epic sandbox and show my labs.\"")
    return 0


# ---------------------------------------------------------------- doctor

def _probe_client(org: Org, client_id: str, redirect_uri: str) -> str:
    """Ask the authorize endpoint about this client without signing in.

    A recognized client is redirected toward the MyChart login; an unknown
    one gets Epic's "OAuth2 Error" page.
    """
    ep = discover(org.fhir_base)
    _, challenge = pkce_pair()
    q = {
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
        "scope": "launch/patient openid fhirUser", "state": "doctor", "aud": org.fhir_base,
        "code_challenge": challenge, "code_challenge_method": "S256",
    }

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    url = ep.authorize + ("&" if "?" in ep.authorize else "?") + urllib.parse.urlencode(q)
    try:
        resp = urllib.request.build_opener(_NoRedirect).open(url, timeout=30)
        status, body, location = resp.status, resp.read().decode("utf-8", "replace"), resp.headers.get("Location")
    except urllib.error.HTTPError as e:
        status, body, location = e.code, e.read().decode("utf-8", "replace"), e.headers.get("Location")
    if status in (301, 302, 303, 307) and location and "error" not in location.lower():
        return "ok"
    if "OAuth2 Error" in body or "Something went wrong" in body or (location and "error" in location.lower()):
        return "rejected"
    return f"unclear (HTTP {status})"


def cmd_doctor(a) -> int:
    ok = True

    def line(passed, label, detail=""):
        nonlocal ok
        ok = ok and passed is not False
        mark = {True: "OK  ", False: "FAIL", None: "WARN"}[passed]
        print(f"[{mark}] {label}" + (f": {detail}" if detail else ""))

    line(sys.version_info >= (3, 10), "Python 3.10+", platform.python_version())
    try:
        import mcp  # noqa: F401
        line(True, "MCP SDK installed")
    except ImportError:
        line(False, "MCP SDK installed", "run: pip install -e .")

    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 8765))
        line(True, "Sign-in port 8765 free")
    except OSError:
        line(None, "Sign-in port 8765 free", "in use (fine if Claude is mid sign-in; otherwise close what's using it)")
    finally:
        s.close()

    cfg = claude_config_path()
    try:
        has = SERVER_NAME in json.loads(cfg.read_text(encoding="utf-8")).get("mcpServers", {})
        line(True if has else None, "Claude desktop config", str(cfg) if has else "not added yet; run: epic-patient-fhir install-claude")
    except (OSError, ValueError):
        line(None, "Claude desktop config", "not found; run: epic-patient-fhir install-claude")

    org = _org_from_args(a)
    try:
        cid = client_id_for(org)
    except SystemExit as e:
        line(False, f"Client ID for {org.name}", str(e))
        return 1
    try:
        discover(org.fhir_base)
        line(True, f"{org.name} reachable")
    except Exception as e:  # noqa: BLE001
        line(False, f"{org.name} reachable", str(e))
        return 1
    verdict = _probe_client(org, cid, redirect_for(org))
    if verdict == "ok":
        line(True, f"{org.name} recognizes this app")
    elif verdict == "rejected":
        hint = (
            "registration hasn't reached the sandbox yet (can take up to a day)"
            if org.sandbox
            else "this health system hasn't received the app yet, or the app isn't production-ready"
        )
        line(False, f"{org.name} recognizes this app", hint)
    else:
        line(None, f"{org.name} recognizes this app", verdict)
    print("\nAll good." if ok else "\nSee the FAIL lines above.")
    return 0 if ok else 1


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="epic-patient-fhir",
        description="Read your own medical record from an Epic (MyChart) health system.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser("orgs", help="Search supported health systems")
    o.add_argument("search", nargs="?", help="Words from the name, e.g. advent")
    o.add_argument("--limit", type=int, default=50)

    e = sub.add_parser("export", help="Sign in with MyChart and export your record to a folder")
    e.add_argument("--org", default="sandbox", help="Health system key or name (see 'orgs'). Default: sandbox")
    e.add_argument("--base", help="Custom FHIR R4 base URL (overrides --org)")
    e.add_argument("--client-id", help="Override the client ID")
    e.add_argument("--port", type=int, default=8765, help="Local callback port. Default 8765")
    e.add_argument("--out", help="Output folder. Default: exports/<org>-<timestamp>")
    e.add_argument("--no-browser", action="store_true", help="Print the sign-in URL instead of opening a browser")
    e.add_argument("--redirect-uri", help="Override the registered redirect URI")

    i = sub.add_parser("install-claude", help="Add this connector to the Claude desktop app")
    i.add_argument("--config", help="Path to claude_desktop_config.json (auto-detected by default)")

    d = sub.add_parser("doctor", help="Check setup and whether a health system recognizes the app")
    d.add_argument("--org", default="sandbox", help="Health system to check. Default: sandbox")

    a = p.parse_args(argv)
    return {
        "orgs": cmd_orgs,
        "export": cmd_export,
        "install-claude": cmd_install_claude,
        "doctor": cmd_doctor,
    }[a.cmd](a)
