import test from "node:test";
import assert from "node:assert/strict";
import { DoctorVoiceController } from "../doctor-voice.js";

function setup() {
  const calls = { actions: [], deliveries: [], contexts: [], stopped: 0, interrupted: 0 };
  let ended;
  const transport = { events: async after => ({ instance_id: "i1", environment: "offline_fixture", state: "running", execution_version: 1, events: [], next_cursor: after }), action: async value => calls.actions.push(value), delivery: async value => calls.deliveries.push(value) };
  const live = { connect: async () => {}, disconnect: async () => {}, sendContext: async value => calls.contexts.push(value), interrupt: async () => { calls.interrupted++; }, speak: async (_text, { onEnded }) => { ended = onEnded; return { stop: () => { calls.stopped++; } }; } };
  const controller = new DoctorVoiceController({ transport, live, now: () => 10 });
  return { controller, calls, end: async () => ended() };
}

test("consent gates connection and browser observations never become confirmations", async () => {
  const { controller, calls } = setup();
  await assert.rejects(controller.connect(), /consent/);
  controller.setRecordingConsent(true); await controller.connect();
  await controller.recordObservedAction("open labs");
  assert.deepEqual(calls.actions[0].payload, { action: "open labs", source: "browser", phase: "observed" });
});

test("corrections preserve attribution and final speech is an intent", async () => {
  const { controller, calls } = setup(); controller.setRecordingConsent(true); await controller.connect();
  await controller.receiveSpeechTranscript({ id: "t1", text: "give aspirin" });
  await controller.receiveSpeechTranscript({ id: "t1", text: "give oxygen", final: true });
  assert.equal(calls.contexts[1].type, "correction");
  assert.deepEqual(calls.actions[0].payload, { action: "give oxygen", source: "speech", phase: "intent" });
});

test("display and speech receipts are separate and a pause prevents stale speech", async () => {
  const { controller, calls, end } = setup(); controller.setRecordingConsent(true); await controller.connect();
  const update = { type: "clinical_update", visibility: "participant", event_id: "u1", execution_version: 1, payload: { delivery_stage: "published", summary: "ECG ready" } };
  controller.receiveEvent(update); await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(calls.deliveries, [{ event_id: "u1", delivery_stage: "displayed", execution_version: 1 }]);
  controller.receiveEvent({ type: "session_state", visibility: "participant", execution_version: 2, payload: { state: "pause_requested" } });
  await end();
  assert.equal(calls.stopped, 1); assert.equal(calls.deliveries.length, 1);
});

test("hidden findings are excluded from the active voice context and barge-in stops playback", async () => {
  const { controller, calls } = setup(); controller.setRecordingConsent(true); await controller.connect();
  controller.receiveEvent({ type: "evaluation_finding", visibility: "examiner", payload: { rationale: "hidden" } });
  await controller.bargeIn();
  assert.equal(calls.contexts.length, 0); assert.equal(calls.interrupted, 1);
});
