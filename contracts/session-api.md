# Local session API: frontend/backend handoff

Version: application events `0.1`; compiled fixture `dnh.compiled-case/0.1`.
Owner: @CrimsonSithria; frontend integration/review: @tijoseymathew.

This is a **loopback-only, synthetic fixture API**. It records observations and
exercises the run controller. It does not create provider sessions, evaluate a
doctor, publish OpenMRS changes or establish clinical readiness. `/ready` remains
503. No production patient IDs are accepted. The adaptive planner described below
is an internal component, not yet wired to these HTTP-created fixture runs.

## Start and credentials

Set a randomly generated `DNH_OPERATOR_TOKEN` (32–256 characters) in the backend
process environment through your secure local setup, then run:

```sh
python3 backend/app.py --port 8000
```

This is an **application bootstrap secret**, not an OpenAI API key. Do not paste
it into chat, commit it, put it in URLs or expose it in frontend code. The server
does not print it and does not load `.env` files. With no operator token configured,
the original read-only scaffold remains available and all POST requests return 405.

Every session endpoint uses `Authorization: Bearer <capability>`. Public health,
readiness and event-schema GETs do not require a capability. Session endpoints
accept only their exact loopback Host and reject browser Origin headers; no CORS
permission is granted. Integrate through the frontend's server/BFF, which must
authenticate its browser user, protect against CSRF, validate its own Origin and
map that user to exactly one assigned run. The BFF's upstream request is a fresh
server-to-server request, not a blind proxy of arbitrary headers/routes.

Keep the operator, examiner and worker capabilities outside the doctor-facing
BFF action handler. Never forward the complete run-creation response to the
browser. Local capabilities expire after one hour and are not user accounts,
production authentication, a recording-consent flow or a deployment configuration.

## Routes and permissions

All request bodies are JSON with exactly the documented fields. Unknown fields,
duplicate JSON keys, invalid JSON, chunked request bodies and bodies above 64 KiB
are rejected. IDs below are illustrative, not live credentials.

| Route | Capability | Request / response |
|---|---|---|
| `POST /api/runs` | Operator | `{ "request_id": "create-1", "mode": "coached" }`; `mode` may also be `assessment`. Returns 201 with run snapshot plus separate `tokens.doctor`, `tokens.examiner`, `tokens.execution`, `tokens.audio` and `expires_in_seconds`. |
| `GET /api/runs/{run_id}` | Any capability belonging to that run | Snapshot: `run_id`, `instance_id`, `environment`, `state`, `execution_version`, `simulation_time_ms`, `assisted`, `review_allowed`. No tokens or hidden findings. |
| `GET /api/runs/{run_id}/events?after=0` | Any run capability | Snapshot plus `events`, `next_cursor`; up to 200 events. Examiner gets its ledger; all other roles get participant-visible events only. |
| `POST /api/runs/{run_id}/actions` | Doctor | `event_id`, `execution_version`, `payload`. Browser source permits only `observed`; speech source permits only `intent`. Returns the server-enveloped event. |
| `POST /api/runs/{run_id}/findings` | Examiner | `event_id`, `execution_version`, `evidence_ids`, `payload` matching the finding schema. Returns an examiner-only provisional finding. |
| `POST /api/runs/{run_id}/commands` | Examiner | `request_id`, `execution_version`, `command`: `start`, `pause`, `coach`, `resume`, `end`. Returns a snapshot receipt. |
| `POST /api/runs/{run_id}/acks` | Execution or audio worker | `request_id`, `execution_version`, `transition`: `pause` or `resume`. Component identity is derived from the capability, never supplied in JSON. |

The operator token creates runs but cannot impersonate a run capability. A token
for one run cannot read or modify another run. There is no endpoint for the doctor
to select an actor, visibility, event-feed audience, patient binding, execution
source, clinical-review status, confirmed order or published clinical result.

Findings currently have shape/provisional/evidence-membership checks, not full
rubric eligibility or clinical evidence-sufficiency checks. Do not treat an
accepted finding receipt as clinical approval. A future examiner adapter must
bind the frozen rubric and establish sufficient evidence before submitting it.

## Minimal action handshake

1. A trusted local operator creates a run. Secure server-side setup distributes
   each capability only to its intended component. The doctor BFF retains only
   the doctor capability for its assigned run.
2. Examiner sends `{"request_id":"start-1","command":"start","execution_version":1}`.
3. Doctor BFF sends to `/actions`:

   ```json
   {
     "event_id": "browser-action-1",
     "execution_version": 1,
     "payload": {"action": "open labs", "phase": "observed", "source": "browser"}
   }
   ```

4. Backend returns a `doctor_action` envelope with server-bound run, actor, time,
   visibility and evidence identity. A UI click does **not** establish that an
   order was submitted or a treatment administered. Backend-confirmed actions
   require the future controlled OpenMRS adapter, not a doctor JSON flag.
5. The voice adapter polls with its audio capability. Only participant-visible
   events are returned; hidden criteria/findings never enter that feed. It must
   select appropriate grounded content rather than narrating every UI action.
   This supplies application context, **not yet an implemented GPT-Live session
   update or client-delegation bridge**. Actual provider messages and delivery
   acknowledgments remain part of #4/#5.

