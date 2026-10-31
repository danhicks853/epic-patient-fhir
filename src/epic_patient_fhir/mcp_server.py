"""MCP server: lets Claude (or any MCP client) read your Epic record.

Runs locally over stdio. Sign-in happens in your own browser; the access
token lives in this process's memory only and is gone when it exits.

Note on privacy: what a tool returns is sent to the AI model you are using,
under that provider's terms. Nothing is sent anywhere else.
"""
from __future__ import annotations

import os
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

try:  # MCP Python SDK 2.x
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from .auth import LoginFlow, Session
from .client import FhirClient
from .export import SEARCHES, _pick_attachment, export_all
from .http import HttpError
from .normalize import (
    cc_text,
    condition_row,
    html_to_text,
    medication_row,
    obs_date,
    observation_row,
)
from .orgs import Org, client_id_for, find_orgs, redirect_for, resolve_org

INSTRUCTIONS = """\
Reads the user's own medical record from their health system (Epic/MyChart).
Start with `connection_status`. If not connected, find the user's health
system with `list_health_systems(search=...)` and call `connect` with its key
(ask the user which one if unsure); the user signs in in their
browser, then call `connection_status` until it says connected.
Data is the user's own. Present it plainly, note reference ranges and flags,
and do not diagnose. Suggest they confirm anything important with their care team.
"""

mcp = _Server("epic-patient-fhir", instructions=INSTRUCTIONS)

SEARCH_BY_LABEL = {label: (rtype, params) for label, rtype, params in SEARCHES}
MAX_NOTE_CHARS = 40_000


class _State:
    def __init__(self):
        self.lock = threading.Lock()
        self.session: Session | None = None
        self.client: FhirClient | None = None
        self.org: Org | None = None
        self.flow: LoginFlow | None = None
        self.flow_error: str | None = None
        self.cache: dict[str, list[dict]] = {}
        self.warnings: dict[str, list] = {}

    def reset(self):
        if self.flow:
            self.flow.close()
        self.__init__()


STATE = _State()

# Overridable for tests: what to do with the sign-in URL.
LAUNCHER = webbrowser.open
# The production bounce page forwards to this port; only change it for testing.
CALLBACK_PORT = int(os.environ.get("EPIC_CALLBACK_PORT", "8765"))


# ---------------------------------------------------------------- helpers

class NotConnected(Exception):
    pass


def _client() -> FhirClient:
    s = STATE.session
    if not s or not STATE.client:
        raise NotConnected("Not connected. Call `connect` first.")
    if time.time() > s.expires_at - 30:
        STATE.session = None
        STATE.client = None
        STATE.cache.clear()
        raise NotConnected("The sign-in expired (they last about an hour). Call `connect` again.")
    return STATE.client


def _fetch(label: str) -> list[dict]:
    """Cached search for one SEARCHES label."""
    if label in STATE.cache:
        return STATE.cache[label]
    client = _client()
    rtype, params = SEARCH_BY_LABEL[label]
    try:
        items, issues = client.search(rtype, params)
    except HttpError as e:
        if e.status == 401:
            STATE.session = None
            STATE.client = None
            raise NotConnected("The health system rejected the sign-in (expired or revoked). Call `connect` again.")
        if e.status == 403:
            raise RuntimeError(f"{label}: this health system doesn't allow the app to read this data (HTTP 403).")
        raise
    STATE.cache[label] = items
    if issues:
        STATE.warnings[label] = [i.get("diagnostics") or cc_text(i.get("details")) for i in issues]
    return items


def _guard(fn):
    """Turn expected failures into readable tool output instead of stack traces."""
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except NotConnected as e:
            return {"error": str(e)}
        except HttpError as e:
            return {"error": f"Health system returned HTTP {e.status}.", "detail": e.body[:300]}
        except RuntimeError as e:
            return {"error": str(e)}

    return wrapper


def _since_ok(date: str, since: str | None) -> bool:
    return not since or (date or "")[:10] >= since[:10]


def _contains(text: str, query: str | None) -> bool:
    return not query or query.lower() in (text or "").lower()


def _period_start(r: dict) -> str:
    p = r.get("period") or {}
    return p.get("start") or r.get("performedDateTime") or (r.get("performedPeriod") or {}).get("start") or ""


# ---------------------------------------------------------------- connection

@mcp.tool()
def list_health_systems(search: str | None = None, limit: int = 25) -> dict:
    """Find a health system to connect to. Searches about 480 Epic organizations by name.

    search: words from the health system's name, e.g. "advent", "mayo", "kaiser".
    Pass the result's `key` to `connect`.
    """
    hits = find_orgs(search, limit=limit)
    return {
        "count": len(hits),
        "health_systems": [{"key": o.key, "name": o.name, "test_only": o.sandbox} for o in hits],
        "note": "Use key 'sandbox' to try Epic's fake test patients.",
    }


