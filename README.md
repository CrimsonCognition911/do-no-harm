# DO NO HARM

Live emergency-medicine simulation and coaching around **OpenMRS 3**. The doctor is the primary participant; Astra on Agents API is the examiner, GPT-Live-1 is its conversational interface, and CUA shows evidence during paused review.

## Current status

This is an **offline backend foundation**, not a working clinical evaluator. Implemented: draft event validation, an in-memory evidence ledger, versioned pause/resume controls, an opt-in authenticated local session API, and a frozen-policy adaptive planner tested with non-clinical fixtures. OpenMRS, Agents API, Live audio, reviewed clinical simulation, scoring and CUA are **not connected yet**. No OpenAI API key is required for the offline components and they make no provider calls.

See the [build plan](grand_rounds_build_doc.md) and [GitHub issues](https://github.com/CrimsonCognition911/do-no-harm/issues). Issues remain open until their actual acceptance criteria are met.

The selected demo is an **acute MI (anterior STEMI)**. You approved the [case YAML and clinical-review rubric](cases/stemi/README.md); the separate [approval record](cases/stemi/review.json) binds the exact reviewed files by hash and supersedes their frozen pre-review status text. The deterministic [compiled case](cases/stemi/compiled.json) is hash-bound to that review and exercises the actual adaptive planner. Runtime/scored use remains disabled until the ECG asset, local protocol, OpenMRS mappings and live integration are verified.

## Quick start

Python 3.11+.

```sh
python3 -m pip install .
```

```sh
python3 backend/app.py --port 8000
```

In another terminal:

```sh
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/api/contracts/events
python3 -m unittest discover -s tests -v
python3 -m backend.demo
```

`GET /health` reports scaffold liveness, **not** provider or clinical readiness. `GET /ready` intentionally returns HTTP 503 until real integrations exist. Without configuration, `POST` is rejected. To enable local fixture sessions, set a random 32–256-character `DNH_OPERATOR_TOKEN` securely in the backend environment. It is an application bootstrap secret, **not an OpenAI API key**. See the [session API handoff](contracts/session-api.md) for endpoints, role capabilities and examples. There are no clinical write endpoints. The development server binds only to loopback and is not a production server.

## Ownership

| Area | Owner | Issues |
|---|---|---|
| `backend/`, `contracts/`, clinical cases/rubrics and backend tests | @CrimsonSithria | [#1](https://github.com/CrimsonCognition911/do-no-harm/issues/1), [#3](https://github.com/CrimsonCognition911/do-no-harm/issues/3), [#4](https://github.com/CrimsonCognition911/do-no-harm/issues/4), [#6](https://github.com/CrimsonCognition911/do-no-harm/issues/6) |
| `frontend/`, `openmrs-config/`, review-browser executor and deployment | @tijoseymathew | [#2](https://github.com/CrimsonCognition911/do-no-harm/issues/2), [#5](https://github.com/CrimsonCognition911/do-no-harm/issues/5), [#7](https://github.com/CrimsonCognition911/do-no-harm/issues/7), [#8](https://github.com/CrimsonCognition911/do-no-harm/issues/8) |

The frontend folder contains handoff instructions. The [OpenMRS configuration pack](openmrs-config/README.md) provides a local synthetic ED workflow fixture, separate identities, live/browser tests and verified metadata mappings; clinical review remains pending. The frontend stack is left to its owner. Keep OpenMRS; do not build a replacement EMR.

Use separate feature branches (for example `codex/examiner` and `codex/doctor-experience`) and small PRs. Coordinate edits to the root manifest, shared plan and event schema; @CrimsonSithria owns schema changes. Do not overwrite each other's work. No Agent Forest.

## Shared contract: draft 0.1

`contracts/events.schema.json` is the first application-event draft for joint review. It is not an OpenAI API schema. Four message types share run/event identity, actor, wall-clock/simulation time, execution version, evidence IDs and visibility:

- `doctor_action`: browser observation or speech intent, distinct from backend-confirmed clinical action.
- `clinical_update`: a published case event or subsequent display/speech acknowledgment. Each stage is separately recorded; an announcement does not prove understanding.
- `evaluation_finding`: examiner-only provisional judgment. A separately authorized coaching/voice projection still needs implementation; do not send the raw finding to the doctor's assessment UI.
- `session_state`: a server-owned state snapshot. The internal run controller enforces transition legality, version checks and assistance rules; real adapters still need to implement quiescence.

The schema is served for frontend development. `backend/contracts.py` validates draft 0.1 shape and checks source against a **trusted adapter-supplied producer**. `backend/runtime.py` constructs event identity context/timestamps, checks registered resource references and same-run evidence membership, deduplicates exact retries, rejects conflicting IDs, and filters participant snapshots. Automated findings remain provisional. These checks do not establish clinical evidence sufficiency or prove that an OpenMRS write happened.

`backend/session_service.py` now authenticates local, expiring run-scoped capabilities for doctor, examiner, execution and audio roles. It exposes action ingestion, examiner findings, controller commands and polling replay through the [documented HTTP contract](contracts/session-api.md). The HTTP layer never accepts a client-selected `producer`, `actor`, visibility, resource allowlist or event-feed audience. Only non-clinical fixture sessions can be created; real patient bindings are unavailable. Production login/consent, provider delegation, streaming, durable persistence and real clinical adapters are still pending. Never directly map an arbitrary request body to `Run.record()` or `Run.events()`.

For local frontend development, use a server-side BFF that authenticates its own browser user and maps them to the assigned run. Keep privileged capabilities out of doctor-facing handlers. The BFF makes controlled server-to-server requests to `http://127.0.0.1:8000`; direct browser Origin headers are rejected and no wildcard CORS policy is enabled. Review the handoff together under #1 before wiring live integrations.

## Safety and next work

- Synthetic patients only. No production OpenMRS access or real patient material.
- Keep keys in ignored local configuration; never put them in the frontend, issues, screenshots or commits. `.env`/`.env.local` are not loaded by this scaffold.
- Do not claim clinical validation, certification, provider access or live integration from passing scaffold tests.
- Next: finish #1 with validated event handling, sample messages, transport/session boundaries and collaborator review; then build the case runner and examiner. Real API checks wait for secure key setup.

## Offline run controller

`python3 -m backend.demo` runs a scripted **non-clinical fixture** through the actual controller: observation, confirmed-action receipt, provisional finding, pause, coaching and resume. It prints participant-visible events and verifies blocked forged confirmation, premature review and stale action paths. It does not evaluate a doctor, generate a case, call an AI model or operate OpenMRS.

- `Run(..., resource_refs=...)` is created by a trusted backend adapter. The allowlist is fixed for this prototype; real resource creation/mapping is still pending.
- `record(...)` records evidence, not orders or event injections. Events and returned snapshots are detached copies; the ledger is append-only within the process, **not durable or tamper-proof storage**.
- `request_pause()` immediately gates new action/update receipts, freezes simulated time and advances the execution version. Both `execution` and `audio` must acknowledge the new version before the run becomes `paused`. The execution adapter must stop scheduling and reconcile/drain in-flight writes; the audio adapter must stop/flush playback. The controller cannot do those external operations itself. Missing acknowledgments leave it safely blocked; timeout/recovery is not implemented yet.
- `begin_coaching()` requires acknowledged pause and permanently marks the attempt assisted. Only then is `review_allowed` true. Assessment mode moves to debrief and cannot resume that attempt. Coached mode can request resume; adapters must first finish teach-back and revoke/drain read-only CUA access. New-version execution/audio readiness acknowledgments restart time, excluding the paused interval.
- New stale-version events fail. An exact retry of an already accepted event returns its original receipt without executing or appending anything again.
- Raw findings stay examiner-only even during coaching. A separately authorized, participant-safe coaching projection still needs implementation; do not send the examiner ledger to Live or the doctor UI.

`backend/adaptation.py` adds frozen case/rubric/policy hashes, bounded optional challenge selection, confirmed-action state preconditions, stale-work rejection and independent due-consequence planning. `contracts/adaptive-fixture.json` supplies two scripted performance paths for engineering tests, **not** a reviewed emergency case. Plans are labelled `planned_not_published`; there is no OpenMRS execution. See the handoff for the review gate and integration boundaries.

This completes the authored-and-reviewed case package in #3 and advances #1 plus the control-plane portion of #6. Next backend work is durable OpenMRS publication and Astra/Live adapters. Frontend and OpenMRS configuration remain in @tijoseymathew's lane.
