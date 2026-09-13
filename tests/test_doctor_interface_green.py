"""Green-path fixture tests for the doctor interface. No provider calls occur."""

import http.client
import json
import threading
import unittest

from frontend.server import ExaminerBridge, OpenAILive, SessionGateway, create_server


class RecordingClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class FakeGateway:
    run_id = "synthetic-run"

    def __init__(self):
        self.actions = []
        self.acks = []

    def feed(self, after):
        return {"run_id": self.run_id, "instance_id": "fixture", "state": "running",
                "execution_version": 1, "simulation_time_ms": 10, "assisted": False,
                "review_allowed": False, "next_cursor": after + 1,
                "events": [{"type": "session_state", "event_id": "state-1", "visibility": "participant",
                            "execution_version": 1, "simulation_time_ms": 0,
                            "payload": {"state": "running", "mode": "coached", "assisted": False}}]}

    def action(self, body):
        self.actions.append(body)
        return {"type": "doctor_action", "event_id": body["event_id"], "visibility": "participant",
                "execution_version": body["execution_version"], "simulation_time_ms": 10,
                "payload": {"action": body["action"], "phase": "intent", "source": "speech"}}

    def audio_ack(self, body):
        self.acks.append(body)
        return {"state": "paused", "execution_version": body["execution_version"]}


