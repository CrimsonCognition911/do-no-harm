import { VoiceController, correlateActions } from "/voice_controller.mjs";

const elements = {
  state: document.querySelector("#state"),
  badge: document.querySelector("#connection-badge"),
  start: document.querySelector("#start"),
  interrupt: document.querySelector("#interrupt"),
  end: document.querySelector("#end"),
  consent: document.querySelector("#consent"),
  detail: document.querySelector("#voice-detail"),
  correction: document.querySelector("#correction"),
  correctionLabel: document.querySelector("#correction-label"),
  recordCorrection: document.querySelector("#record-correction"),
  feed: document.querySelector("#feed"),
  empty: document.querySelector("#empty-feed"),
  audio: document.querySelector("#remote-audio"),
};

let csrf;
let cursor = 0;
let peer;
let channel;
let microphone;
let pollTimer;
let liveSessionId;
let voiceAttempt = 0;
let assessmentWelcome;
let welcomeSent = false;

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(csrf ? { "X-DNH-CSRF": csrf } : {}), ...(options.headers || {}) },
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}

function sendLive(event) {
  if (channel?.readyState === "open") channel.send(JSON.stringify(event));
}

function setStatus(status) {
  document.body.className = status;
  elements.state.textContent = status.replaceAll("_", " ");
  elements.badge.textContent = status === "connected" || status === "running" ? "Live" : status.replaceAll("_", " ");
  if (status === "coaching") {
    elements.correctionLabel.textContent = "Teach-back: explain your revised plan";
    elements.correction.placeholder = "Explain what you would change and why…";
  } else {
    elements.correctionLabel.textContent = "Correct or clarify your last statement";
    elements.correction.placeholder = "Say what you intended…";
  }
}

function setPlayback(enabled, { flush }) {
  elements.audio.muted = !enabled;
  if (!enabled) elements.audio.pause();
  else elements.audio.play().catch(() => {});
  if (flush) {
    // Muting alone does not drain a WebRTC receiver. Close the old transport
    // before acknowledging pause; resume requires a fresh voice connection.
    disconnectVoice({clearContext: false, technicalFault: false});
  }
}

function renderEvent(event) {
  elements.empty.hidden = true;
  const item = document.createElement("li");
  let title = event.type.replaceAll("_", " ");
  let detail = event.payload.state || event.payload.summary || event.payload.action || "Update";
  if (event.type === "doctor_action") {
    title = event.payload.phase === "confirmed" ? "Backend confirmed action" : event.payload.source === "speech" ? "Spoken intent" : "Chart interaction observed";
    item.className = event.payload.phase === "confirmed" ? "confirmed" : "observed";
  } else if (event.type === "clinical_update") {
    title = `${event.payload.delivery_stage} clinical update`;
    item.className = "update";
  }
  const copy = document.createElement("div");
  const strong = document.createElement("strong");
  strong.textContent = title;
  const description = document.createElement("small");
  description.textContent = detail;
  copy.append(strong, description);
  const time = document.createElement("small");
  time.textContent = `${Math.round(event.simulation_time_ms / 1000)}s`;
  item.append(copy, time);
  elements.feed.append(item);
}

const controller = new VoiceController({
  sendLive,
  requestTechnicalPause: (body) => api("/api/technical-pause", {method: "POST", body: JSON.stringify(body)}),
  delegate: (body) => api("/api/delegations", { method: "POST", body: JSON.stringify(body) }).catch((error) => {
    elements.detail.textContent = `Examiner unavailable: ${error.message}. The conversation remains unscored.`;
    return null;
  }),
  acknowledgeDelivery: (event, stage) => api("/api/delivery", {
    method: "POST",
    body: JSON.stringify({ event_id: event.event_id, execution_version: event.execution_version, stage }),
  }).catch(() => ({ accepted: false })),
  acknowledgeAudio: (transition, executionVersion) => api("/api/audio/ack", {
    method: "POST",
    body: JSON.stringify({ request_id: `browser-audio:${transition}:${executionVersion}`, execution_version: executionVersion, transition }),
  }),
  setPlayback,
  renderEvent,
  onStatus: setStatus,
});

async function waitForIce(connection) {
  if (connection.iceGatheringState === "complete") return;
  await new Promise((resolve) => {
    const done = () => {
      if (connection.iceGatheringState === "complete") {
        connection.removeEventListener("icegatheringstatechange", done);
        resolve();
      }
    };
    connection.addEventListener("icegatheringstatechange", done);
  });
}

