# Doctor voice BFF boundary

`doctor-voice.js` never calls the fixture API from the browser and never receives
a provider key, operator token, examiner token, execution token, or audio token.
Deploy it behind an authenticated same-origin BFF which binds the signed-in doctor
to one assigned run and exposes the injected transport interface:

```js
transport.events(after)              // participant-only replay
transport.action({event_id, execution_version, payload})
transport.delivery({event_id, execution_version, delivery_stage})
```

The BFF validates CSRF and its own Origin, creates its own upstream request to
the loopback API, and permits `action` only for `{source:"browser",phase:"observed"}`
or `{source:"speech",phase:"intent"}`. It must reject client-provided run IDs,
roles, visibility, producer fields, `confirmed` phases, and arbitrary paths.
`delivery` is a server-authorized receipt path supplied by DNH-04: it records
displayed/spoken delivery separately from the authoritative published update.
It is unavailable in the current offline fixture API, so the UI must remain
labelled fixture-only until that adapter exists.

The injected `live` adapter is responsible for client delegation and implements
`connect`, `disconnect`, `sendContext`, `speak`, and `interrupt`. It may receive
only the sanitized participant events passed by this controller. GPT-Live-1 is
given relevant text/audio context only; no image/video context is accepted here.
