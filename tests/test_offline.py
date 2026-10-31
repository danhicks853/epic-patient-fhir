"""End-to-end test against a fake Epic server. No network, no real data.

Exercises: SMART discovery, PKCE (verified server-side), state check,
callback, token exchange, paging, OperationOutcome handling, a 403 on one
search, note download via Binary, and CSV output.
"""
import base64
import csv
import hashlib
import http.server
import json
import socket
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

from epic_patient_fhir.auth import login
from epic_patient_fhir.client import FhirClient
from epic_patient_fhir.export import export_all

CLIENT_ID = "test-client"
PATIENT = "P123"
TOKEN = "tok-abc"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def bundle(resources, next_url=None):
    b = {"resourceType": "Bundle", "type": "searchset", "entry": [{"resource": r} for r in resources]}
    b["link"] = [{"relation": "next", "url": next_url}] if next_url else []
    return b


class FakeEpic(http.server.BaseHTTPRequestHandler):
    pending = {}  # code -> (challenge, redirect_uri)

    def log_message(self, *a):
        pass

    def _json(self, obj, status=200, ctype="application/fhir+json"):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        return self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def do_GET(self):  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        root = f"http://127.0.0.1:{self.server.server_port}"

        if u.path == "/fhir/.well-known/smart-configuration":
            return self._json(
                {"authorization_endpoint": root + "/oauth2/authorize", "token_endpoint": root + "/oauth2/token"},
                ctype="application/json",
            )
        if u.path == "/oauth2/authorize":
            assert q["client_id"] == CLIENT_ID
            assert q["code_challenge_method"] == "S256"
            assert q["aud"] == root + "/fhir"
            FakeEpic.pending["code-1"] = (q["code_challenge"], q["redirect_uri"])
            loc = q["redirect_uri"] + "?" + urllib.parse.urlencode({"code": "code-1", "state": q["state"]})
            self.send_response(302)
            self.send_header("Location", loc)
            self.end_headers()
            return

        if not self._authed():
            return self._json({"resourceType": "OperationOutcome"}, status=401)

        if u.path == f"/fhir/Patient/{PATIENT}":
            return self._json({"resourceType": "Patient", "id": PATIENT, "name": [{"text": "Test Patient"}]})

        if u.path == "/fhir/Observation" and q.get("category") == "laboratory":
            assert q.get("patient") == PATIENT or q.get("page") == "2"
            if q.get("page") == "2":
                return self._json(bundle([
                    {"resourceType": "Observation", "id": "o2", "status": "final",
                     "code": {"text": "CRP"}, "effectiveDateTime": "2026-10-29T13:06:00Z",
                     "valueQuantity": {"value": 34.6, "unit": "mg/L"}},
                    {"resourceType": "OperationOutcome", "issue": [{"diagnostics": "Some results filtered"}]},
                ]))
            return self._json(bundle([
                {"resourceType": "Observation", "id": "o1", "status": "final",
                 "code": {"text": "WBC"}, "effectiveDateTime": "2026-10-31T05:37:00Z",
                 "valueQuantity": {"value": 12.6, "unit": "10*3/uL"},
                 "referenceRange": [{"low": {"value": 4.8}, "high": {"value": 10.8}}],
                 "interpretation": [{"text": "High"}]},
                # duplicate across pages must be dropped
            ], next_url=root + "/fhir/Observation?category=laboratory&page=2"))

        if u.path == "/fhir/Observation" and q.get("category") == "vital-signs":
            return self._json(bundle([
                {"resourceType": "Observation", "id": "bp1", "status": "final", "code": {"text": "Blood pressure"},
                 "effectiveDateTime": "2026-10-30T22:00:00Z",
                 "component": [{"code": {"text": "Systolic"}, "valueQuantity": {"value": 116, "unit": "mm[Hg]"}},
                               {"code": {"text": "Diastolic"}, "valueQuantity": {"value": 62, "unit": "mm[Hg]"}}]},
            ]))

        if u.path == "/fhir/Condition" and q.get("category") == "health-concern":
            return self._json({"resourceType": "OperationOutcome"}, status=403)

        if u.path == "/fhir/DocumentReference":
            return self._json(bundle([
                {"resourceType": "DocumentReference", "id": "d1", "date": "2026-10-30T22:43:00Z",
                 "type": {"text": "Progress Note"},
                 "content": [{"attachment": {"contentType": "text/rtf", "url": "Binary/r1"}},
                             {"attachment": {"contentType": "text/html", "url": "Binary/h1"}}]},
            ]))

        if u.path == "/fhir/DiagnosticReport":
            return self._json(bundle([
                {"resourceType": "DiagnosticReport", "id": "r1", "status": "final",
                 "code": {"text": "CT Abdomen Pelvis"}, "category": [{"text": "Imaging"}],
                 "effectiveDateTime": "2026-10-29T14:40:00Z",
                 "result": [{"reference": "Observation/o1"}],
                 "presentedForm": [{"contentType": "text/html", "url": "Binary/h2"}]},
            ]))

        if u.path == "/fhir/MedicationRequest":
            return self._json(bundle([
                {"resourceType": "MedicationRequest", "id": "m1", "status": "active", "intent": "order",
                 "authoredOn": "2026-10-29", "medicationCodeableConcept": {"text": "piperacillin-tazobactam"},
                 "dosageInstruction": [{"text": "4.5 g IV q8h"}]},
            ]))

        if u.path == "/fhir/Binary/h2":
            body = b"<html><body><p>IMPRESSION: Perianal fistula with abscess.</p></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if u.path == "/fhir/Binary/h1":
            assert self.headers.get("Accept") == "text/html"
            body = b"<html><body><p>Hospital Day: 2</p><p>Plan&nbsp;continue Zosyn</p></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if u.path.startswith("/fhir/"):
            return self._json(bundle([]))
        self._json({}, status=404)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode()).items()}
        challenge, redirect = FakeEpic.pending.pop(form.get("code"), (None, None))
        computed = base64.urlsafe_b64encode(
            hashlib.sha256(form.get("code_verifier", "").encode()).digest()
        ).rstrip(b"=").decode()
        if challenge is None or computed != challenge or form.get("redirect_uri") != redirect:
            return self._json({"error": "invalid_grant"}, status=400, ctype="application/json")
        self._json(
            {"access_token": TOKEN, "token_type": "Bearer", "expires_in": 3600,
             "scope": "patient/*.read", "patient": PATIENT},
            ctype="application/json",
        )


