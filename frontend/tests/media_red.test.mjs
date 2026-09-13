import assert from "node:assert/strict";
import test from "node:test";
import { VoiceController } from "../voice_controller.mjs";
const state = (id, value, version) => ({ type: "session_state", event_id: id, visibility: "participant", execution_version: version, payload: { state: value } });
import { readFile } from "node:fs/promises";
import vm from "node:vm";

async function browserHarness() {
  const calls = { requests: [], stopped: 0, closed: 0, played: 0 };
  const elements = new Map();
  const track = () => ({ stop: () => calls.stopped++ });
  const stream = () => ({ getTracks: () => [track()] });
  const element = () => ({ listeners: {}, disabled: false, muted: true, currentTime: 0,
    textContent: "", value: "", addEventListener(name, fn) { this.listeners[name] = fn; },
    pause() {}, play() { calls.played++; return Promise.resolve(); }, append() {} });
  let failFeed = false;
  let feed = { execution_version: 1, events: [state("start", "running", 1)], next_cursor: 1 };
  let peer;
  class Peer {
    constructor() { peer = this; this.listeners = {}; this.iceGatheringState = "complete"; }
    createDataChannel() { this.channel = { readyState: "open", listeners: {}, send() {}, close() {},
      addEventListener(name, fn) { this.listeners[name] = fn; } }; return this.channel; }
    addEventListener(name, fn) { this.listeners[name] = fn; }
    addTrack() {}
    async createOffer() { return { type: "offer", sdp: "offer" }; }
    async setLocalDescription(value) { this.localDescription = value; }
    async setRemoteDescription() {
      this.listeners.track({ streams: [stream()] });
      this.channel.listeners.message({ data: JSON.stringify({ type: "session.started" }) });
    }
    close() { calls.closed++; }
  }
  const sandbox = { VoiceController, correlateActions: () => [], console,
    crypto: { randomUUID: () => "test-id" }, RTCPeerConnection: Peer,
    navigator: { mediaDevices: { getUserMedia: async () => stream() } },
    setTimeout: () => 1, clearTimeout() {},
    document: { body: {}, querySelector(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); }, createElement: element },
    window: { addEventListener() {} },
    fetch: async (path, options = {}) => {
      calls.requests.push({ path, body: options.body && JSON.parse(options.body), audioAttached: !!elements.get("#remote-audio")?.srcObject });
      if (path.startsWith("/api/events") && failFeed) throw new Error("offline");
      const result = path === "/api/bootstrap" ? { csrf: "test", live_available: true }
        : path.startsWith("/api/events") ? feed
        : path === "/api/technical-pause" ? {state: "pause_requested", execution_version: 2}
        : path === "/api/live/session" ? { session: { id: "live-test" }, transport: { sdp: "answer" } } : {};
      return { ok: true, json: async () => result };
    },
  };
  const source = (await readFile(new URL("../app.js", import.meta.url), "utf8")).replace(/^import .*;\n/, "");
  const api = await vm.runInNewContext(`(async () => { ${source}\nreturn { controller, connectVoice, poll }; })()`, sandbox);
  await api.poll();
  api.controller.setConsent(true);
  return { ...api, calls, elements, setFeed: (value) => { feed = value; }, failFeed: () => { failFeed = true; }, peer: () => peer };
}

test("browser pause stops tracks and detaches audio before acknowledging; reconnect restores playback", async () => {
  const h = await browserHarness();
  await h.connectVoice();
  assert.ok(h.elements.get("#remote-audio").srcObject);
  h.setFeed({ execution_version: 2, events: [state("pause-real", "pause_requested", 2)], next_cursor: 2 });
  await h.poll();
  assert.ok(h.calls.stopped >= 2);
  assert.ok(h.calls.closed >= 1);
  assert.equal(h.elements.get("#remote-audio").srcObject, null);
  const ack = h.calls.requests.find((item) => item.path === "/api/audio/ack");
  assert.equal(ack.audioAttached, false);
  h.setFeed({ execution_version: 3, events: [state("resume-real", "resume_requested", 3)], next_cursor: 3 });
  await h.poll();
  await h.connectVoice();
  h.setFeed({ execution_version: 3, events: [state("running-real", "running", 3)], next_cursor: 4 });
  await h.poll();
  assert.equal(h.elements.get("#remote-audio").muted, false);
  assert.ok(h.calls.played >= 2);
});

test("browser interrupt and feed failure close voice; explicit reconnect recovers interrupt", async () => {
  const h = await browserHarness();
  await h.connectVoice();
  await h.elements.get("#interrupt").listeners.click();
  assert.ok(h.calls.closed >= 1);
  assert.equal(h.elements.get("#start").disabled, false);
  h.setFeed({ state: "resume_requested", execution_version: 3, events: [], next_cursor: 2 });
  await h.poll();
  await h.connectVoice();
  h.setFeed({ state: "running", execution_version: 3, events: [], next_cursor: 3 });
  await h.poll();
  assert.equal(h.elements.get("#remote-audio").muted, false);
  h.failFeed();
  await h.poll();
  assert.ok(h.calls.closed >= 2);
  assert.equal(h.controller.connected, false);
  await h.connectVoice();
  assert.equal(h.calls.requests.filter((item) => item.path === "/api/live/session").length, 2);
});

test("browser media progress never submits a spoken receipt", async () => {
  const h = await browserHarness();
  await h.connectVoice();
  const audio = h.elements.get("#remote-audio");
  audio.currentTime = 5;
  await audio.listeners.timeupdate?.();
  await h.controller.playbackObserved?.();
  assert.equal(h.calls.requests.some((item) => item.path === "/api/delivery" && item.body.stage === "spoken"), false);
});

