"""Pull everything the app is allowed to read and write it to disk."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from .client import FhirClient
from .http import HttpError
from .normalize import (
    COND_FIELDS,
    MED_FIELDS,
    OBS_FIELDS,
    cc_text,
    condition_row,
    html_to_text,
    medication_row,
    observation_row,
    slug,
)

# (label, resource type, extra search params). Epic requires a category on
# some searches (Observation), so those are split by category.
SEARCHES = [
    ("AllergyIntolerance", "AllergyIntolerance", {}),
    ("Condition-problem-list", "Condition", {"category": "problem-list-item"}),
    ("Condition-encounter-diagnosis", "Condition", {"category": "encounter-diagnosis"}),
    ("Condition-health-concern", "Condition", {"category": "health-concern"}),
    ("DiagnosticReport", "DiagnosticReport", {}),
    ("DocumentReference-clinical-notes", "DocumentReference", {"category": "clinical-note"}),
    ("Encounter", "Encounter", {}),
    ("Immunization", "Immunization", {}),
    ("MedicationRequest", "MedicationRequest", {}),
    ("Observation-laboratory", "Observation", {"category": "laboratory"}),
    ("Observation-vital-signs", "Observation", {"category": "vital-signs"}),
    ("Observation-social-history", "Observation", {"category": "social-history"}),
    ("Procedure", "Procedure", {}),
]

PREFERRED_TYPES = ["text/html", "text/plain", "application/pdf", "text/rtf", "application/rtf"]
EXT = {
    "text/html": "html",
    "text/plain": "txt",
    "application/pdf": "pdf",
    "text/rtf": "rtf",
    "application/rtf": "rtf",
    "application/xml": "xml",
    "text/xml": "xml",
}


def _pick_attachment(attachments: list[dict]) -> dict | None:
    if not attachments:
        return None
    by_type = {(a.get("contentType") or "").split(";")[0]: a for a in attachments}
    for t in PREFERRED_TYPES:
        if t in by_type:
            return by_type[t]
    return attachments[0]


def _doc_date(doc: dict) -> str:
    d = doc.get("date") or ((doc.get("context") or {}).get("period") or {}).get("start")
    d = d or doc.get("effectiveDateTime") or doc.get("issued") or ""
    return d[:10] or "undated"


def _save_attachment(client, att, folder: Path, stem: str) -> str | None:
    got = client.fetch_attachment(att)
    if not got:
        return None
    data, ctype = got
    ext = EXT.get(ctype.split(";")[0], "bin")
    path = folder / f"{stem}.{ext}"
    path.write_bytes(data)
    if ext in ("html", "txt"):
        text = data.decode("utf-8", "replace")
        if ext == "html":
            (folder / f"{stem}.txt").write_text(html_to_text(text), encoding="utf-8")
    return path.name


def _write_csv(path: Path, fields: list[str], rows: list[dict]):
    rows = sorted(rows, key=lambda r: r.get(fields[0], ""), reverse=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def export_all(client: FhirClient, out_dir: Path, log=print) -> dict:
    out_dir = Path(out_dir)
    raw = out_dir / "raw"
    notes = out_dir / "notes"
    reports = out_dir / "reports"
    for d in (raw, notes, reports):
        d.mkdir(parents=True, exist_ok=True)

    summary = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "fhir_base": client.base,
        "counts": {},
        "errors": {},
        "warnings": {},
        "files": {},
    }

    try:
        patient = client.read("Patient", client.s.patient_id)
        (raw / "Patient.json").write_text(json.dumps(patient, indent=2), encoding="utf-8")
        summary["counts"]["Patient"] = 1
    except HttpError as e:
        summary["errors"]["Patient"] = f"HTTP {e.status}"

    results: dict[str, list[dict]] = {}
    for label, rtype, params in SEARCHES:
        try:
            items, issues = client.search(rtype, params)
        except HttpError as e:
            hint = " (not enabled in the app registration?)" if e.status in (401, 403) else ""
            summary["errors"][label] = f"HTTP {e.status}{hint}"
            log(f"  {label}: error HTTP {e.status}{hint}")
            continue
        results[label] = items
        summary["counts"][label] = len(items)
        if issues:
            summary["warnings"][label] = [
                i.get("diagnostics") or cc_text(i.get("details")) for i in issues
            ]
        (raw / f"{label}.json").write_text(json.dumps(items, indent=2), encoding="utf-8")
        log(f"  {label}: {len(items)}")

    # Clinical notes
    note_files = []
    for doc in results.get("DocumentReference-clinical-notes", []):
        att = _pick_attachment([c.get("attachment", {}) for c in doc.get("content", [])])
        if not att:
            continue
        title =cc_text(doc.get("type")) or doc.get("description") or "note"
        stem = f"{_doc_date(doc)}_{slug(title)}_{slug(doc.get('id', ''), 16)}"
        try:
            name = _save_attachment(client, att, notes, stem)
            if name:
                note_files.append(name)
        except HttpError as e:
            summary["errors"].setdefault("notes", []).append(f"{doc.get('id')}: HTTP {e.status}")
    summary["files"]["notes"] = len(note_files)

    # Report text (imaging reads, pathology) attached to DiagnosticReports
    report_files = []
    for rep in results.get("DiagnosticReport", []):
        att = _pick_attachment(rep.get("presentedForm", []))
        if not att:
            continue
        stem =f"{_doc_date(rep)}_{slug(cc_text(rep.get('code')) or 'report')}_{slug(rep.get('id', ''), 16)}"
        try:
            name = _save_attachment(client, att, reports, stem)
            if name:
                report_files.append(name)
        except HttpError as e:
            summary["errors"].setdefault("reports", []).append(f"{rep.get('id')}: HTTP {e.status}")
    summary["files"]["reports"] = len(report_files)

    # Flat CSVs for analysis
    obs_rows = []
    for label in ("Observation-laboratory", "Observation-vital-signs", "Observation-social-history"):
        cat = label.split("-", 1)[1]
        obs_rows += [observation_row(o, cat) for o in results.get(label, [])]
    _write_csv(out_dir / "observations.csv", OBS_FIELDS, obs_rows)

    cond_rows = []
    for label in ("Condition-problem-list", "Condition-encounter-diagnosis", "Condition-health-concern"):
        cond_rows += [condition_row(c) for c in results.get(label, [])]
    _write_csv(out_dir / "conditions.csv", COND_FIELDS, cond_rows)

    _write_csv(out_dir / "medications.csv", MED_FIELDS, [medication_row(m) for m in results.get("MedicationRequest", [])])

    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
