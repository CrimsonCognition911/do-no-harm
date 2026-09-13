# Doctor voice and action feed

This directory implements the doctor-facing sidecar for the configured OpenMRS
workspace. It is a small, dependency-free browser client plus a loopback Python
BFF. OpenMRS remains the chart; this UI provides voice controls, run state and an
evidence feed.

The implementation follows the official OpenAI documentation for
[GPT-Live client delegation](https://developers.openai.com/api/docs/guides/live-delegation)
and [browser WebRTC sessions](https://developers.openai.com/api/docs/guides/voice-webrtc?api=live):
the browser carries audio and events, while the trusted BFF creates the
`gpt-live-1` session with `delegation.type = client`. `OPENAI_API_KEY`, run
capabilities and the examiner bridge credential remain server-side.

Application contract **0.1 is frozen**. Use these files; do not guess field names:

| Need | Where |
|---|---|
| Event envelopes | `contracts/events.schema.json` — also `GET /api/contracts/events` |
| Action → evidence → voice handshake and sample JSON | `contracts/samples/handshake.json` — also `GET /api/contracts/handshake` |
| What Live may say | `contracts/voice-update.schema.json` — also `GET /api/contracts/voice-update` and `GET /api/runs/{run_id}/voice` (audio capability) |
| HTTP routes, tokens, pause acks | `contracts/session-api.md` |

## What is implemented

- Full-duplex browser WebRTC connection, playback interruption, explicit
  provider hangup and reconnect.
- Timestamped input/output transcript fragments plus explicit correction and
  teach-back entry. Fragments remain context; they do not become confirmed care.
- Explicit OpenMRS integration events recorded as browser observations, and
  spoken corrections recorded as intentions.
- Participant-feed validation against the shared event-contract semantics. Raw
  findings, examiner visibility and unknown future-event types are dropped.
- Display acknowledgments for published clinical updates. Browser playback ticks
  do not identify which announcement played, so spoken receipts are disabled
  until a verified provider-to-playback correlation adapter exists.
- Pause-safe playback: the microphone and WebRTC transport close before the BFF submits the
  run-scoped audio worker acknowledgment. Resume waits for a connected voice
  session. Disconnect, feed failure and Stop playback request a real server pause
  using the audio capability. Failed requests retry with the same ID; reconnect
  alone cannot resume the run. Execution must separately acknowledge quiescence,
  and the examiner must request resume. If the backend is unreachable, local audio
  stays off and server pause remains unconfirmed until connectivity returns.
- Delayed examiner responses are discarded after pause, disconnect, consent
  revocation or execution-version changes. Current snapshot state gates replay.
- Explicit recording consent. The client keeps audio/transcript context in
  memory for this browser session only and clears it on consent revocation or
  an explicit end.

## Local setup

Start the existing authenticated fixture session API and create/start a
synthetic run as described in `contracts/session-api.md` (use `--port 8010`
if port 8000 is occupied). Give the doctor BFF
only the run ID plus doctor and audio capabilities:

```sh
export DNH_SESSION_API=http://127.0.0.1:8010
export DNH_RUN_ID='<assigned synthetic run>'
export DNH_DOCTOR_CAPABILITY='<doctor capability>'
export DNH_AUDIO_CAPABILITY='<audio capability>'
python3 frontend/server.py --port 3000
```

Open `http://127.0.0.1:3000`. Do not commit or paste capability values. The BFF
binds only to loopback, checks Host/Origin and CSRF, and maps one process to one
run. This is not production user authentication.

For an actual voice session, set `OPENAI_API_KEY` only in the BFF environment.
If it is absent, the UI reports provider unavailability while the evidence feed
continues to work. Provider access has not been proven by fixture tests.

To connect the BFF delegation to the backend coordinator, start the backend with its
OpenAI key, saved `DNH_EXAMINER_AGENT_ID` and fixture-session operator token. Then
give this BFF the run-scoped **examiner** capability as its bridge token:

```sh
export DNH_EXAMINER_BRIDGE_URL='http://127.0.0.1:8010/api/examiner'
export DNH_EXAMINER_BRIDGE_TOKEN='<run-scoped examiner capability>'
```

Both values are required together. A delegation request contains its opaque
delegation ID, run/version, bounded participant transcript context, and evidence
IDs taken from the BFF's authorized feed. The response must be:

```json
{
  "status": "complete",
  "spoken_update": "Participant-safe verified result.",
  "evidence_ids": ["participant-visible-event-id"]
}
```

Only those three fields cross back to Live. Evidence references outside the
authorized participant feed fail closed. The backend chooses the rubric, treats
speech as unconfirmed context, records provisional findings privately, and returns
frozen professor copy for a next-step question or clarification. A concern returns
`pause_requested`; the BFF does not race that text into Live while the authoritative
pause closes audio. Post-pause teaching remains behind the acknowledged coaching gate.
The BFF allows up to 60 seconds for this model round trip. Display acknowledgments
use the `/delivery` child endpoint with `type: delivery_ack`, and are accepted only
for a current published event the BFF observed. They do not prove speech or audio
playback.

## OpenMRS interaction hook

An OpenMRS extension running in the same page can report an explicit chart
interaction without claiming clinical execution:

```js
window.dispatchEvent(new CustomEvent("dnh:chart-interaction", {
  detail: { action: "opened laboratory results" },
}));
```

Do not dispatch this for generic clicks. The controlled OpenMRS backend must
publish a separate `doctor_action` with `phase: confirmed`, its resource
reference and evidence attribution before the feed labels anything confirmed.

## Tests and remaining integration gate

```sh
python3 -m unittest discover -s tests -v
npm run test:green
npm run test:red
```

The tests are explicitly fixture/mock tests and make no OpenAI, examiner or
OpenMRS provider calls. The backend delegation endpoint now exists, but completing
the real voice → examiner → voice proof still requires a provider-enabled browser
run and authoritative append/playback receipts; keep the GitHub issue open until
that evidence exists.
