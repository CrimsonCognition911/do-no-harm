# Doctor experience — @tijoseymathew

Own the GPT-Live-1 voice interface, doctor action capture, session status and read-only CUA review experience: [#5](https://github.com/CrimsonCognition911/do-no-harm/issues/5), [#7](https://github.com/CrimsonCognition911/do-no-harm/issues/7), [#8](https://github.com/CrimsonCognition911/do-no-harm/issues/8).

This folder reserves your work area; no frontend framework or UI is scaffolded yet. Build around the existing OpenMRS chart, not a replacement EMR.

Application contract **0.1 is frozen**. Use these files; do not guess field names:

| Need | Where |
|---|---|
| Event envelopes | `contracts/events.schema.json` — also `GET /api/contracts/events` |
| Action → evidence → voice handshake and sample JSON | `contracts/samples/handshake.json` — also `GET /api/contracts/handshake` |
| What Live may say | `contracts/voice-update.schema.json` — also `GET /api/contracts/voice-update` and `GET /api/runs/{run_id}/voice` (audio capability) |
| HTTP routes, tokens, pause acks | `contracts/session-api.md` |

1. Pull `dev` and create your own feature branch.
2. Do not change message shapes without @CrimsonSithria. Browser interactions are observations, not confirmed clinical execution.
3. Integrate one real voice -> examiner -> voice exchange early. API-backed session creation and credentials stay on the backend; never place keys in browser code.
4. Speak only `permitted_voice_update` packets. Hidden rubric, findings and unreleased events must never reach the active assessment client or Live.
5. CUA uses a separate read-only review browser only after pause acknowledgment or during debrief. It does not execute doctor orders, release results or modify the record.

Do not infer live provider readiness from the scaffold health endpoint; `/ready` deliberately returns 503.
