"""Maintainer script: refresh the bundled list of Epic health systems.

Epic publishes every customer's patient-facing FHIR R4 endpoint and asks apps
not to query that directory at runtime. So we snapshot it into the package
and refresh it per release:

    python scripts/update_orgs.py
"""
import json
import re
import sys
import urllib.request
from pathlib import Path

SOURCE = "https://open.epic.com/Endpoints/R4"
OUT = Path(__file__).resolve().parents[1] / "src" / "epic_patient_fhir" / "orgs_data.json"


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def main() -> int:
    req = urllib.request.Request(SOURCE, headers={"Accept": "application/json", "User-Agent": "epic-patient-fhir"})
    bundle = json.load(urllib.request.urlopen(req, timeout=120))
    orgs, seen = [], {}
    for entry in bundle.get("entry", []):
        res = entry.get("resource") or {}
        name = (res.get("name") or "").strip()
        address = (res.get("address") or "").strip().rstrip("/")
        if res.get("status", "active") != "active" or not name or not address.startswith("https://"):
            continue
        key = slug(name)
        if key in seen:
            seen[key] += 1
            key = f"{key}-{seen[key]}"
        else:
            seen[key] = 1
        orgs.append({"key": key, "name": name, "fhir_base": address})
    orgs.sort(key=lambda o: o["name"].lower())
    OUT.write_text(json.dumps({"source": SOURCE, "count": len(orgs), "orgs": orgs}, indent=1) + "\n", encoding="utf-8")
    print(f"Wrote {len(orgs)} health systems to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
