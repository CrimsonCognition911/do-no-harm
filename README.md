# DO NO HARM

Live emergency-medicine simulation and coaching around **OpenMRS 3**. The doctor is the primary participant; Astra on Agents API is the examiner, GPT-Live-1 is its conversational interface, and CUA shows evidence during paused review.

## Current status

This is an **offline backend foundation**, not a working clinical evaluator. Implemented: a local read-only HTTP scaffold, draft event validation, an in-memory evidence ledger and a versioned run controller with pause/resume and assistance gates. OpenMRS, Agents API, Live audio, HTTP event ingestion, adaptive clinical simulation, scoring and CUA are **not connected yet**. No API key is required for the offline components and they make no provider calls.

See the [build plan](grand_rounds_build_doc.md) and [GitHub issues](https://github.com/CrimsonCognition911/do-no-harm/issues). Issues remain open until their actual acceptance criteria are met.

## Quick start

Python 3.11+; no third-party runtime dependencies or installation step for the scaffold.

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

`GET /health` reports scaffold liveness, **not** provider or clinical readiness. `GET /ready` intentionally returns HTTP 503 until real integrations exist. `POST` is rejected; there are no clinical mutation endpoints yet. The development server binds only to loopback and is not a production server.

## Ownership

| Area | Owner | Issues |
|---|---|---|
| `backend/`, `contracts/`, clinical cases/rubrics and backend tests | @CrimsonSithria | [#1](https://github.com/CrimsonCognition911/do-no-harm/issues/1), [#3](https://github.com/CrimsonCognition911/do-no-harm/issues/3), [#4](https://github.com/CrimsonCognition911/do-no-harm/issues/4), [#6](https://github.com/CrimsonCognition911/do-no-harm/issues/6) |
| `frontend/`, `openmrs-config/`, review-browser executor and deployment | @tijoseymathew | [#2](https://github.com/CrimsonCognition911/do-no-harm/issues/2), [#5](https://github.com/CrimsonCognition911/do-no-harm/issues/5), [#7](https://github.com/CrimsonCognition911/do-no-harm/issues/7), [#8](https://github.com/CrimsonCognition911/do-no-harm/issues/8) |

The frontend and OpenMRS folders currently contain handoff instructions, not implemented applications/configuration. The frontend stack is left to its owner. Keep OpenMRS; do not build a replacement EMR.

Use separate feature branches (for example `codex/examiner` and `codex/doctor-experience`) and small PRs. Coordinate edits to the root manifest, shared plan and event schema; @CrimsonSithria owns schema changes. Do not overwrite each other's work. No Agent Forest.

## Shared contract: draft 0.1

`contracts/events.schema.json` is the first application-event draft for joint review. It is not an OpenAI API schema. Four message types share run/event identity, actor, wall-clock/simulation time, execution version, evidence IDs and visibility:

- `doctor_action`: browser observation or speech intent, distinct from backend-confirmed clinical action.
- `clinical_update`: a published case event or subsequent display/speech acknowledgment. Each stage is separately recorded; an announcement does not prove understanding.
- `evaluation_finding`: examiner-only provisional judgment. A separately authorized coaching/voice projection still needs implementation; do not send the raw finding to the doctor's assessment UI.
- `session_state`: a server-owned state snapshot. The internal run controller enforces transition legality, version checks and assistance rules; real adapters still need to implement quiescence.

The schema is served for frontend development. `backend/contracts.py` validates draft 0.1 shape and checks source against a **trusted adapter-supplied producer**. `backend/runtime.py` constructs event identity context/timestamps, checks registered resource references and same-run evidence membership, deduplicates exact retries, rejects conflicting IDs, and filters participant snapshots. Automated findings remain provisional. These checks do not establish clinical evidence sufficiency or prove that an OpenMRS write happened.

**Authentication, HTTP event ingestion, streaming and durable persistence are not implemented.** A client cannot grant itself authority by setting `producer`, `actor`, `visibility`, a resource allowlist or an event-feed audience. Future adapters must authenticate the connection, derive those values server-side and bind resources to a synthetic run before invoking internal methods. Never directly map an arbitrary request body to `Run.record()` or `Run.events()`.

For local frontend development, proxy the required paths to `http://127.0.0.1:8000`; no wildcard CORS policy is enabled. Agree the transport and session-creation endpoints under #1 before wiring live integrations.

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

This advances #1 and the control-plane portion of #6; neither issue is complete. Next backend work is authenticated transport, the reviewed case/policy runner and Astra/Live adapters. Frontend and OpenMRS configuration remain in @tijoseymathew's lane.
