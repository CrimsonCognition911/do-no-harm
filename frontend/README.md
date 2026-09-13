# Doctor experience — @tijoseymathew

Own the GPT-Live-1 voice interface, doctor action capture, session status and read-only CUA review experience: [#5](https://github.com/CrimsonCognition911/do-no-harm/issues/5), [#7](https://github.com/CrimsonCognition911/do-no-harm/issues/7), [#8](https://github.com/CrimsonCognition911/do-no-harm/issues/8).

This folder reserves your work area; no frontend framework or UI is scaffolded yet. Build around the existing OpenMRS chart, not a replacement EMR.

`doctor-interface.html` and its ES modules now provide a framework-free,
fixture-labelled doctor voice shell. `doctor-voice.js` captures browser
observations and speech intentions separately, supports consent, correction,
barge-in, replay/reconnect and pause-safe playback. It requires an authenticated
same-origin BFF and injected client-delegated Live adapter; see
[`doctor-bff.md`](doctor-bff.md). No credentials or direct clinical writes are
present in browser code. Run its fixture tests with `npm test` in this directory.

1. Pull main and create your own feature branch.
2. Review the draft `contracts/events.schema.json` with @CrimsonSithria before changing message shapes. The local Python backend serves it at `/api/contracts/events`.
3. Develop client interaction capture against clearly labeled fixtures while backend event ingestion/streaming is built. Browser interactions are observations, not confirmed clinical execution.
4. Integrate one real voice -> examiner -> voice exchange early. API-backed session creation and credentials stay on the backend; never place keys in browser code.
5. CUA uses a separate read-only review browser only after pause acknowledgment or during debrief. It does not execute doctor orders, release results or modify the record.

Hidden rubric and future events must never be delivered to the active assessment client. Do not infer live provider readiness from the scaffold health endpoint; `/ready` deliberately returns 503.
