# DO NO HARM

Live emergency-medicine simulation and coaching around **OpenMRS 3**. The doctor is the primary participant; Astra on Agents API is the examiner, GPT-Live-1 is its conversational interface, and CUA shows evidence during paused review.

## Current status

This is the **initial project scaffold**, not a working clinical evaluator. Implemented: a local read-only backend with health/readiness endpoints, a draft event schema and HTTP tests. OpenMRS, Agents API, Live audio, event ingestion, simulation, scoring and CUA are **not connected yet**. No API key is required to run this scaffold and it makes no provider calls.

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
```

`GET /health` reports scaffold liveness, **not** provider or clinical readiness. `GET /ready` intentionally returns HTTP 503 until real integrations exist. `POST` is rejected; there are no clinical mutation endpoints yet. The development server binds only to loopback and is not a production server.

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
- `session_state`: a state snapshot. The future backend must enforce transition legality, quiescence and assistance rules.

The schema is served for frontend development. **No runtime message ingestion, authentication, schema validation, streaming, persistence or state machine is implemented yet.** A client cannot grant itself authority by setting `source`, `actor` or `visibility`; the backend must derive/check those against the authenticated source. Examiner-only events must be filtered server-side, not hidden after delivery. Evidence sufficiency and run/resource ownership are also backend checks, not established by schema validity.

For local frontend development, proxy the required paths to `http://127.0.0.1:8000`; no wildcard CORS policy is enabled. Agree the transport and session-creation endpoints under #1 before wiring live integrations.

## Safety and next work

- Synthetic patients only. No production OpenMRS access or real patient material.
- Keep keys in ignored local configuration; never put them in the frontend, issues, screenshots or commits. `.env`/`.env.local` are not loaded by this scaffold.
- Do not claim clinical validation, certification, provider access or live integration from passing scaffold tests.
- Next: finish #1 with validated event handling, sample messages, transport/session boundaries and collaborator review; then build the case runner and examiner. Real API checks wait for secure key setup.
