# epic-patient-fhir

Let Claude (or any MCP client) read your own medical record from any Epic
(MyChart) health system, using the official patient access API (SMART on
FHIR). You sign in with your normal MyChart login in your own browser. It runs
on your computer; there is no server in the middle.

Works with Claude. Not affiliated with Anthropic or Epic.

**Not medical advice.** This tool only retrieves records you already have a
legal right to see. Ask your care team about anything in them.

## Install

Requires Python 3.10+.

```
git clone https://github.com/danhicks853/epic-patient-fhir.git
cd epic-patient-fhir
pip install -e .
```

## Use with Claude

### Claude desktop app

Add this to your Claude desktop config (Settings, Developer, Edit Config),
then restart the app:

```json
{
  "mcpServers": {
    "epic-patient-fhir": {
      "command": "epic-patient-fhir-mcp"
    }
  }
}
```

If the app can't find the command (common on Windows), use Python directly:

```json
{
  "mcpServers": {
    "epic-patient-fhir": {
      "command": "python",
      "args": ["-m", "epic_patient_fhir.mcp_server"]
    }
  }
}
```

### Claude Code

```
claude mcp add epic-patient-fhir -- epic-patient-fhir-mcp
```

### Then ask

> Connect to my AdventHealth record and show my last white blood cell counts.

Claude calls `connect`, your browser opens to MyChart, you sign in and approve,
and Claude can then use these tools:

| Tool | What it returns |
|---|---|
| `list_health_systems`, `connect`, `connection_status`, `disconnect` | Sign-in management |
| `get_patient` | Name, birth date, recorded sex |
| `get_labs`, `get_lab_trend` | Lab results with ranges and flags; one test over time |
| `get_vitals` | Blood pressure, pulse, temperature, SpO2, weight |
| `get_conditions` | Problem list, encounter diagnoses, health concerns |
| `get_medications`, `get_allergies`, `get_immunizations` | Meds, allergies, vaccines |
| `get_encounters`, `get_procedures` | Visits, hospital stays, surgeries |
| `list_notes`, `get_note` | Clinical notes in full text |
| `list_reports`, `get_report` | Imaging and pathology reads with linked results |
| `export_record` | Saves everything to a folder on your computer |

Access is read-only. A sign-in lasts about an hour; Claude will ask you to
reconnect after that.

## Command line export (no AI needed)

```
epic-patient-fhir orgs
epic-patient-fhir export --org adventhealth
```

Saves to `exports/<org>-<timestamp>/`:

```
observations.csv    labs, vitals, social history (date, name, value, unit, range, flag)
conditions.csv      problem list, encounter diagnoses, health concerns
medications.csv     prescriptions and inpatient orders
notes/              clinical notes (.html plus a plain .txt copy)
reports/            report text attached to results (imaging reads, pathology)
raw/                every FHIR resource as JSON
summary.json        counts, warnings, and anything that failed
```

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

- No server run by this project. Records travel directly from your health
  system to your computer.
- **When you use it with an AI assistant, whatever a tool returns is sent to
  that assistant's provider (for Claude, Anthropic) under their terms.** Only
  what Claude asks for in your conversation is sent, not your whole record.
  The command line export sends nothing anywhere.
- Your MyChart password never reaches this software.
- Access tokens live in memory only and are gone when the program exits.
- `exports/` is git-ignored. Don't commit your own data.

## Development

```
NO_PROXY=127.0.0.1,localhost PYTHONPATH=src:tests python -m unittest -v tests/test_offline.py tests/test_mcp.py
```

The tests run a full sign-in, export, and every MCP tool against a fake Epic
server, with PKCE verified. No network or real data.

## Roadmap

1. Sandbox validation and production registration.
2. Full list of Epic health systems, generated from Epic's published endpoint directory.
3. Longer sessions via Epic's dynamic client registration for native apps.