class DoctorInterfaceGreenTests(unittest.TestCase):
    def test_session_gateway_maps_observations_without_client_selected_authority(self):
        client = RecordingClient([(200, event("doctor_action", "a1", 2,
                                             {"action": "open labs", "phase": "observed", "source": "browser"}, actor="doctor"))])
        gateway = SessionGateway("http://127.0.0.1:8000", "run", "doctor-capability", "audio-capability", client=client)
        result = gateway.action({"event_id": "a1", "execution_version": 2, "action": "open labs", "kind": "browser"})
        self.assertEqual(result["payload"]["phase"], "observed")
        sent = client.calls[0][1]["body"]
        self.assertEqual(sent["payload"], {"action": "open labs", "source": "browser", "phase": "observed"})
        self.assertNotIn("actor", sent)
        self.assertNotIn("visibility", sent)

    def test_live_session_uses_gpt_live_client_delegation_and_returns_only_connection_data(self):
        client = RecordingClient([(201, {"session": {"id": "live_session"},
                                         "transport": {"type": "webrtc", "sdp": "answer"},
                                         "secret": "must-not-cross"}), (204, {})])
        live = OpenAILive("provider-secret", client=client)
        result = live.create("offer")
        sent = client.calls[0][1]
        self.assertEqual(sent["body"]["session"]["model"], "gpt-live-1")
        self.assertEqual(sent["body"]["session"]["delegation"], {"type": "client"})
        instructions = sent["body"]["session"]["instructions"]
        self.assertIn("professor", instructions)
        self.assertIn("Ask one brief", instructions)
        self.assertIn("partial transcript", instructions)
        self.assertEqual(sent["token"], "provider-secret")
        self.assertEqual(result, {"session": {"id": "live_session"}, "transport": {"type": "webrtc", "sdp": "answer"}})
        self.assertEqual(live.hangup("live_session"), {"ended": True})
        self.assertEqual(client.calls[1][0], "https://api.openai.com/v1/live/sessions/live_session/hangup")

    def test_loopback_bff_serves_feed_and_runs_consent_gated_voice_exchange(self):
        provider = RecordingClient([(201, {"session": {"id": "live"}, "transport": {"type": "webrtc", "sdp": "answer"}})])
        bridge_client = RecordingClient([
            (200, {"status": "complete", "spoken_update": "The verified fixture result is ready.", "evidence_ids": ["state-1"], "rubric": "hidden"}),
            (200, {"accepted": True}),
        ])
        gateway = FakeGateway()
        server = create_server(0, gateway=gateway, live=OpenAILive("secret", client=provider),
                               bridge=ExaminerBridge("https://examiner.example/bridge", "bridge-secret", client=bridge_client))
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)

        status, bootstrap = self.request(server, "GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        csrf = bootstrap["csrf"]
        self.assertNotIn("secret", json.dumps(bootstrap))
        status, result = self.request(server, "POST", "/api/live/session", {"sdp": "offer"}, csrf=csrf)
        self.assertEqual((status, result["error"]), (409, "recording_consent_required"))
        self.assertEqual(self.request(server, "POST", "/api/consent", {"recording": True, "retention": "session_only"}, csrf=csrf)[0], 200)
        self.assertEqual(self.request(server, "POST", "/api/live/session", {"sdp": "offer"}, csrf=csrf)[0], 201)
        # Poll first so evidence identity and execution version are server-bound.
        self.assertEqual(self.request(server, "GET", "/api/events?after=0")[1]["events"][0]["payload"]["state"], "running")
        delegation = {"delegation_id": "opaque-id", "offset_ms": 30, "execution_version": 1,
                      "transcript": [{"speaker": "doctor", "text": "check result", "partial": True}]}
        status, safe = self.request(server, "POST", "/api/delegations", delegation, csrf=csrf)
        self.assertEqual(status, 200)
        self.assertEqual(set(safe), {"status", "spoken_update", "evidence_ids"})
        self.assertNotIn("hidden", json.dumps(safe))
        self.assertEqual(bridge_client.calls[0][1]["timeout"], 60)
        server.publications["event-1"] = 1
        delivery = {"event_id": "event-1", "execution_version": 1, "stage": "displayed"}
        self.assertEqual(self.request(server, "POST", "/api/delivery", delivery, csrf=csrf), (200, {"accepted": True}))
        self.assertEqual(
            bridge_client.calls[1][0], "https://examiner.example/bridge/delivery"
        )

    def test_bff_discards_prior_version_evidence_after_resume(self):
        class VersionedGateway(FakeGateway):
            def __init__(self):
                super().__init__()
                self.version = 1

            def feed(self, after):
                item = event(
                    "doctor_action", f"evidence-v{self.version}", self.version,
                    {"action": "review ECG", "phase": "observed", "source": "browser"},
                    actor="doctor",
                )
                return {
                    "run_id": self.run_id, "instance_id": "fixture", "state": "running",
                    "execution_version": self.version, "simulation_time_ms": 10,
                    "assisted": False, "review_allowed": False,
                    "next_cursor": after + 1, "events": [item],
                }

        gateway = VersionedGateway()
        bridge_client = RecordingClient([
            (200, {"status": "complete", "spoken_update": "Question one?", "evidence_ids": []}),
            (200, {"status": "complete", "spoken_update": "Question two?", "evidence_ids": []}),
        ])
        server = create_server(
            0, gateway=gateway,
            bridge=ExaminerBridge("https://examiner.example/bridge", "secret", client=bridge_client),
        )
        self.addCleanup(server.server_close)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True
        )
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        _, bootstrap = self.request(server, "GET", "/api/bootstrap")
        csrf = bootstrap["csrf"]
        self.request(server, "GET", "/api/events?after=0")
        delegation = {
            "delegation_id": "d1", "offset_ms": 0, "execution_version": 1,
            "transcript": [],
        }
        self.request(server, "POST", "/api/delegations", delegation, csrf=csrf)
        gateway.version = 2
        self.request(server, "GET", "/api/events?after=1")
        self.request(
            server, "POST", "/api/delegations",
            {**delegation, "delegation_id": "d2", "execution_version": 2}, csrf=csrf,
        )
        self.assertEqual(
            bridge_client.calls[0][1]["body"]["participant_event_ids"], ["evidence-v1"]
        )
        self.assertEqual(
            bridge_client.calls[1][1]["body"]["participant_event_ids"], ["evidence-v2"]
        )

    def request(self, server, method, path, body=None, *, csrf=None, origin=True, headers=None):
        request_headers = dict(headers or {})
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        if origin:
            request_headers["Origin"] = f"http://127.0.0.1:{server.server_port}"
        if csrf:
            request_headers["X-DNH-CSRF"] = csrf
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=request_headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()


def event(kind, event_id, version, payload, *, actor="simulation_service", visibility="participant"):
    return {"schema_version": "0.1", "type": kind, "event_id": event_id, "run_id": "run", "actor": actor,
            "occurred_at": "2026-09-13T00:00:00Z", "simulation_time_ms": 0, "execution_version": version,
            "evidence_ids": [], "visibility": visibility, "payload": payload}
