import assert from "node:assert/strict";
import test from "node:test";
import { VoiceController } from "../voice_controller.mjs";

function harness() {
  const calls = { live: [], delivery: [], audio: [], playback: [], rendered: [] };
  const controller = new VoiceController({
    sendLive: (event) => calls.live.push(event), delegate: async () => null,
    acknowledgeDelivery: async (...args) => calls.delivery.push(args),
    acknowledgeAudio: async (...args) => calls.audio.push(args),
    setPlayback: (...args) => calls.playback.push(args), renderEvent: (event) => calls.rendered.push(event), onStatus: () => {},
  });
  return { controller, calls };
}

const state = (id, value, version) => ({ type: "session_state", event_id: id, visibility: "participant", execution_version: version, payload: { state: value } });

test("drops examiner findings and unknown future-event types", async () => {
  const { controller, calls } = harness();
  await controller.applyFeed({ events: [
    { type: "evaluation_finding", event_id: "hidden", visibility: "examiner", execution_version: 1, payload: { rationale: "answer" } },
    { type: "future_event", event_id: "future", visibility: "participant", execution_version: 1, payload: { summary: "later" } },
  ] });
  assert.equal(calls.rendered.length, 0);
  assert.equal(controller.events.size, 0);
});

test("pause flushes speech before acknowledging and suppresses clinical announcements", async () => {
  const { controller, calls } = harness();
  controller.onLiveEvent({ type: "session.started" });
  await controller.applyFeed({ execution_version: 2, events: [
    state("pause", "pause_requested", 2),
    { type: "clinical_update", event_id: "late", visibility: "participant", execution_version: 2,
      payload: { delivery_stage: "published", summary: "must stay quiet" } },
  ] });
  assert.deepEqual(calls.playback[0], [false, { flush: true }]);
  assert.deepEqual(calls.audio, [["pause", 2]]);
  assert.equal(calls.live.some((event) => event.content?.includes("must stay quiet")), false);
});

test("duplicate replay cannot duplicate display or announcement", async () => {
  const { controller, calls } = harness();
  const running = state("running", "running", 1);
  await controller.applyFeed({ execution_version: 1, events: [running] });
  await controller.applyFeed({ execution_version: 1, events: [running] });
  assert.equal(calls.rendered.length, 1);
});

test("disconnect enters technical pause and clears playback without inventing a clinical penalty", () => {
  const { controller, calls } = harness();
  controller.disconnected();
  assert.equal(controller.connected, false);
  assert.deepEqual(calls.playback, [[false, { flush: true }]]);
});
