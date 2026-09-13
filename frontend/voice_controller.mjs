const PARTICIPANT_TYPES = new Set(["doctor_action", "clinical_update", "session_state"]);
const QUIET_STATES = new Set(["pause_requested", "paused", "coaching", "resume_requested", "debrief", "ended", "failed", "technical_pause"]);

function text(value, maximum = 4000) {
  return typeof value === "string" && value.length > 0 && value.length <= maximum;
}

function validParticipantEvent(event) {
  return Boolean(event && typeof event === "object" && event.visibility === "participant" &&
    PARTICIPANT_TYPES.has(event.type) && text(event.event_id, 256) &&
    Number.isInteger(event.execution_version) && event.execution_version >= 1 &&
    event.payload && typeof event.payload === "object");
}

export class VoiceController {
  constructor({ sendLive, delegate, acknowledgeDelivery, acknowledgeAudio, setPlayback, renderEvent, onStatus, requestTechnicalPause = async () => { throw new Error("Pause transport unavailable"); } }) {
    this.sendLive = sendLive;
    this.requestTechnicalPause = requestTechnicalPause;
    this.generation = 0;
    this.fault = null;
    this.pauseInFlight = null;
    this.displayed = new Set();
    this.delegate = delegate;
    this.acknowledgeDelivery = acknowledgeDelivery;
    this.acknowledgeAudio = acknowledgeAudio;
    this.setPlayback = setPlayback;
    this.renderEvent = renderEvent;
    this.onStatus = onStatus;
    this.state = "created";
    this.executionVersion = 1;
    this.connected = false;
    this.consent = false;
    this.transcript = [];
    this.events = new Map();
    this.announced = new Set();
  }

  setConsent(value) {
    this.consent = value === true;
    if (!this.consent) this.clearConversation();
  }

  clearConversation() {
    this.transcript.length = 0;
    this.generation++;
  }

  addCorrection(value) {
    const correction = typeof value === "string" ? value.trim() : "";
    if (!correction) return false;
    this.transcript.push({ speaker: "doctor", text: correction, corrected: true, source: "typed", start_ms: null, end_ms: null });
    this.trimTranscript();
    return true;
  }

  trimTranscript() {
    if (this.transcript.length > 300) this.transcript.splice(0, this.transcript.length - 300);
  }

  onLiveEvent(event) {
    if (!event || typeof event !== "object") return;
    if (event.type === "session.started") {
      this.connected = true;
      this.onStatus(this.state === "created" ? "connected" : this.state);
      if (this.transcript.length) {
        const recent = this.transcript.slice(-20).map((item) => `${item.speaker}: ${item.text}`).join(" ").slice(-1500);
        this.sendLive({ type: "session.thinking.append", event_id: `reconnect-context:${crypto.randomUUID()}`,
          delegation_id: null, content: `Participant-visible context restored after reconnect: ${recent}` });
      }
      if (this.state === "resume_requested") return this.acknowledgeAudio("resume", this.executionVersion);
      if (this.state === "running" && !this.fault) {
        for (const item of this.events.values()) {
          if (item.type === "clinical_update" && item.payload.delivery_stage === "published") this.announce(item);
        }
      }
      return;
    }
    if (event.type === "session.input_transcript.delta" || event.type === "session.output_transcript.delta") {
      if (!text(event.delta)) return;
      this.transcript.push({
        speaker: event.type.includes("input") ? "doctor" : "assistant",
        text: event.delta,
        partial: true,
        start_ms: Number.isFinite(event.start_ms) ? event.start_ms : null,
        end_ms: Number.isFinite(event.end_ms) ? event.end_ms : null,
      });
      this.trimTranscript();
      return;
    }
    if (event.type === "session.delegation.created") return this.handleDelegation(event);
  }

  async handleDelegation(event) {
    if (!this.consent || !this.connected || this.state !== "running" || this.fault) return;
    const version = this.executionVersion;
    const generation = this.generation;
    const delegationId = event.delegation?.id;
    if (!text(delegationId, 256) || event.delegation?.target !== "client") return;
    const result = await this.delegate({
      delegation_id: delegationId,
      offset_ms: Number.isInteger(event.offset_ms) && event.offset_ms >= 0 ? event.offset_ms : 0,
      execution_version: this.executionVersion,
      transcript: this.transcript.map((item) => ({ ...item })),
    });
    // A concern has already moved the authoritative run toward pause. Do not
    // race a commentary append against the poll that closes/flushed WebRTC;
    // post-pause teaching requires the separately acknowledged coaching path.
    if (result?.status === "pause_requested") return;
    if (!result || !text(result.spoken_update, 2000) || version !== this.executionVersion ||
        generation !== this.generation || !this.consent || !this.connected || this.state !== "running" || this.fault) return;
    this.sendLive({
      type: "session.commentary.append",
      event_id: `delegation-result:${crypto.randomUUID()}`,
      delegation_id: delegationId,
      content: result.spoken_update,
    });
  }

