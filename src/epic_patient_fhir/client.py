"""Read-only FHIR client scoped to one signed-in patient."""
from __future__ import annotations

import base64
import json
import urllib.parse

from .auth import Session
from .http import HttpError, get_json, request


class FhirClient:
    def __init__(self, session: Session):
        self.s = session
        self.base = session.fhir_base.rstrip("/")

    def _url(self, path_or_url: str) -> str:
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url
        return f"{self.base}/{path_or_url.lstrip('/')}"

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.s.access_token}"}

    def read(self, rtype: str, rid: str) -> dict:
        return get_json(self._url(f"{rtype}/{rid}"), token=self.s.access_token)

    def search(self, rtype: str, params: dict | None = None, max_pages: int = 500):
        """Return (resources, issues). Follows paging, dedupes by id.

        Epic mixes OperationOutcome entries into search bundles (for example
        "some results were filtered"); those come back as `issues`.
        """
        q = {"patient": self.s.patient_id, **(params or {})}
        url = self._url(rtype) + "?" + urllib.parse.urlencode(q)
        out, issues, seen, pages = [], [], set(), 0
        while url and pages < max_pages:
            bundle = get_json(url, token=self.s.access_token)
            pages += 1
            for entry in bundle.get("entry") or []:
                res = entry.get("resource") or {}
                rt = res.get("resourceType")
                if rt == rtype:
                    rid = res.get("id")
                    if rid and rid in seen:
                        continue
                    if rid:
                        seen.add(rid)
                    out.append(res)
                elif rt == "OperationOutcome":
                    issues.extend(res.get("issue", []))
            url = next(
                (l.get("url") for l in bundle.get("link", []) if l.get("relation") == "next"),
                None,
            )
            if url:
                url = self._url(url)
        return out, issues

    def fetch_attachment(self, att: dict):
        """Return (bytes, content_type) for a FHIR Attachment, or None."""
        ctype = att.get("contentType") or "application/octet-stream"
        if att.get("data"):
            return base64.b64decode(att["data"]), ctype
        url = att.get("url")
        if not url:
            return None
        url = self._url(url)
        try:
            body, got = request(url, headers={"Accept": ctype, **self._auth()})
            got = (got.split(";")[0].strip() or ctype)
            # Some servers answer with a FHIR Binary resource regardless.
            if "json" in got and "json" not in ctype:
                try:
                    res = json.loads(body)
                    if res.get("resourceType") == "Binary":
                        return base64.b64decode(res.get("data", "")), res.get("contentType", ctype)
                except ValueError:
                    pass
            return body, got
        except HttpError:
            res = get_json(url, token=self.s.access_token, accept="application/fhir+json")
            return base64.b64decode(res.get("data", "")), res.get("contentType", ctype)
