# DNH-06 synthetic case runner

The loopback session service can run an allowlisted case against an existing DNH-02 synthetic patient/visit. Its server scheduler checks due events every 100 ms; model proposals can select only frozen optional events, supported by same-run findings. A separate simulation identity publishes a text observation in a reassessment encounter and verifies patient, visit, encounter type, location, provider, concept and value by REST read-back. Doctor observations and speech intents cannot assert execution or publication.

## Configure and run

Create a fresh DNH-02 manifest with `openmrs-config/configure.py --synthetic-instance --output runs/openmrs/runner-manifest.json`. This adds a fresh synthetic patient/visit while preserving existing history. Use the same installed instance and existing identity credentials; do not reset shared data.

Create an ignored operator configuration, for example `runs/runner.json` (paths are relative to that file):

```json
{
  "ledger_path": "runner.sqlite3",
  "runs": [{
    "manifest": "openmrs/runner-manifest.json",
    "credentials": "openmrs/credentials.json",
    "case": "../contracts/adaptive-fixture.json",
    "review": null,
    "fixture": true,
    "bindings": {"state": "reassessment", "result": "reassessment", "challenge": "reassessment"}
  }]
}
```

Supply `DNH_OPERATOR_TOKEN` privately and run:

```sh
python3 -m backend.app --port 8000 --runner-config runs/runner.json
```

Create a session using the existing authenticated `POST /api/runs` contract. Its `request_id` must equal an allowlisted manifest's `run_key`; `mode` is `coached` or `assessment`. The response contains separate doctor, examiner, execution and audio capabilities. Resource UUIDs, credentials and case files are never accepted in HTTP requests. Start with the examiner's existing `commands` endpoint.

## Runner routes

All routes are `POST /api/runs/{run_id}/{route}`, with a bearer capability bound to that run. Existing action, finding, command, acknowledgment and event-feed contracts remain unchanged.

| Route | Capability | Body |
|---|---|---|
| `proposals` | examiner | `request_id`, `event_id`, `finding_id`, `at_ms`, `difficulty`, `reason`, `execution_version` |
| `due` | examiner | `execution_version` |
| `publish` | execution | `event_id`, `execution_version`; optional manual trigger using the same executor as the scheduler |
| `reconcile` | execution | `event_id`; read-only reconciliation of an uncertain attempt, including while paused |
| `failure` | examiner | `component` (`openmrs`, `examiner_model`, `voice_model`, `voice_transport`), `execution_version` |

The model integration in DNH-04 should submit findings/proposals and call `failure` on a failed model operation. It cannot supply event payloads or change the rubric. Speech interruption alone changes no backend state. The existing audio technical-pause endpoint handles a transport fault.

## Pause, retries and retained evidence

Pause freezes simulation time and invalidates execution versions immediately. Work admitted before pause may finish remotely while the state is `pause_requested`; execution cannot acknowledge pause until that work has drained and its outcome is verified. No new POST is admitted after pause. A late verified write is retained as an external receipt without advancing clinical state while paused. On resume, the scheduler recovers the receipt and records the publication without posting again. Assessment coaching enters debrief and ends unassisted scoring.

Before each POST, SQLite commits a durable attempt identity. Lost responses and process crashes never permit blindly retrying that POST. Reconciliation searches for the exact marker and verifies the encounter. If an attempted write is not visible, the run remains blocked from execution acknowledgment; an empty search is not proof that a slow server did not write. Preflight failures before dispatch can pause and resume without an uncertain write. Concurrent executor instances sharing the same ledger are serialized by a file lock. Use one ledger per installation and do not delete or swap it to retry a run.

SQLite retains run bindings, frozen case/rubric/policy hashes, append-only events, proposed paths, publication plans, technical failures, reconciliation and receipts. A storage failure stops the run. Restart does not silently restart time/scoring: an already-used visit is rejected, evidence is retained, and a fresh synthetic manifest is required. This version deliberately does not resume a scored attempt across process restarts.

## Supported scope and verification

Publication currently supports **text results only**, using actual installed text concepts and explicit encounter/observation timestamps. It does not turn summary text into numeric labs, doses, orders or ECG assets. Numeric units and typed clinical events need reviewed mappings before support is added. REST verification proves durable chart data; a browser check must separately establish rendering in the installed O3 UI.

The hash-approved STEMI authoring package remains gated for clinical runtime: non-fixture configuration requires a review record with `runtime_release_approved: true` as well as the existing exact compiled artifact hash. This code does not grant that approval or manufacture missing ECG/local-pathway content. Fixture mode is explicitly labelled in the session response.

Run `python3 -m unittest discover -s tests -v` and `npm test`. Regressions cover two fixed-rubric performance paths, role/run isolation, durable publication, retry/crash ambiguity, pause before/during writes, late read-back, model faults and server scheduling. `openmrs-config/test_live.py` contains a live publication gate; configure a disposable synthetic manifest and identity credentials before running it. Live OpenAI provider integration belongs to DNH-04, and browser evidence review to DNH-07.