async function connectVoice() {
  if (!controller.consent || !["running", "resume_requested"].includes(controller.state) ||
      (controller.fault && controller.state !== "resume_requested")) return;
  const attempt = ++voiceAttempt;
  elements.start.disabled = true;
  elements.detail.textContent = "Requesting microphone access…";
  const input = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
  if (attempt !== voiceAttempt || !controller.consent) {
    input.getTracks().forEach(track => track.stop());
    return;
  }
  microphone = input;
  peer = new RTCPeerConnection();
  const connection = peer;
  channel = peer.createDataChannel("oai-events");
  channel.addEventListener("message", ({ data }) => {
    try {
      const event = JSON.parse(data);
      if (peer === connection) {
        Promise.resolve(controller.onLiveEvent(event)).catch(() => controller.technicalPause());
        if (event.type === "session.started" && !welcomeSent && assessmentWelcome) {
          welcomeSent = true;
          sendLive({
            type: "session.commentary.append",
            event_id: `assessment-welcome:${crypto.randomUUID()}`,
            delegation_id: null,
            content: assessmentWelcome,
          });
        }
      }
    } catch { /* malformed provider events are ignored */ }
  });
  peer.addEventListener("track", (event) => { if (peer === connection) elements.audio.srcObject = event.streams[0]; });
  peer.addEventListener("connectionstatechange", () => {
    if (peer === connection && ["failed", "disconnected"].includes(connection.connectionState)) {
      disconnectVoice({ clearContext: false });
      elements.start.textContent = "Reconnect voice";
      elements.detail.textContent = "Voice disconnected. Playback stopped; requesting a server technical pause. Reconnect does not resume the run.";
    }
  });
  microphone.getTracks().forEach((track) => peer.addTrack(track, microphone));
  await connection.setLocalDescription(await connection.createOffer());
  await waitForIce(connection);
  if (attempt !== voiceAttempt) return;
  const result = await api("/api/live/session", { method: "POST", body: JSON.stringify({ sdp: connection.localDescription.sdp }) });
  if (attempt !== voiceAttempt || !controller.consent) {
    await api("/api/live/hangup", {method: "POST", body: JSON.stringify({session_id: result.session.id})}).catch(() => {});
    return;
  }
  liveSessionId = result.session.id;
  await connection.setRemoteDescription({ type: "answer", sdp: result.transport.sdp });
  elements.interrupt.disabled = false;
  elements.end.disabled = false;
  elements.detail.textContent = "Listening. You can interrupt or correct at any time; backend work continues independently.";
}

function disconnectVoice({ clearContext, technicalFault = true }) {
  voiceAttempt++;
  if (liveSessionId) {
    api("/api/live/hangup", { method: "POST", body: JSON.stringify({ session_id: liveSessionId }) }).catch(() => {});
    liveSessionId = undefined;
  }
  const previousPeer = peer;
  const previousChannel = channel;
  const previousMicrophone = microphone;
  peer = channel = microphone = undefined;
  previousMicrophone?.getTracks().forEach((track) => track.stop());
  elements.audio.srcObject?.getTracks().forEach((track) => track.stop());
  elements.audio.pause();
  elements.audio.muted = true;
  elements.audio.srcObject = null;
  previousChannel?.close();
  previousPeer?.close();
  elements.start.textContent = "Reconnect voice";
  if (technicalFault) void controller.disconnected();
  else controller.connected = false;
  if (clearContext) controller.clearConversation();
  elements.start.disabled = !controller.consent;
  elements.interrupt.disabled = true;
  elements.end.disabled = true;
}

function endVoice() {
  disconnectVoice({ clearContext: true });
  elements.start.textContent = "Start voice";
}

async function poll() {
  try {
    const feed = await api(`/api/events?after=${cursor}`);
    await controller.applyFeed(feed);
    cursor = feed.next_cursor;
  } catch (error) {
    await controller.technicalPause();
    elements.detail.textContent = `Evidence feed unavailable: ${error.message}. No clinical penalty should be inferred.`;
  } finally {
    pollTimer = setTimeout(poll, 750);
  }
}

elements.consent.addEventListener("change", async () => {
  const recording = elements.consent.checked;
  await api("/api/consent", { method: "POST", body: JSON.stringify({ recording, retention: "session_only" }) });
  controller.setConsent(recording);
  elements.start.disabled = !recording;
  if (!recording && peer) endVoice();
});
elements.start.addEventListener("click", () => connectVoice().catch((error) => {
  disconnectVoice({ clearContext: false });
  elements.detail.textContent = `Voice unavailable: ${error.message}`;
}));
elements.end.addEventListener("click", endVoice);
elements.interrupt.addEventListener("click", () => controller.technicalPause());
elements.recordCorrection.addEventListener("click", async () => {
  const value = elements.correction.value.trim();
  if (!controller.addCorrection(value)) return;
  const eventId = `speech-correction:${crypto.randomUUID()}`;
  await api("/api/actions", { method: "POST", body: JSON.stringify({
    event_id: eventId, execution_version: controller.executionVersion, action: value, kind: "speech",
  }) });
  sendLive({
    type: "session.instructions.append",
    event_id: eventId,
    delegation_id: null,
    content: "The doctor corrected the prior statement. Stop relying on it and delegate to the client now for current context.",
  });
  elements.correction.value = "";
});
window.addEventListener("dnh:chart-interaction", ({ detail }) => {
  if (!detail || typeof detail.action !== "string" || !detail.action.trim()) return;
  api("/api/actions", { method: "POST", body: JSON.stringify({
    event_id: `browser-action:${crypto.randomUUID()}`, execution_version: controller.executionVersion,
    action: detail.action.trim(), kind: "browser",
  }) }).catch(() => {});
});

const bootstrap = await api("/api/bootstrap");
csrf = bootstrap.csrf;
assessmentWelcome = bootstrap.assessment_welcome;
elements.detail.textContent = bootstrap.live_available ? "Voice is ready after consent." : "Provider credentials are not configured; the evidence feed remains available.";
elements.start.disabled = true;
poll();

window.addEventListener("beforeunload", () => {
  clearTimeout(pollTimer);
  microphone?.getTracks().forEach((track) => track.stop());
});

// Export the correlation helper for an OpenMRS extension or review overlay without
// granting it any authority to confirm an action.
window.dnhDoctorExperience = { correlateActions };
