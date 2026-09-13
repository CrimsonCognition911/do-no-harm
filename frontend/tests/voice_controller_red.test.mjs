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

test("late delegation replies are discarded after pause", async () => {
  const { controller, calls } = harness();
  controller.setConsent(true);
  controller.connected = true;
  controller.state = "running";
  let resolve;
  controller.delegate = () => new Promise(r => { resolve = r; });
  const pending = controller.handleDelegation({delegation: {id: "old", target: "client"}});
  await controller.applyState(state("pause", "pause_requested", 2));
  resolve({spoken_update: "stale result"});
  await pending;
  assert.equal(calls.live.some(e => e.content === "stale result"), false);
});

test("disconnect requests a real pause and reconnect cannot clear it", async () => {
  const { controller, calls } = harness();
  controller.state = "running";
  controller.connected = true;
  let pauses = 0;
  controller.requestTechnicalPause = async () => { pauses++; return {state: "pause_requested", execution_version: 2}; };
  await controller.disconnected();
  assert.equal(pauses, 1);
  assert.equal(controller.state, "pause_requested");
  controller.onLiveEvent({type: "session.started"});
  assert.notEqual(controller.state, "running");
  assert.equal(calls.playback.some(([enabled]) => enabled), false);
});

test("generic audio ticks cannot establish a spoken receipt", async () => {
  const {controller, calls} = harness();
  controller.state = "running"; controller.connected = true;
  controller.announce({event_id: "new-result", payload: {summary: "not yet spoken"}});
  await controller.playbackObserved?.();
  assert.equal(calls.delivery.some(([,stage]) => stage === "spoken"), false);
});

test("pause acknowledgment retries after a transient failure", async () => {
  const {controller} = harness();
  let attempts = 0;
  controller.acknowledgeAudio = async () => { if (++attempts === 1) throw new Error("offline"); };
  const feed = {state: "pause_requested", execution_version: 2, events: [state("pause", "pause_requested", 2)]};
  await assert.rejects(controller.applyFeed(feed));
  await controller.applyFeed(feed);
  assert.equal(attempts, 2);
});

test("fault stays latched across stale running snapshots and clears only after server resume", async () => {
  const {controller, calls} = harness();
  controller.state = 'running'; controller.connected = true;
  let attempts = 0;
  controller.requestTechnicalPause = async () => {
    if (++attempts === 1) throw new Error('backend unavailable');
    return {state: 'pause_requested', execution_version: 2};
  };
  await controller.technicalPause();
  const id = controller.fault.id;
  await controller.applyFeed({state: 'running', execution_version: 1, events: []});
  assert.equal(attempts, 2);
  assert.equal(controller.fault.id, id);
  assert.equal(controller.state, 'pause_requested');
  assert.equal(calls.playback.some(([enabled]) => enabled), false);
  await controller.applyFeed({state: 'resume_requested', execution_version: 3, events: []});
  await controller.applyFeed({state: 'running', execution_version: 3, events: []});
  assert.equal(controller.fault, null);
  assert.equal(calls.playback.at(-1)[0], true);
});

test("paused snapshots cannot replay old running events into audible updates", async () => {
  const {controller, calls} = harness();
  controller.connected = true;
  await controller.applyFeed({state: 'paused', execution_version: 2, events: [
    state('old-running', 'running', 1),
    {type: 'clinical_update', event_id: 'old-result', visibility: 'participant', execution_version: 1,
      payload: {delivery_stage: 'published', summary: 'replayed result'}},
  ]});
  assert.equal(controller.state, 'paused');
  assert.equal(calls.playback.some(([enabled]) => enabled), false);
  assert.equal(calls.live.some(e => e.content?.includes('replayed result')), false);
});

test("disconnect/reconnect and consent revocation invalidate outstanding replies", async () => {
  for (const invalidate of [c => {void c.disconnected(); c.onLiveEvent({type: 'session.started'});}, c => c.setConsent(false)]) {
    const {controller, calls} = harness();
    controller.setConsent(true); controller.connected = true; controller.state = 'running';
    let resolve;
    controller.delegate = () => new Promise(r => {resolve = r;});
    const pending = controller.handleDelegation({delegation: {id: 'pending', target: 'client'}});
    invalidate(controller);
    resolve({spoken_update: 'must be dropped'});
    await pending;
    assert.equal(calls.live.some(e => e.content === 'must be dropped'), false);
  }
});


test("a new disconnect during resume requests a new server pause", async () => {
  const {controller} = harness();
  controller.state = 'running'; controller.connected = true;
  const ids = [];
  controller.requestTechnicalPause = async ({request_id}) => {
    ids.push(request_id);
    return {state: 'pause_requested', execution_version: ids.length * 2};
  };
  await controller.disconnected();
  await controller.applyFeed({state: 'resume_requested', execution_version: 3, events: []});
  await controller.disconnected();
  assert.equal(ids.length, 2);
  assert.notEqual(ids[0], ids[1]);
  assert.equal(controller.executionVersion, 4);
  assert.equal(controller.state, 'pause_requested');
});
