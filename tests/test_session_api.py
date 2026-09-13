"""Real loopback HTTP tests: authentication, replay and controller integration."""
import http.client
import inspect
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from backend import app


OPERATOR = "synthetic-test-operator-token-32-characters"


class SessionAPITests(unittest.TestCase):
    def setUp(self):
        self.assertIn("operator_token", inspect.signature(app.create_server).parameters,
                      "Authenticated session transport is not implemented")
        self.now = 0.0
        self.server = app.create_server(0, operator_token=OPERATOR, clock=lambda: self.now)
        self.addCleanup(self.server.server_close)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.shutdown)
        status, _, self.session = self.request("POST", "/api/runs", OPERATOR, {"request_id": "create-1", "mode": "coached"})
        self.assertEqual(status, 201)
        self.path = "/api/runs/" + self.session["run_id"]
        self.tokens = self.session["tokens"]

    def request(self, method, path, token=None, body=None, *, headers=None, raw=None):
        request_headers = {"Content-Type": "application/json"}
        if token:
            request_headers["Authorization"] = "Bearer " + token
        request_headers.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            conn.request(method, path, body=raw if raw is not None else (json.dumps(body) if body is not None else None), headers=request_headers)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), json.loads(response.read())
        finally:
            conn.close()

    def command(self, command, version=1, request_id=None, role="examiner"):
        return self.request("POST", self.path + "/commands", self.tokens[role],
                            {"request_id": request_id or command, "command": command, "execution_version": version})

    def action(self, event_id="action-1", version=1, payload=None, **extra):
        body = {"event_id": event_id, "execution_version": version,
                "payload": payload or {"action": "open labs", "phase": "observed", "source": "browser"}}
        body.update(extra)
        return self.request("POST", self.path + "/actions", self.tokens["doctor"], body)

    def test_creation_is_operator_only_and_idempotent(self):
        for token in (None, "invalid", self.tokens["doctor"]):
            status, _, _ = self.request("POST", "/api/runs", token, {"request_id": "unauthorized", "mode": "coached"})
            self.assertIn(status, (401, 403))
        status, _, same = self.request("POST", "/api/runs", OPERATOR, {"request_id": "create-1", "mode": "coached"})
        self.assertEqual(status, 200)
        self.assertEqual(same, self.session)
        status, _, _ = self.request("POST", "/api/runs", OPERATOR, {"request_id": "create-1", "mode": "assessment"})
        self.assertEqual(status, 409)
        self.assertEqual(self.session["environment"], "offline_fixture")

    def test_creation_cannot_bind_arbitrary_patient_or_claim_review(self):
        status, _, _ = self.request("POST", "/api/runs", OPERATOR,
                                    {"request_id": "bad", "mode": "coached", "patient_uuid": "real-patient", "reviewed": True})
        self.assertEqual(status, 422)

    def test_capabilities_are_run_scoped_and_keys_never_appear_in_snapshots(self):
        _, _, other = self.request("POST", "/api/runs", OPERATOR, {"request_id": "other", "mode": "coached"})
        status, _, _ = self.request("GET", "/api/runs/" + other["run_id"], self.tokens["doctor"])
        self.assertEqual(status, 403)
        status, headers, snapshot = self.request("GET", self.path, self.tokens["doctor"])
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(snapshot["state"], "created")
        for secret in [OPERATOR, *self.tokens.values()]:
            self.assertNotIn(secret, json.dumps(snapshot))

    def test_doctor_action_is_server_enveloped_and_exact_retry_deduplicates(self):
        self.assertEqual(self.command("start")[0], 200)
        status, _, event = self.action()
        self.assertEqual(status, 200)
        self.assertEqual(event["actor"], "doctor")
        self.assertEqual(event["run_id"], self.session["run_id"])
        self.assertEqual(event["visibility"], "participant")
        self.assertEqual(self.action()[2], event)
        status, _, feed = self.request("GET", self.path + "/events?after=0", self.tokens["doctor"])
        self.assertEqual(status, 200)
        self.assertEqual(len(feed["events"]), 3)
        self.assertEqual(feed["next_cursor"], 3)
        self.assertEqual(self.request("GET", self.path + "/events?after=3", self.tokens["doctor"])[2]["events"], [])

    def test_doctor_cannot_forge_authority_or_control_examiner(self):
        self.command("start")
        for extra in ({"actor": "examiner"}, {"visibility": "examiner"}, {"producer": "openmrs_backend"}, {"run_id": "other"}):
            with self.subTest(extra=extra):
                self.assertEqual(self.action(**extra)[0], 422)
        self.assertEqual(self.action(payload={"action": "order", "phase": "confirmed", "source": "openmrs_backend", "resource_ref": "order-1"})[0], 403)
        self.assertEqual(self.command("pause", role="doctor")[0], 403)
        self.assertEqual(self.request("POST", self.path + "/acks", self.tokens["doctor"],
                                      {"request_id": "ack", "transition": "pause", "execution_version": 1})[0], 403)

    def test_role_inference_blocks_audio_from_impersonating_execution(self):
        self.command("start")
        self.command("pause")
        body = {"request_id": "ack", "transition": "pause", "execution_version": 2}
        status, _, snapshot = self.request("POST", self.path + "/acks", self.tokens["audio"], body)
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["state"], "pause_requested")
        self.assertEqual(self.request("POST", self.path + "/acks", self.tokens["audio"], {**body, "component": "execution"})[0], 422)
        self.assertEqual(self.request("POST", self.path + "/acks", self.tokens["execution"], body)[2]["state"], "paused")

    def test_voice_feed_is_audio_only_and_omits_findings_and_ui_clicks(self):
        self.command("start")
        self.action()
        finding = {"event_id": "finding-1", "execution_version": 1, "evidence_ids": ["action-1"],
                   "payload": {"criterion_id": "hidden", "outcome": "concern", "rationale": "Hidden answer", "requires_clinician_review": True}}
        self.assertEqual(self.request("POST", self.path + "/findings", self.tokens["examiner"], finding)[0], 200)
        self.assertEqual(self.request("GET", self.path + "/voice", self.tokens["doctor"])[0], 403)
        self.assertEqual(self.request("GET", self.path + "/voice", self.tokens["examiner"])[0], 403)
        self.assertEqual(self.request("GET", self.path + "/voice?after=0", self.tokens["audio"])[0], 422)
        status, _, body = self.request("GET", self.path + "/voice", self.tokens["audio"])
        self.assertEqual(status, 200)
        self.assertEqual(body["updates"], [])
        self.command("pause")
        for role in ("execution", "audio"):
            self.request("POST", self.path + "/acks", self.tokens[role],
                         {"request_id": "pause-ack", "transition": "pause", "execution_version": 2})
        updates = self.request("GET", self.path + "/voice", self.tokens["audio"])[2]["updates"]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["kind"], "permitted_voice_update")
        self.assertEqual(updates[0]["say"], "Simulation paused. Wait for examiner instructions.")
        blob = json.dumps(updates)
        self.assertNotIn("Hidden answer", blob)
        self.assertNotIn("criterion_id", blob)
        for secret in [OPERATOR, *self.tokens.values()]:
            self.assertNotIn(secret, blob)

    def test_hidden_findings_never_enter_doctor_or_audio_replay(self):
        self.command("start")
        self.action()
        finding = {"event_id": "finding-1", "execution_version": 1, "evidence_ids": ["action-1"],
                   "payload": {"criterion_id": "hidden", "outcome": "concern", "rationale": "Hidden answer", "requires_clinician_review": True}}
        self.assertEqual(self.request("POST", self.path + "/findings", self.tokens["examiner"], finding)[0], 200)
        self.assertEqual(self.request("POST", self.path + "/findings", self.tokens["doctor"], finding)[0], 403)
        for role in ("doctor", "audio"):
            feed = self.request("GET", self.path + "/events", self.tokens[role])[2]
            self.assertEqual(feed["next_cursor"], 3)
            self.assertNotIn("Hidden answer", json.dumps(feed))
            self.assertEqual(self.request("GET", self.path + "/events?audience=examiner", self.tokens[role])[0], 422)
        self.assertEqual(len(self.request("GET", self.path + "/events", self.tokens["examiner"])[2]["events"]), 4)

    def test_pause_resume_handshake_and_idempotent_commands(self):
        started = self.command("start")
        self.assertEqual(self.command("start"), started)
        self.now = 3
        self.assertEqual(self.command("pause")[2]["state"], "pause_requested")
        self.assertEqual(self.action()[0], 409)
        self.assertEqual(self.command("coach", 2)[0], 409)
        for role in ("execution", "audio"):
            self.assertEqual(self.request("POST", self.path + "/acks", self.tokens[role],
                                         {"request_id": "pause-ack", "transition": "pause", "execution_version": 2})[0], 200)
        self.assertTrue(self.command("coach", 2)[2]["review_allowed"])
        self.now = 30
        self.assertEqual(self.command("resume", 2)[2]["state"], "resume_requested")
        for role in ("execution", "audio"):
            self.request("POST", self.path + "/acks", self.tokens[role],
                         {"request_id": "resume-ack", "transition": "resume", "execution_version": 3})
        self.now = 32
        self.assertEqual(self.action("fresh", 3)[2]["simulation_time_ms"], 5000)

    def test_duplicate_command_does_not_accept_boolean_version(self):
        self.command("start")
        self.assertEqual(self.command("start", version=True)[0], 422)

    def test_parallel_create_retries_produce_one_session(self):
        with ThreadPoolExecutor(max_workers=4) as workers:
            responses = list(workers.map(lambda _: self.request("POST", "/api/runs", OPERATOR,
                                         {"request_id": "parallel", "mode": "coached"}), range(8)))
        self.assertEqual(sum(response[0] == 201 for response in responses), 1)
        self.assertEqual(len({response[2]["run_id"] for response in responses}), 1)

    def test_expired_tokens_are_rejected(self):
        self.now = 3601
        self.assertEqual(self.request("GET", self.path, self.tokens["doctor"])[0], 401)

    def test_body_and_request_guards_fail_closed(self):
        self.assertEqual(self.request("GET", self.path, self.tokens["doctor"], headers={"Host": "attacker.example"})[0], 403)
        self.assertEqual(self.request("GET", self.path, self.tokens["doctor"], headers={"Origin": "https://attacker.example"})[0], 403)
        self.assertEqual(self.request("POST", self.path + "/actions", self.tokens["doctor"], raw="{")[0], 400)
        self.assertEqual(self.request("POST", self.path + "/actions", self.tokens["doctor"], raw='{"event_id":"a","event_id":"b"}')[0], 400)
        self.assertEqual(self.request("POST", self.path + "/actions", self.tokens["doctor"], raw="x" * 70000)[0], 413)
        self.assertEqual(self.request("GET", self.path + "/events?after=-1", self.tokens["doctor"])[0], 422)
        self.assertEqual(self.request("GET", self.path + "/events?after=999", self.tokens["doctor"])[0], 409)

    def test_weak_operator_secret_is_rejected(self):
        with self.assertRaises(ValueError):
            app.create_server(0, operator_token="weak")

    def test_examiner_route_forwards_exact_body_and_run_scoped_token(self):
        class Bridge:
            calls = []

            def handle(self, token, body):
                self.calls.append((token, body))
                return {
                    "status": "complete",
                    "spoken_update": "What will you reassess next?",
                    "evidence_ids": [],
                }

            def handle_delivery(self, token, body):
                self.calls.append((token, body))
                return {"accepted": True}

        bridge = Bridge()
        self.server.examiner_bridge = bridge
        body = {
            "type": "live_delegation",
            "run_id": self.session["run_id"],
            "delegation_id": "delegate-http-1",
            "offset_ms": 10,
            "execution_version": 1,
            "transcript": [],
            "participant_event_ids": [],
        }
        status, _, result = self.request(
            "POST", "/api/examiner", self.tokens["examiner"], body
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["spoken_update"], "What will you reassess next?")
        self.assertEqual(bridge.calls, [(self.tokens["examiner"], body)])
        delivery = {
            "type": "delivery_ack",
            "run_id": self.session["run_id"],
            "event_id": "published-1",
            "execution_version": 1,
            "stage": "displayed",
        }
        status, _, result = self.request(
            "POST", "/api/examiner/delivery", self.tokens["examiner"], delivery
        )
        self.assertEqual((status, result), (200, {"accepted": True}))
        self.assertEqual(bridge.calls[-1], (self.tokens["examiner"], delivery))

    def test_examiner_configuration_fails_closed_on_partial_credentials(self):
        self.assertIsNone(app.configured_examiner_bridge_factory({}))
        with self.assertRaises(ValueError):
            app.configured_examiner_bridge_factory({"OPENAI_API_KEY": "secret"})
        with self.assertRaises(ValueError):
            app.configured_examiner_bridge_factory({"DNH_EXAMINER_AGENT_ID": "agent-1"})
