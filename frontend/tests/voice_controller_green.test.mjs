import assert from "node:assert/strict";
import test from "node:test";
import { VoiceController, correlateActions } from "../voice_controller.mjs";

function harness() {
  const calls = { live: [], delegated: [], delivery: [], audio: [], playback: [], rendered: [], status: [] };
  const controller = new VoiceController({
    sendLive: (event) => calls.live.push(event),
    delegate: async (body) => { calls.delegated.push(body); return { spoken_update: "Verified update" }; },
    acknowledgeDelivery: async (event, stage) => calls.delivery.push([event.event_id, stage]),
    acknowledgeAudio: async (...args) => calls.audio.push(args),
    setPlayback: (...args) => calls.playback.push(args),
    renderEvent: (event) => calls.rendered.push(event.event_id),
    onStatus: (state) => calls.status.push(state),
  });
  return { controller, calls };
}

const state = (id, value, version = 1) => ({ type: "session_state", event_id: id, visibility: "participant", execution_version: version, payload: { state: value } });
const update = { type: "clinical_update", event_id: "result-1", visibility: "participant", execution_version: 1,
  payload: { delivery_stage: "published", summary: "Troponin available", scenario_event_id: "lab", resource_ref: "obs" } };

test("keeps timestamped partial transcripts and explicit corrections for client delegation", async () => {
  const { controller, calls } = harness();
  controller.setConsent(true);
  controller.connected = true;
  controller.state = "running";
  controller.onLiveEvent({ type: "session.input_transcript.delta", delta: "Order", start_ms: 10, end_ms: 80 });
  controller.addCorrection("I meant review the order");
  await controller.onLiveEvent({ type: "session.delegation.created", offset_ms: 100, delegation: { id: "opaque", target: "client" } });
  assert.equal(calls.delegated[0].transcript.length, 2);
  assert.equal(calls.delegated[0].transcript[0].partial, true);
  assert.equal(calls.delegated[0].transcript[1].corrected, true);
  assert.equal(calls.live.at(-1).type, "session.commentary.append");
  assert.equal(calls.live.at(-1).delegation_id, "opaque");
});

test("renders publication and records display without inventing spoken evidence", async () => {
  const { controller, calls } = harness();
  await controller.applyFeed({ execution_version: 1, events: [state("running", "running"), update] });
  controller.onLiveEvent({ type: "session.started" });
  assert.deepEqual(calls.delivery, [["result-1", "displayed"]]);
  assert.match(calls.live.at(-1).content, /Confirmed synthetic chart update/);
  assert.deepEqual(calls.delivery, [["result-1", "displayed"]]);
});

test("correlates observations with a separate authoritative confirmation", () => {
  const observed = { type: "doctor_action", event_id: "observed", visibility: "participant", execution_version: 1,
    payload: { action: "request labs", phase: "observed", source: "browser" } };
  const confirmed = { type: "doctor_action", event_id: "confirmed", visibility: "participant", execution_version: 1,
    evidence_ids: ["observed"], payload: { action: "labs submitted", phase: "confirmed", source: "openmrs_backend", resource_ref: "order" } };
  assert.equal(correlateActions([observed, confirmed])[0].confirmedBy.event_id, "confirmed");
});
