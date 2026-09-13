"""Red-path fixture tests for authority, privacy and transport failures."""

import http.client
import json
import threading
import unittest

from frontend.server import BFFError, ExaminerBridge, SessionGateway, create_server
from tests.test_doctor_interface_green import FakeGateway, RecordingClient, event


class DoctorInterfaceRedTests(unittest.TestCase):
    def setUp(self):
        self.gateway = FakeGateway()
        self.server = create_server(0, gateway=self.gateway)
        self.addCleanup(self.server.server_close)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.shutdown)
        _, bootstrap = self.request("GET", "/api/bootstrap")
        self.csrf = bootstrap["csrf"]

    def request(self, method, path, body=None, *, csrf=None, origin=None, headers=None, raw=None):
        request_headers = dict(headers or {})
        if body is not None or raw is not None:
            request_headers.setdefault("Content-Type", "application/json")
        if origin is not False:
            request_headers["Origin"] = origin or f"http://127.0.0.1:{self.server.server_port}"
        if csrf:
            request_headers["X-DNH-CSRF"] = csrf
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=raw if raw is not None else (json.dumps(body) if body is not None else None), headers=request_headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_mutations_require_exact_origin_and_csrf(self):
        body = {"recording": True, "retention": "session_only"}
        self.assertEqual(self.request("POST", "/api/consent", body, csrf=self.csrf, origin="https://attacker.example")[0], 403)
        self.assertEqual(self.request("POST", "/api/consent", body)[0], 403)
        self.assertEqual(self.request("POST", "/api/consent", body, csrf=self.csrf)[0], 200)

    def test_browser_cannot_claim_confirmation_or_backend_source(self):
        legitimate = {"event_id": "a", "execution_version": 1, "action": "order", "kind": "browser"}
        for extra in ({"phase": "confirmed"}, {"source": "openmrs_backend"}, {"actor": "examiner"}, {"visibility": "examiner"}):
            with self.subTest(extra=extra):
                self.assertEqual(self.request("POST", "/api/actions", legitimate | extra, csrf=self.csrf)[0], 422)
        self.assertEqual(self.request("POST", "/api/actions", legitimate, csrf=self.csrf)[0], 200)

    def test_gateway_drops_hidden_or_unknown_events_even_if_upstream_misbehaves(self):
        response = {"run_id": "run", "instance_id": "fixture", "state": "running", "execution_version": 1,
                    "simulation_time_ms": 0, "assisted": False, "review_allowed": False, "next_cursor": 3,
                    "events": [
                        event("evaluation_finding", "hidden", 1, {"criterion_id": "secret", "outcome": "concern", "rationale": "rubric answer", "requires_clinician_review": True}, actor="examiner", visibility="examiner"),
                        event("clinical_update", "visible", 1, {"scenario_event_id": "result", "summary": "safe", "resource_ref": "obs", "delivery_stage": "published"}),
                        event("future_event", "future", 1, {"summary": "later"}),
                        event("clinical_update", "other-run", 1, {"scenario_event_id": "other", "summary": "cross-run", "resource_ref": "obs", "delivery_stage": "published"}) | {"run_id": "different-run"},
                    ]}
        client = RecordingClient([(200, response)])
        gateway = SessionGateway("http://localhost:8000", "run", "doctor", "audio", client=client)
        feed = gateway.feed(0)
        self.assertEqual([event["event_id"] for event in feed["events"]], ["visible"])
        self.assertNotIn("rubric answer", json.dumps(feed))
        self.assertNotIn("later", json.dumps(feed))
        self.assertNotIn("cross-run", json.dumps(feed))

    def test_examiner_bridge_strips_private_fields_and_rejects_bad_safe_shape(self):
        client = RecordingClient([(200, {"status": "complete", "spoken_update": "safe", "evidence_ids": [],
                                         "finding": "hidden", "future_events": ["hidden"]})])
        bridge = ExaminerBridge("https://examiner.example", "token", client=client)
        self.assertEqual(bridge.delegate("run", {"delegation_id": "opaque", "participant_event_ids": []}),
                         {"status": "complete", "spoken_update": "safe", "evidence_ids": []})
        invalid = ExaminerBridge("https://examiner.example", "token", client=RecordingClient([(200, {"status": "complete", "spoken_update": "", "evidence_ids": []})]))
        with self.assertRaises(BFFError):
            invalid.delegate("run", {"delegation_id": "opaque", "participant_event_ids": []})

    def test_examiner_cannot_reference_evidence_outside_authorized_participant_feed(self):
        bridge = ExaminerBridge("https://examiner.example", "token", client=RecordingClient([
            (200, {"status": "complete", "spoken_update": "unsafe", "evidence_ids": ["hidden-finding"]})
        ]))
        with self.assertRaisesRegex(BFFError, "examiner_referenced_hidden_evidence"):
            bridge.delegate("run", {"delegation_id": "opaque", "participant_event_ids": ["visible-action"]})

    def test_delivery_ack_requires_a_server_observed_publication(self):
        body = {"event_id": "made-up", "execution_version": 1, "stage": "displayed"}
        status, result = self.request("POST", "/api/delivery", body, csrf=self.csrf)
        self.assertEqual((status, result["error"]), (409, "unknown_publication"))

    def test_browser_cannot_assert_spoken_receipts_even_for_known_publications(self):
        self.server.publications["known"] = 1
        body = {"event_id": "known", "execution_version": 1, "stage": "spoken"}
        self.assertEqual(self.request("POST", "/api/delivery", body, csrf=self.csrf)[0], 422)

    def test_unconfigured_dependencies_fail_closed_and_body_parser_rejects_duplicates(self):
        self.assertEqual(self.request("POST", "/api/live/session", {"sdp": "offer"}, csrf=self.csrf)[0], 409)
        self.request("POST", "/api/consent", {"recording": True, "retention": "session_only"}, csrf=self.csrf)
        self.assertEqual(self.request("POST", "/api/live/session", {"sdp": "offer"}, csrf=self.csrf)[1]["error"], "live_provider_unavailable")
        duplicate = '{"recording":true,"recording":false,"retention":"session_only"}'
        self.assertEqual(self.request("POST", "/api/consent", csrf=self.csrf, raw=duplicate)[0], 400)
        self.assertEqual(self.request("POST", "/api/live/hangup", {"session_id": "invented"}, csrf=self.csrf)[0], 404)


if __name__ == "__main__":
    unittest.main()
