/**
 * Doctor-facing voice controller.
 *
 * This module deliberately has no provider key, OpenMRS credential, or direct
 * connection to the fixture API.  A same-origin, authenticated BFF supplies
 * `transport`; a provider-specific, client-delegated Live implementation
 * supplies `live`.  Both are injected so browser code cannot accidentally
 * acquire an examiner or worker capability.
 */
export class DoctorVoiceController {
  constructor({ transport, live, render = () => {}, now = () => Date.now() }) {
    if (!transport || !live) throw new TypeError("transport and live are required");
    this.transport = transport;
    this.live = live;
    this.render = render;
    this.now = now;
    this.snapshot = { state: "created", execution_version: 1, assisted: false, review_allowed: false };
    this.cursor = 0;
    this.instanceId = null;
    this.consent = false;
    this.connected = false;
    this.sequence = 0;
    this.playback = null;
    this.transcripts = new Map();
  }

  setRecordingConsent(granted) {
    this.consent = granted === true;
    if (!this.consent) this.disconnect("recording_consent_withdrawn");
    this.render({ kind: "consent", granted: this.consent });
  }

  async connect() {
    if (!this.consent) throw new Error("Recording consent is required before voice can start");
    await this.live.connect();
    this.connected = true;
    await this.reconnect();
  }

  async disconnect(reason = "user") {
    this.stopPlayback(reason);
    if (this.connected) await this.live.disconnect?.(reason);
    this.connected = false;
    this.render({ kind: "connection", connected: false, reason });
  }

  async reconnect() {
    const page = await this.transport.events(this.cursor);
    // A fresh in-memory backend instance is a new attempt, never a replay.
    if (this.instanceId && this.instanceId !== page.instance_id) this.cursor = 0;
    this.instanceId = page.instance_id;
    this.applySnapshot(page);
    for (const event of page.events || []) this.receiveEvent(event);
    this.cursor = page.next_cursor;
    this.render({ kind: "connection", connected: true, fixture: page.environment === "offline_fixture" });
  }

  applySnapshot(snapshot) {
    const priorVersion = this.snapshot.execution_version;
    this.snapshot = { ...this.snapshot, ...snapshot };
    if (snapshot.execution_version !== priorVersion || snapshot.state !== "running") {
      // Invalidate audio scheduled before a pause/resume transition.
      this.sequence += 1;
      this.stopPlayback("session_transition");
    }
    this.render({ kind: "session", snapshot: this.snapshot });
  }

  async recordObservedAction(action, resourceRef) {
    return this.recordAction(action, "browser", "observed", resourceRef);
  }

  async recordSpeechIntent(action) {
    return this.recordAction(action, "speech", "intent");
  }

  async recordAction(action, source, phase, resource_ref) {
    if (this.snapshot.state !== "running") throw new Error("Actions are disabled while the run is not running");
    if (typeof action !== "string" || !action.trim()) throw new TypeError("action is required");
    const payload = { action: action.trim(), source, phase };
    if (resource_ref) payload.resource_ref = resource_ref;
    // The BFF accepts only observed browser actions and speech intents; neither
    // browser code nor this controller can label an action confirmed.
    return this.transport.action({ event_id: this.eventId(source), execution_version: this.snapshot.execution_version, payload });
  }

  async receiveSpeechTranscript({ id, text, final = false }) {
    if (!this.consent || !this.connected || typeof text !== "string") return;
    const previous = this.transcripts.get(id);
    this.transcripts.set(id, text);
    if (previous && previous !== text) await this.live.sendContext({ type: "correction", id, replaces: previous, text });
    else await this.live.sendContext({ type: "doctor_speech", id, text, final });
    if (final) await this.recordSpeechIntent(text);
  }

  async bargeIn() {
    this.stopPlayback("barge_in");
    await this.live.interrupt?.();
    this.render({ kind: "barge_in" });
  }

  receiveEvent(event) {
    // The BFF must only return the participant stream.  Defence in depth keeps
    // an examiner finding/future payload out of the active voice context.
    if (!event || event.visibility !== "participant" || event.type === "evaluation_finding") return;
    if (event.type === "session_state") {
      this.applySnapshot({ ...event.payload, execution_version: event.execution_version });
      return;
    }
    if (event.type === "clinical_update" && event.payload.delivery_stage === "published") {
      this.presentPublishedUpdate(event);
    }
  }

  async presentPublishedUpdate(event) {
    const version = event.execution_version;
    const generation = this.sequence;
    this.render({ kind: "clinical_update", event, delivery: "displayed" });
    await this.transport.delivery({ event_id: event.event_id, delivery_stage: "displayed", execution_version: version });
    if (this.snapshot.state !== "running" || version !== this.snapshot.execution_version || generation !== this.sequence) return;
    this.playback = await this.live.speak(event.payload.summary, { onEnded: async () => {
      if (this.snapshot.state === "running" && version === this.snapshot.execution_version && generation === this.sequence) {
        await this.transport.delivery({ event_id: event.event_id, delivery_stage: "spoken", execution_version: version });
        this.render({ kind: "clinical_update", event, delivery: "spoken" });
      }
    }});
  }

  stopPlayback(reason) {
    this.playback?.stop?.(reason);
    this.playback = null;
  }

  eventId(source) { return `${source}-${this.now()}-${++this.sequence}`; }
}