@mcp.tool()
def connect(health_system: str, custom_fhir_base: str | None = None) -> dict:
    """Start MyChart sign-in for a health system.

    health_system: a `key` from `list_health_systems` (or the exact name).
    Opens the user's browser to their health system's sign-in page and returns
    right away. The user signs in and approves; then call `connection_status`.
    `custom_fhir_base` targets any Epic FHIR R4 endpoint not in the list.
    """
    with STATE.lock:
        if STATE.flow:
            STATE.flow.close()
        STATE.reset()
        if custom_fhir_base:
            org = Org("custom", "Custom endpoint", custom_fhir_base.rstrip("/"))
        else:
            org = resolve_org(health_system)
            if not org:
                return {"error": f"No single match for '{health_system}'. Call list_health_systems with a search."}
        try:
            cid = client_id_for(org)
            redirect = redirect_for(org)
        except SystemExit as e:
            return {"error": str(e)}
        try:
            flow = LoginFlow(org.fhir_base, cid, port=CALLBACK_PORT, redirect_uri=redirect)
        except Exception as e:  # discovery or port failure
            return {"error": f"Could not start sign-in: {e}"}
        STATE.org = org
        STATE.flow = flow

    def finish():
        try:
            session = flow.wait(timeout=600)
        except Exception as e:
            with STATE.lock:
                if STATE.flow is flow:
                    STATE.flow_error = str(e)
                    STATE.flow = None
            return
        with STATE.lock:
            if STATE.flow is flow:
                STATE.session = session
                STATE.client = FhirClient(session)
                STATE.flow = None

    threading.Thread(target=finish, daemon=True).start()
    try:
        LAUNCHER(flow.auth_url)
        opened = True
    except Exception:
        opened = False
    return {
        "status": "waiting_for_sign_in",
        "health_system": org.name,
        "message": (
            "A MyChart sign-in page should be open in your browser. Sign in and approve access, "
            "then I'll check the connection."
            if opened
            else "Open this link to sign in, then I'll check the connection."
        ),
        "sign_in_url": flow.auth_url,
    }


@mcp.tool()
def connection_status() -> dict:
    """Report whether a health system is connected, still waiting, or failed."""
    with STATE.lock:
        if STATE.session:
            left = int((STATE.session.expires_at - time.time()) / 60)
            if left <= 0:
                return {"status": "expired", "message": "Call `connect` again."}
            return {
                "status": "connected",
                "health_system": STATE.org.name if STATE.org else STATE.session.fhir_base,
                "minutes_left": left,
            }
        if STATE.flow:
            return {"status": "waiting_for_sign_in", "sign_in_url": STATE.flow.auth_url}
        if STATE.flow_error:
            return {"status": "failed", "error": STATE.flow_error}
        return {"status": "not_connected"}


@mcp.tool()
def disconnect() -> dict:
    """Forget the current sign-in and all cached data in memory."""
    with STATE.lock:
        STATE.reset()
    return {"status": "disconnected"}


# ---------------------------------------------------------------- record

@mcp.tool()
@_guard
def get_patient() -> dict:
    """Basic demographics from the record (name, birth date, recorded sex)."""
    client = _client()
    p = client.read("Patient", client.s.patient_id)
    names = [n.get("text") or " ".join(n.get("given", []) + [n.get("family", "")]).strip() for n in p.get("name", [])]
    return {
        "names": [n for n in names if n],
        "birth_date": p.get("birthDate", ""),
        "recorded_sex": p.get("gender", ""),
    }


@mcp.tool()
@_guard
def get_labs(name: str | None = None, since: str | None = None, limit: int = 100) -> dict:
    """Lab results, newest first.

    name: optional substring filter, e.g. "WBC", "a1c", "glucose".
    since: optional ISO date, e.g. "2026-10-01".
    """
    rows = [observation_row(o, "laboratory") for o in _fetch("Observation-laboratory")]
    rows = [r for r in rows if _contains(r["name"], name) and _since_ok(r["date"], since)]
    rows.sort(key=lambda r: r["date"], reverse=True)
    return {
        "count": len(rows),
        "results": [{k: r[k] for k in ("date", "name", "value", "unit", "reference_range", "flag")} for r in rows[:limit]],
        "warnings": STATE.warnings.get("Observation-laboratory", []),
    }


