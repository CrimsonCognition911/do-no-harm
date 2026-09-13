"""Session integration, allowlisting, durable audit and fixed-rubric paths."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from backend.adaptation import FrozenCase
from backend.runner import RunnerFactory
from backend.session_service import APIError, SessionService
from test_execution import FakeOpenMRS, FIXTURE


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "evidence.db"
        self.now = 0
        self.case = FrozenCase(json.loads(FIXTURE.read_text()), fixture=True)
        self.client = FakeOpenMRS()
        self.factory = RunnerFactory({}, self.path)
        self.factory.entries["approved-run"] = {"case": self.case, "client": self.client,
            "bindings": {"state": "reassessment", "result": "reassessment", "challenge": "reassessment"},
            "manifest": {"fixture_only": True, "base_url": "http://localhost/openmrs", "run_key": "approved-run",
                "seed": {"patient_uuid": "patient-1", "visit_uuid": "visit-1", "fixture_only": True},
                "concepts": {"reassessment": "concept-state"},
                "encounter_types": {"Reassessment": "reassessment-type"}, "locations": {"Observation": "observation-location"},
                "identities": {"simulation": {"provider_uuid": "simulation-provider"}}, "encounter_role": "clinician-role"}}
        self.service = SessionService("x" * 32, clock=lambda: self.now, runner_factory=self.factory)
        _, self.session = self.service.handle("POST", [], {}, "x" * 32, {"request_id": "approved-run", "mode": "coached"})
        self.run_id = self.session["run_id"]
        self.tokens = self.session["tokens"]
        self.post("examiner", "commands", {"request_id": "start", "command": "start", "execution_version": 1})

    def post(self, role, route, body):
        return self.service.handle("POST", [self.run_id, route], {}, self.tokens[role], body)[1]

    def test_api_publication_is_authoritative_and_persisted(self):
        self.now = 6
        due = self.post("examiner", "due", {"execution_version": 1})
        self.assertEqual(due[0]["event_id"], "unresolved-consequence")
        request = {"event_id": "unresolved-consequence", "execution_version": 1}
        for role in ("doctor", "examiner", "audio"):
            with self.assertRaises(APIError) as caught:
                self.post(role, "publish", request)
            self.assertEqual(caught.exception.status, 403)
        first = self.post("execution", "publish", request)
        self.assertEqual(first, self.post("execution", "publish", request))
        self.assertEqual(self.client.writes, 1)
        self.assertEqual(self.post("examiner", "due", {"execution_version": 1}), [])
        connection = sqlite3.connect(self.path)
        try:
            rows = connection.execute("SELECT kind,value FROM evidence ORDER BY sequence").fetchall()
            self.assertTrue(any(k == "publication" for k, _ in rows))
            self.assertTrue(any(json.loads(v).get("type") == "clinical_update" for k,v in rows if k == "event"))
        finally:
            connection.close()

    def test_restart_and_unknown_run_fail_closed(self):
        other = SessionService("y" * 32, runner_factory=self.factory)
        for key in ("unknown", "approved-run"):
            with self.assertRaises(APIError):
                other.handle("POST", [], {}, "y" * 32, {"request_id": key, "mode": "coached"})
        self.assertEqual(self.client.writes, 0)

    def test_two_performance_paths_use_same_rubric(self):
        hashes = []
        for outcome, event_id, difficulty in (("acceptable", "harder-challenge", 2), ("concern", "paced-challenge", 1)):
            # Independent fresh session and journal for each scripted participant.
            if hashes:
                self.tearDown()
                self.setUp()
            self.post("doctor", "actions", {"event_id": "observed", "execution_version": 1,
                "payload": {"action": "inspect fixture", "phase": "observed", "source": "browser"}})
            self.post("examiner", "findings", {"event_id": "finding", "execution_version": 1, "evidence_ids": ["observed"],
                "payload": {"criterion_id": "fixture-reassessment", "outcome": outcome,
                            "rationale": "Scripted performance evidence", "requires_clinician_review": True}})
            choice = self.post("examiner", "proposals", {"request_id": "choice", "event_id": event_id,
                "finding_id": "finding", "at_ms": 1000, "difficulty": difficulty,
                "reason": "Performance path fixture", "execution_version": 1})
            hashes.append(choice["rubric_hash"])
            self.now = 1
            receipt = self.post("execution", "publish", {"event_id": event_id, "execution_version": 1})
            self.assertEqual(receipt["scenario_event_id"], event_id)
        self.assertEqual(hashes[0], hashes[1])

    def test_model_fault_is_durable_and_does_not_score_doctor(self):
        self.post("examiner", "failure", {"component": "examiner_model", "execution_version": 1})
        entry = self.service._runs[self.run_id]
        self.assertEqual(entry["run"].state, "pause_requested")
        self.assertFalse(any(e["type"] == "evaluation_finding" for e in entry["run"].events()))
        connection = sqlite3.connect(self.path)
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM evidence WHERE kind='technical_failure'").fetchone()[0], 1)
        finally:
            connection.close()

    def test_scheduler_publishes_due_events_without_client_claims(self):
        self.now = 6
        self.service.tick()
        self.service.tick()
        self.assertEqual(self.client.writes, 1)
        self.assertEqual(self.post("examiner", "due", {"execution_version": 1}), [])

    def test_http_configured_runner_preserves_authorization(self):
        from backend.app import create_server
        from threading import Thread
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        # Use the already-created session service, so the durable run is unique.
        server = create_server(0, operator_token="x" * 32)
        server.sessions = self.service
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            self.now = 6
            def publish(role):
                body = json.dumps({"event_id": "unresolved-consequence", "execution_version": 1}).encode()
                return urlopen(Request(f"http://127.0.0.1:{server.server_port}/api/runs/{self.run_id}/publish",
                    data=body, headers={"Authorization": "Bearer " + self.tokens[role], "Content-Type": "application/json"}))
            with self.assertRaises(HTTPError) as error:
                publish("doctor")
            self.assertEqual(error.exception.code, 403)
            error.exception.close()
            with publish("execution") as response:
                self.assertEqual(json.load(response)["visit_uuid"], "visit-1")
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)

    def test_service_lock_does_not_delay_pause_during_publication(self):
        from threading import Event, Thread
        admitted, release = Event(), Event()
        original = self.client.request
        errors = []
        def request(method, path, body=None):
            if method == "POST":
                admitted.set()
                if not release.wait(3):
                    raise TimeoutError()
            return original(method, path, body)
        self.client.request = request
        self.now = 6
        def publish():
            try:
                self.post("execution", "publish", {"event_id": "unresolved-consequence", "execution_version": 1})
            except Exception as error:
                errors.append(error)
        worker = Thread(target=publish)
        worker.start()
        try:
            self.assertTrue(admitted.wait(2))
            paused = self.post("examiner", "commands", {"request_id": "pause", "command": "pause", "execution_version": 1})
            self.assertEqual(paused["state"], "pause_requested")
            self.now = 60
            with self.assertRaises(APIError):
                self.post("execution", "acks", {"request_id": "ack", "transition": "pause", "execution_version": 2})
            self.assertEqual(self.service._runs[self.run_id]["run"].simulation_time_ms, 6000)
        finally:
            release.set()
            worker.join(4)
        self.assertEqual(errors, [])
        self.post("execution", "acks", {"request_id": "ack", "transition": "pause", "execution_version": 2})
        self.post("audio", "acks", {"request_id": "ack", "transition": "pause", "execution_version": 2})
        self.assertEqual(self.service._runs[self.run_id]["run"].state, "paused")

    def test_storage_failure_stops_time_and_rejects_further_actions(self):
        run = self.service._runs[self.run_id]["run"]
        def unavailable(event):
            raise OSError("disk unavailable")
        run._evidence_sink = unavailable
        self.now = 2
        with self.assertRaises(OSError):
            self.post("doctor", "actions", {"event_id": "observed", "execution_version": 1,
                "payload": {"action": "inspect", "phase": "observed", "source": "browser"}})
        self.assertEqual(run.state, "failed")
        self.now = 20
        self.assertEqual(run.simulation_time_ms, 2000)
        self.service.tick()
        self.assertEqual(self.client.writes, 0)
