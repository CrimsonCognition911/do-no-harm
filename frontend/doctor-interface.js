import { DoctorVoiceController } from "./doctor-voice.js";

// Mountable shell; deployment injects same-origin BFF and delegated Live client.
export function mountDoctorInterface(root, dependencies) {
  root.innerHTML = `<section aria-label="Doctor voice interface"><p id="status">Connecting…</p><label><input id="consent" type="checkbox"> I consent to this fixture recording</label><button id="start" disabled>Start voice</button><button id="interrupt" disabled>Interrupt</button><p id="notice" role="status"></p><ul id="updates" aria-live="polite"></ul></section>`;
  const status = root.querySelector("#status"), notice = root.querySelector("#notice"), updates = root.querySelector("#updates");
  const controller = new DoctorVoiceController({ ...dependencies, render(message) {
    if (message.kind === "session") status.textContent = `Session: ${message.snapshot.state}`;
    if (message.kind === "connection") notice.textContent = message.fixture ? "Fixture-only voice transport" : "Voice connected";
    if (message.kind === "clinical_update" && message.delivery === "displayed") {
      const item = document.createElement("li"); item.textContent = message.event.payload.summary; updates.append(item);
    }
  }});
  root.querySelector("#consent").addEventListener("change", event => { controller.setRecordingConsent(event.target.checked); root.querySelector("#start").disabled = !event.target.checked; });
  root.querySelector("#start").addEventListener("click", () => controller.connect().catch(error => { notice.textContent = error.message; }));
  root.querySelector("#interrupt").addEventListener("click", () => controller.bargeIn());
  return controller;
}