@mcp.tool()
@_guard
def get_lab_trend(name: str) -> dict:
    """Every recorded value for a lab test over time, oldest first, grouped by exact test name."""
    rows = [observation_row(o, "laboratory") for o in _fetch("Observation-laboratory")]
    groups: dict[str, list] = {}
    for r in rows:
        if _contains(r["name"], name):
            groups.setdefault(r["name"], []).append(r)
    return {
        test: [
            {k: r[k] for k in ("date", "value", "unit", "reference_range", "flag")}
            for r in sorted(rs, key=lambda r: r["date"])
        ]
        for test, rs in groups.items()
    } or {"message": f"No lab results matching '{name}'."}


@mcp.tool()
@_guard
def get_vitals(name: str | None = None, since: str | None = None, limit: int = 100) -> dict:
    """Vital signs (blood pressure, pulse, temperature, SpO2, weight...), newest first."""
    rows = [observation_row(o, "vital-signs") for o in _fetch("Observation-vital-signs")]
    rows = [r for r in rows if _contains(r["name"], name) and _since_ok(r["date"], since)]
    rows.sort(key=lambda r: r["date"], reverse=True)
    return {"count": len(rows), "results": [{k: r[k] for k in ("date", "name", "value", "unit")} for r in rows[:limit]]}


@mcp.tool()
@_guard
def get_conditions() -> dict:
    """Problem list, encounter diagnoses, and health concerns."""
    out = {}
    for label, key in (
        ("Condition-problem-list", "problem_list"),
        ("Condition-encounter-diagnosis", "encounter_diagnoses"),
        ("Condition-health-concern", "health_concerns"),
    ):
        try:
            out[key] = [condition_row(c) for c in _fetch(label)]
        except RuntimeError as e:
            out[key] = {"error": str(e)}
    return out


@mcp.tool()
@_guard
def get_medications(status: str | None = None) -> dict:
    """Medication orders and prescriptions. status: e.g. "active", "stopped", "completed"."""
    rows = [medication_row(m) for m in _fetch("MedicationRequest")]
    if status:
        rows = [r for r in rows if r["status"].lower() == status.lower()]
    rows.sort(key=lambda r: r["authored"], reverse=True)
    return {"count": len(rows), "medications": rows}


@mcp.tool()
@_guard
def get_allergies() -> dict:
    """Allergies and intolerances."""
    out = []
    for a in _fetch("AllergyIntolerance"):
        out.append({
            "substance": cc_text(a.get("code")),
            "status": cc_text(a.get("clinicalStatus")),
            "criticality": a.get("criticality", ""),
            "reactions": "; ".join(
                cc_text(m) for r in a.get("reaction", []) for m in r.get("manifestation", [])
            ),
            "recorded": a.get("recordedDate", ""),
        })
    return {"count": len(out), "allergies": out}


@mcp.tool()
@_guard
def get_encounters(since: str | None = None, limit: int = 50) -> dict:
    """Visits and hospital stays, newest first."""
    out = []
    for e in _fetch("Encounter"):
        start = _period_start(e)
        if not _since_ok(start, since):
            continue
        out.append({
            "start": start,
            "end": (e.get("period") or {}).get("end", ""),
            "class": (e.get("class") or {}).get("display") or (e.get("class") or {}).get("code", ""),
            "type": cc_text(e.get("type")),
            "reason": cc_text(e.get("reasonCode")),
            "location": "; ".join((l.get("location") or {}).get("display", "") for l in e.get("location", [])),
            "status": e.get("status", ""),
        })
    out.sort(key=lambda r: r["start"], reverse=True)
    return {"count": len(out), "encounters": out[:limit]}


@mcp.tool()
@_guard
def get_procedures(since: str | None = None) -> dict:
    """Procedures and surgeries, newest first."""
    out = []
    for p in _fetch("Procedure"):
        d = _period_start(p)
        if _since_ok(d, since):
            out.append({"date": d, "name": cc_text(p.get("code")), "status": p.get("status", "")})
    out.sort(key=lambda r: r["date"], reverse=True)
    return {"count": len(out), "procedures": out}


@mcp.tool()
@_guard
def get_immunizations() -> dict:
    """Vaccination history, newest first."""
    out = [
        {"date": i.get("occurrenceDateTime", ""), "vaccine": cc_text(i.get("vaccineCode")), "status": i.get("status", "")}
        for i in _fetch("Immunization")
    ]
    out.sort(key=lambda r: r["date"], reverse=True)
    return {"count": len(out), "immunizations": out}


def _doc_date(d: dict) -> str:
    return d.get("date") or ((d.get("context") or {}).get("period") or {}).get("start") or ""