  async applyFeed(feed) {
    if (!feed || !Array.isArray(feed.events)) return;
    // Replays may contain old running states. Gate playback with the current
    // snapshot before rendering history or announcing any publications.
    const latest = [...feed.events].reverse().find(e => validParticipantEvent(e) && e.type === "session_state");
    const version = feed.execution_version ?? latest?.execution_version;
    const state = feed.state ?? latest?.payload.state;
    if (this.fault && this.fault.pauseVersion === null) await this.retryTechnicalPause();
    if (this.fault && this.fault.pauseVersion !== null && state === "running" && version > this.fault.pauseVersion) {
      this.fault = null; // Only a new, completed server resume releases the latch.
    }
    if (state && version >= this.executionVersion) {
      await this.applyState({event_id: `snapshot:${version}:${state}`, execution_version: version, payload: {state}});
    }
    for (const event of feed.events) {
      if (!validParticipantEvent(event)) continue;
      if (!this.events.has(event.event_id)) {
        this.events.set(event.event_id, event);
        this.renderEvent(event);
      }
      if (event.type === "clinical_update" && event.payload.delivery_stage === "published") {
        if (!this.displayed.has(event.event_id)) {
          const receipt = await this.acknowledgeDelivery(event, "displayed");
          if (receipt?.accepted !== false) this.displayed.add(event.event_id);
        }
        this.announce(event);
      }
    }
  }

  async applyState(event) {
    const next = event.payload.state;
    if (typeof next !== "string") return;
    if (next !== this.state || event.execution_version !== this.executionVersion) this.generation++;
    this.state = next;
    this.executionVersion = event.execution_version;
    this.onStatus(next);
    if (next === "resume_requested") {
      this.setPlayback(false, { flush: false });
      if (this.connected) await this.acknowledgeAudio("resume", event.execution_version);
      return;
    }
    if (QUIET_STATES.has(next)) {
      this.setPlayback(false, { flush: true });
      if (this.connected) {
        this.sendLive({
          type: "session.instructions.append",
          event_id: `pause:${event.event_id}`,
          delegation_id: null,
          content: "Stop speaking now. The simulation is paused. Do not announce clinical updates until the application reports that it is running.",
        });
      }
      if (next === "pause_requested") await this.acknowledgeAudio("pause", event.execution_version);
      return;
    }
    if (next === "running" && !this.fault) {
      this.setPlayback(true, { flush: false });
      for (const item of this.events.values()) {
        if (item.type === "clinical_update" && item.payload.delivery_stage === "published") this.announce(item);
      }
    }
  }

  announce(event) {
    if (!this.connected || this.fault || this.state !== "running" || this.announced.has(event.event_id)) return;
    this.announced.add(event.event_id);
    this.sendLive({
      type: "session.commentary.append",
      event_id: `clinical-update:${event.event_id}`,
      delegation_id: null,
      content: `Confirmed synthetic chart update: ${event.payload.summary}`,
    });
  }

  async retryTechnicalPause() {
    if (!this.fault || this.fault.pauseVersion !== null) return;
    if (this.pauseInFlight) return this.pauseInFlight;
    const fault = this.fault;
    this.pauseInFlight = (async () => {
      try {
        const snapshot = await this.requestTechnicalPause({request_id: fault.id});
        if (!Number.isInteger(snapshot?.execution_version) || !QUIET_STATES.has(snapshot.state)) {
          throw new Error("Pause was not confirmed");
        }
        fault.pauseVersion = snapshot.execution_version;
        if (snapshot.execution_version >= this.executionVersion) {
          await this.applyState({event_id: fault.id, execution_version: snapshot.execution_version,
            payload: {state: snapshot.state}});
        }
      } catch {
        // Keep playback blocked; the next poll retries the same request ID.
        this.onStatus("technical_pause");
      }
    })();
    try { await this.pauseInFlight; } finally { this.pauseInFlight = null; }
  }

  technicalPause() {
    this.generation++;
    if (!this.fault || (this.fault.pauseVersion !== null && this.executionVersion > this.fault.pauseVersion)) {
      this.fault = {id: `audio-fault:${crypto.randomUUID()}`, pauseVersion: null};
    }
    this.setPlayback(false, {flush: true});
    this.onStatus("technical_pause");
    return this.retryTechnicalPause();
  }

  disconnected() {
    this.connected = false;
    return this.technicalPause();
  }

}

export function correlateActions(events) {
  const actions = events.filter((event) => validParticipantEvent(event) && event.type === "doctor_action");
  return actions.map((event) => {
    if (event.payload.phase !== "observed" && event.payload.phase !== "intent") return { event, confirmedBy: null };
    const confirmedBy = actions.find((candidate) => candidate.payload.phase === "confirmed" &&
      (candidate.evidence_ids?.includes(event.event_id) ||
       (candidate.payload.resource_ref && candidate.payload.resource_ref === event.payload.resource_ref))) || null;
    return { event, confirmedBy };
  });
}