class OfflineEndToEnd(unittest.TestCase):
    def setUp(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeEpic)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}/fhir"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _login(self):
        # Stand in for the browser: follow the authorize redirect to our callback.
        def launcher(url):
            threading.Thread(target=lambda: urllib.request.urlopen(url, timeout=10).read(), daemon=True).start()

        return login(self.base, CLIENT_ID, port=free_port(), launcher=launcher, timeout=20, log=lambda *a: None)

    def test_login_and_export(self):
        session = self._login()
        self.assertEqual(session.patient_id, PATIENT)
        self.assertEqual(session.access_token, TOKEN)

        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            summary = export_all(FhirClient(session), out, log=lambda *a: None)

            self.assertEqual(summary["counts"]["Observation-laboratory"], 2)  # paged
            self.assertIn("Some results filtered", summary["warnings"]["Observation-laboratory"])
            self.assertIn("403", summary["errors"]["Condition-health-concern"])
            self.assertEqual(summary["files"]["notes"], 1)

            notes = sorted(p.name for p in (out / "notes").iterdir())
            self.assertTrue(any(n.endswith(".html") for n in notes), notes)  # html preferred over rtf
            txt = next(p for p in (out / "notes").iterdir() if p.suffix == ".txt").read_text()
            self.assertIn("Hospital Day: 2", txt)
            self.assertIn("Plan continue Zosyn", txt)

            with (out / "observations.csv").open() as f:
                rows = list(csv.DictReader(f))
            by_name = {r["name"]: r for r in rows}
            self.assertEqual(by_name["WBC"]["value"], "12.6")
            self.assertEqual(by_name["WBC"]["reference_range"], "4.8-10.8")
            self.assertEqual(by_name["WBC"]["flag"], "High")
            self.assertEqual(by_name["Blood pressure"]["value"], "116/62")
            self.assertTrue((out / "raw" / "Patient.json").exists())

    def test_bad_verifier_rejected(self):
        # Sanity check that the fake server really enforces PKCE.
        from epic_patient_fhir.http import HttpError, post_form

        FakeEpic.pending["code-x"] = ("not-the-challenge", "http://localhost:1/callback")
        with self.assertRaises(HttpError):
            post_form(f"http://127.0.0.1:{self.server.server_port}/oauth2/token",
                      {"code": "code-x", "code_verifier": "wrong", "redirect_uri": "http://localhost:1/callback"})


if __name__ == "__main__":
    unittest.main()
