"""Flatten FHIR resources into simple rows for CSV and analysis."""
from __future__ import annotations

import re
from html.parser import HTMLParser


def cc_text(cc) -> str:
    """Readable text for a CodeableConcept (or list of them)."""
    if not cc:
        return ""
    if isinstance(cc, list):
        return "; ".join(t for t in (cc_text(c) for c in cc) if t)
    if cc.get("text"):
        return cc["text"]
    for coding in cc.get("coding", []):
        if coding.get("display"):
            return coding["display"]
    for coding in cc.get("coding", []):
        if coding.get("code"):
            return coding["code"]
    return ""


def _quantity(q) -> tuple[str, str]:
    if not q:
        return "", ""
    v = q.get("value")
    comp = q.get("comparator", "")
    return (f"{comp}{v}" if v is not None else ""), (q.get("unit") or q.get("code") or "")


def _value(o: dict) -> tuple[str, str]:
    if "valueQuantity" in o:
        return _quantity(o["valueQuantity"])
    if "valueString" in o:
        return str(o["valueString"]), ""
    if "valueCodeableConcept" in o:
        return cc_text(o["valueCodeableConcept"]), ""
    for k in ("valueBoolean", "valueInteger", "valueDateTime", "valueTime"):
        if k in o:
            return str(o[k]), ""
    if "valueRange" in o:
        lo, u = _quantity(o["valueRange"].get("low"))
        hi, u2 = _quantity(o["valueRange"].get("high"))
        return f"{lo}-{hi}", u or u2
    comps = o.get("component") or []
    if comps:  # e.g. blood pressure: 116/62
        vals, unit = [], ""
        for c in comps:
            v, u = _value(c)
            vals.append(v)
            unit = unit or u
        return "/".join(vals), unit
    if "dataAbsentReason" in o:
        return f"(absent: {cc_text(o['dataAbsentReason'])})", ""
    return "", ""


def _ref_range(o: dict) -> str:
    rr = (o.get("referenceRange") or [{}])[0]
    if rr.get("text"):
        return rr["text"]
    lo, u = _quantity(rr.get("low"))
    hi, _ = _quantity(rr.get("high"))
    if lo or hi:
        return f"{lo}-{hi} {u}".strip()
    return ""


def obs_date(o: dict) -> str:
    return (
        o.get("effectiveDateTime")
        or (o.get("effectivePeriod") or {}).get("start")
        or o.get("issued")
        or ""
    )


OBS_FIELDS = ["date", "category", "name", "value", "unit", "reference_range", "flag", "status", "id"]


def observation_row(o: dict, category: str) -> dict:
    value, unit = _value(o)
    return {
        "date": obs_date(o),
        "category": category,
        "name": cc_text(o.get("code")),
        "value": value,
        "unit": unit,
        "reference_range": _ref_range(o),
        "flag": cc_text(o.get("interpretation")),
        "status": o.get("status", ""),
        "id": o.get("id", ""),
    }


COND_FIELDS = ["recorded", "onset", "name", "category", "clinical_status", "verification", "id"]


def condition_row(c: dict) -> dict:
    return {
        "recorded": c.get("recordedDate", ""),
        "onset": c.get("onsetDateTime") or (c.get("onsetPeriod") or {}).get("start", ""),
        "name": cc_text(c.get("code")),
        "category": cc_text(c.get("category")),
        "clinical_status": cc_text(c.get("clinicalStatus")),
        "verification": cc_text(c.get("verificationStatus")),
        "id": c.get("id", ""),
    }


MED_FIELDS = ["authored", "name", "dosage", "status", "intent", "id"]


def medication_row(m: dict) -> dict:
    name = cc_text(m.get("medicationCodeableConcept")) or (m.get("medicationReference") or {}).get("display", "")
    dosage = "; ".join(d.get("text", "") for d in m.get("dosageInstruction", []) if d.get("text"))
    return {
        "authored": m.get("authoredOn", ""),
        "name": name,
        "dosage": dosage,
        "status": m.get("status", ""),
        "intent": m.get("intent", ""),
        "id": m.get("id", ""),
    }


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag == "td":
            self.parts.append("\t")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    text = "".join(p.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def slug(s: str, n: int = 40) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", s or "").strip("-").lower()
    return (s[:n].rstrip("-")) or "untitled"