@mcp.tool()
@_guard
def list_notes(query: str | None = None, since: str | None = None, limit: int = 50) -> dict:
    """Clinical notes (progress notes, op notes, discharge summaries...), newest first.

    query: optional substring matched against the note type and author.
    Use `get_note` with an `id` to read one.
    """
    out = []
    for d in _fetch("DocumentReference-clinical-notes"):
        date = _doc_date(d)
        title = cc_text(d.get("type")) or d.get("description", "")
        author = "; ".join(a.get("display", "") for a in d.get("author", []))
        if _since_ok(date, since) and (_contains(title, query) or _contains(author, query)):
            out.append({"id": d.get("id"), "date": date, "type": title, "author": author, "status": d.get("docStatus", "")})
    out.sort(key=lambda r: r["date"], reverse=True)
    return {"count": len(out), "notes": out[:limit]}


def _attachment_text(att: dict) -> str:
    got = _client().fetch_attachment(att)
    if not got:
        return ""
    data, ctype = got
    ctype = ctype.split(";")[0]
    if ctype == "text/html":
        text = html_to_text(data.decode("utf-8", "replace"))
    elif ctype.startswith("text/") and ctype not in ("text/rtf",):
        text = data.decode("utf-8", "replace")
    else:
        return f"(This document is {ctype}, which can't be shown as text. Use `export_record` to save it.)"
    if len(text) > MAX_NOTE_CHARS:
        text = text[:MAX_NOTE_CHARS] + f"\n\n[Truncated at {MAX_NOTE_CHARS} characters.]"
    return text


@mcp.tool()
@_guard
def get_note(note_id: str) -> dict:
    """Full text of one clinical note, by the `id` from `list_notes`."""
    doc = next((d for d in _fetch("DocumentReference-clinical-notes") if d.get("id") == note_id), None)
    if not doc:
        return {"error": f"No note with id '{note_id}'. Call list_notes."}
    att = _pick_attachment([c.get("attachment", {}) for c in doc.get("content", [])])
    if not att:
        return {"error": "This note has no attached content."}
    return {
        "date": _doc_date(doc),
        "type": cc_text(doc.get("type")),
        "author": "; ".join(a.get("display", "") for a in doc.get("author", [])),
        "text": _attachment_text(att),
    }


@mcp.tool()
@_guard
def list_reports(query: str | None = None, since: str | None = None, limit: int = 50) -> dict:
    """Diagnostic reports (imaging, pathology, lab panels), newest first. Read one with `get_report`."""
    out = []
    for r in _fetch("DiagnosticReport"):
        date = r.get("effectiveDateTime") or (r.get("effectivePeriod") or {}).get("start") or r.get("issued", "")
        name = cc_text(r.get("code"))
        if _since_ok(date, since) and _contains(name + " " + cc_text(r.get("category")), query):
            out.append({
                "id": r.get("id"),
                "date": date,
                "name": name,
                "category": cc_text(r.get("category")),
                "status": r.get("status", ""),
                "has_text": bool(r.get("presentedForm")),
                "result_count": len(r.get("result", [])),
            })
    out.sort(key=lambda r: r["date"], reverse=True)
    return {"count": len(out), "reports": out[:limit]}


@mcp.tool()
@_guard
def get_report(report_id: str) -> dict:
    """One diagnostic report: its narrative text (e.g. a radiology impression) and linked results."""
    rep = next((r for r in _fetch("DiagnosticReport") if r.get("id") == report_id), None)
    if not rep:
        return {"error": f"No report with id '{report_id}'. Call list_reports."}
    out = {
        "name": cc_text(rep.get("code")),
        "date": rep.get("effectiveDateTime") or rep.get("issued", ""),
        "conclusion": rep.get("conclusion", ""),
    }
    att = _pick_attachment(rep.get("presentedForm", []))
    if att:
        out["text"] = _attachment_text(att)
    ids = {(ref.get("reference") or "").split("/")[-1] for ref in rep.get("result", [])}
    if ids:
        labs = {o.get("id"): o for o in _fetch("Observation-laboratory")}
        out["results"] = [
            {k: row[k] for k in ("name", "value", "unit", "reference_range", "flag")}
            for row in (observation_row(labs[i], "laboratory") for i in ids if i in labs)
        ]
    return out


@mcp.tool()
@_guard
def export_record(folder: str | None = None) -> dict:
    """Save the full record to a folder on this computer (CSV, notes, raw FHIR JSON).

    Default folder: ~/epic-patient-fhir-exports/<health-system>-<timestamp>.
    """
    client = _client()
    key = STATE.org.key if STATE.org else "export"
    out = Path(folder).expanduser() if folder else (
        Path.home() / "epic-patient-fhir-exports" / f"{key}-{datetime.now():%Y%m%d-%H%M%S}"
    )
    summary = export_all(client, out, log=lambda *a: None)
    return {"folder": str(out.resolve()), "counts": summary["counts"], "files": summary["files"], "errors": summary["errors"]}


def main():
    mcp.run()


if __name__ == "__main__":
    main()