## Pause, coaching and resume

From running version 1:

1. Examiner sends `pause` with version 1. Receipt is `pause_requested`, version 2.
   The internal clock stops and new action receipts are gated immediately.
2. Execution worker drains/reconciles its actual work; audio worker stops and
   flushes actual playback. Each sends `{"request_id":"pause-ack-1","transition":"pause","execution_version":2}`
   using **its own** capability. Two audio acknowledgments cannot stand in for
   execution. Only both components allow state `paused`.
3. Examiner sends `coach` at version 2. The attempt becomes permanently assisted
   and `review_allowed` becomes true. Coached mode enters `coaching`; assessment
   enters `debrief` and cannot resume that scored attempt. Raw findings remain
   hidden; a separately authorized coaching projection is still pending.
4. For coached mode, finish teach-back and revoke/drain CUA review access before
   sending `resume` at version 2. It returns `resume_requested`, version 3.
   Execution/audio workers prepare the new version and acknowledge `resume` at
   version 3. Only then does the clock restart and new actions become acceptable.

Worker acknowledgments assert external operations that **the HTTP service does
not perform or verify itself**. These tests exercise fixture acknowledgments,
not actual OpenMRS-write cancellation or audio playback. Missing acknowledgments
leave the transition blocked. CUA must use a separate read-only account; this API
only exposes the state gate, not a browser executor or write permissions.

## Retries, replay and cancellation

- Retry run creation using the same `request_id` and body. It returns the same
  credentials/run with 200 while unexpired; a changed body returns 409. A new
  request ID creates a separate run and never resets a shared database.
- Retry an action/finding using its original `event_id` and exact body. It returns
  its original receipt without appending again. Conflicts and new stale-version
  events return 409. Keep transport retries separate from new participant actions.
- Command/acknowledgment idempotency keys are scoped by run and authenticated
  role. Reuse the exact request ID/body only for a retry. Its response is the
  **original receipt**, not necessarily current state; GET the snapshot afterward.
- Replay cursors count only the caller's authorized stream, so hidden findings
  cannot leak through cursor increments. Store cursor with `instance_id`, run and
  audience. Resume polling from `next_cursor`; deduplicate downstream by event ID.
  There is no SSE/WebSocket subscription yet. Asking past the stream returns 409;
  a negative/malformed cursor returns 422.
- Tokens and evidence are in process memory. Restart invalidates tokens and loses
  runs; a new instance ID/new run is a fresh attempt, never a replay continuation.
  There is no durable storage, token refresh or production recovery flow yet.
- A dropped HTTP connection or interrupted speech does not cancel a command.
  Retry to discover its receipt. A provider failure should be handled by the
  server adapter requesting an operational pause, not submitting a clinical
  penalty. Automatic disconnect/heartbeat detection and timeout recovery are
  still pending; no real clinical/event execution should be enabled before those
  adapters and lifecycle controls are implemented.

401 means missing/unknown/expired capability; 403 means wrong role/run/origin;
409 means a state, event, cursor or idempotency conflict; 422 means an invalid
request shape/value. The local service has bounded session/event/command counts,
but it is not a hardened public server or complete denial-of-service defense.

## Adaptive planner handoff

`backend/adaptation.py` validates and freezes the **compiled** case, rubric and
policy, preserving their SHA-256 hashes. The runtime planner uses same-run,
current-execution-version findings, confirmed actions and declared boolean state
preconditions. It bounds timing, difficulty and pending challenges. Insufficient
evidence cannot justify a harder branch; pending proposals expire or invalidate
on state/version changes. Already-due clinical consequences remain due regardless
of optional educational pacing.

`contracts/adaptive-fixture.json` is explicitly **non-clinical** engineering data.
It is not the case YAML promised in #3, a treatment guideline or a clinician-signed
rubric. `FrozenCase(..., fixture=True)` is for fixture testing only. Non-fixture
compilation requires an approved content hash supplied from a **trusted clinical
review registry**; that registry and reviewer identity workflow are not built yet.
Never accept an approval hash from an examiner model or compute it to auto-approve.

`propose()` and `due_events()` return `planned_not_published` instructions with
run identity, execution version and frozen hashes. They do not create a clinical
update receipt or call an external API. An eventual executor must serialize with
run control, revalidate state/version/bindings immediately before a write, publish
idempotently and verify durable read-back. Only a matching authoritative published
receipt suppresses replanning; displayed/spoken messages do not prove publication.
Planner proposals/audit are examiner-only data and must never be passed wholesale
to Live or the participant feed. Both transport and planner currently remain
offline components; wiring them to reviewed cases and OpenMRS is remaining #6 work.

No issue is closed by these fixtures. #1 still needs collaborator agreement and
provider/delegation lifecycle wiring; #3 now has a [user-approved MI authoring draft](../cases/stemi/review.json)
but still needs its release checks and runtime compilation; #4 needs verified API access and real provider proof; #6 needs verified
OpenMRS publication, durable evidence and operational failure handling.
