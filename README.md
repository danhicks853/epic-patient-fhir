# epic-patient-fhir

Download your own medical record from any Epic (MyChart) health system, using
the official patient access API (SMART on FHIR). You sign in with your normal
MyChart login in your own browser. Your data is saved to a folder on your own
computer and goes nowhere else.

**Not medical advice.** This tool only retrieves records you already have a
legal right to see. Ask your care team about anything in them.

## What you get

```
exports/<org>-<timestamp>/
  observations.csv    labs, vitals, social history (date, name, value, unit, range, flag)
  conditions.csv      problem list, encounter diagnoses, health concerns
  medications.csv     prescriptions and inpatient orders
  notes/              clinical notes (.html plus a plain .txt copy)
  reports/            report text attached to results (imaging reads, pathology)
  raw/                every FHIR resource as JSON
  summary.json        counts, warnings, and anything that failed
```

No dependencies beyond Python 3.9+.

## Use it

```
git clone https://github.com/danhicks853/epic-patient-fhir.git
cd epic-patient-fhir
pip install -e .
epic-patient-fhir orgs
epic-patient-fhir export --org adventhealth
```

Your browser opens to your health system's MyChart sign-in. Approve access,
and the export runs. Access is read-only and expires after about an hour.

---

## For the maintainer: registering the app with Epic

Done once, by whoever publishes the repo. Users never do this.

Menu labels on fhir.epic.com change over time. If a label below doesn't match
exactly, go by what it means.

1. Create a free account at https://fhir.epic.com and sign in.
2. Go to **Build Apps** and create a new app.
3. **Application audience:** Patients.
4. **Incoming APIs:** add the R4 *read* and *search* APIs for:
   AllergyIntolerance, Binary, Condition (problems, encounter diagnoses, health concerns),
   DiagnosticReport, DocumentReference (clinical notes), Encounter, Immunization,
   Medication, MedicationRequest, Observation (labs, vitals, social history),
   Patient, Practitioner, Procedure.
   Select everything read-only you might ever want now. **You cannot add APIs
   after the app is marked Ready for Production.**
5. **Endpoint (redirect) URIs:** add both, exact match:
   - `http://localhost:8765/callback` (sandbox)
   - `https://danhicks853.github.io/epic-patient-fhir/callback.html` (production)

   Epic only allows https redirects in production. The https one is
   `docs/callback.html` served by GitHub Pages (repo Settings, Pages, deploy
   from the `docs` folder). It forwards the one-time code to the app on
   localhost. The code can't be redeemed without the PKCE secret, which never
   leaves the user's computer, so the bounce page sees nothing useful.
   Put the https URL in `PROD_REDIRECT_URI` in `orgs.py`.
6. **Confidential client:** No. This is a public client using PKCE. No secret.
7. **SMART on FHIR version:** R4. **Scope version:** SMART v1.
8. **Automatic client distribution:** USCDI v3 (or whichever option
   auto-distributes to all eligible Epic customers).
9. Save, but **do not mark Ready for Production yet.**
10. Copy the **Non-Production Client ID** into `NONPROD_CLIENT_ID` in
    `src/epic_patient_fhir/orgs.py`. Changes can take up to a day to reach the sandbox.
11. Test against the sandbox:
    `epic-patient-fhir export --org sandbox`
    Sign in as one of Epic's test patients (listed in the fhir.epic.com
    documentation under sandbox test data). Confirm every section in
    `summary.json` has a count and no errors.
12. Only then mark the app **Ready for Production.** This locks it.
13. Copy the **Production Client ID** into `PROD_CLIENT_ID`. Auto-distribution
    to health systems takes up to about 48 hours.
14. Test at a real health system with your own record.

## Privacy

- No server. Nothing is uploaded anywhere. The only network traffic is between
  your computer and your health system.
- Tokens live in memory for one run and are never written to disk.
- `exports/` is git-ignored. Don't commit your own data.

## Development

```
NO_PROXY=127.0.0.1,localhost PYTHONPATH=src python -m unittest -v
```

The tests run a full sign-in and export against a fake Epic server, with PKCE
verified. No network or real data.

## Roadmap

1. Sandbox validation and production registration.
2. MCP server so Claude (or any MCP client) can query your record directly.
3. Full list of Epic health systems, generated from Epic's published endpoint directory.
4. Longer sessions via Epic's dynamic client registration for native apps.
