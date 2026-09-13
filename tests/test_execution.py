"""DNH-06 acceptance tests for authoritative synthetic publication."""

import importlib
import gc
import json
from pathlib import Path
import tempfile
import unittest
import warnings

from backend.adaptation import AdaptivePlanner, FrozenCase
from backend.runtime import Run


FIXTURE = Path(__file__).resolve().parents[1] / "contracts" / "adaptive-fixture.json"


class FakeOpenMRS:
    """Complete boundary fake for the two REST operations used by the publisher."""

    def __init__(self):
        self.encounters = []
        self.writes = 0
        self.fail_after_write = False
        self.wrong_visit_on_read = False
        self.wrong_creator_on_read = False

    def all(self, resource):
        if resource != "encounter?patient=patient-1":
            raise AssertionError(f"unexpected collection request: {resource}")
        return [json.loads(json.dumps(item)) for item in self.encounters]

    def request(self, method, path, body=None):
        if method == "GET" and path == "patient/patient-1?v=full":
            return {"uuid": "patient-1", "person": {"display": "DNH SYNTHETIC"}, "identifiers": [{"identifier": "DNH-test"}]}
        if method == "GET" and path == "visit/visit-1?v=full":
            return {"uuid": "visit-1", "patient": {"uuid": "patient-1"}, "stopDatetime": None}
        if method == "GET" and path == "provider/simulation-provider?v=full":
            return {"uuid": "simulation-provider", "identifier": "dnh-simulation", "retired": False}
        if method == "GET" and path.startswith("concept/"):
            return {"uuid": path.removeprefix("concept/").removesuffix("?v=full"), "datatype": {"display": "Text"}, "retired": False}
        if method == "POST" and path == "encounter":
            self.writes += 1
            encounter = {
                "uuid": f"encounter-{self.writes}",
                "patient": {"uuid": body["patient"]},
                "visit": {"uuid": body["visit"]},
                "encounterType": {"uuid": body["encounterType"]},
                "location": {"uuid": body["location"]},
                "auditInfo": {"creator": {"uuid": "simulation-user"}},
                "encounterProviders": [{
                    "provider": {"uuid": body["encounterProviders"][0]["provider"]},
                    "encounterRole": {"uuid": body["encounterProviders"][0]["encounterRole"]},
                }],
                "obs": [{
                    "uuid": f"obs-{self.writes}",
                    "concept": {"uuid": body["obs"][0]["concept"]},
                    "value": body["obs"][0]["value"],
                    "comment": body["obs"][0]["comment"],
                }],
            }
            self.encounters.append(encounter)
            if self.fail_after_write:
                self.fail_after_write = False
                raise OSError("connection dropped after OpenMRS accepted the encounter")
            return {"uuid": encounter["uuid"]}
        if method == "GET" and path.startswith("encounter/") and path.endswith("?v=full"):
            uuid = path[len("encounter/"):-len("?v=full")]
            match = next(item for item in self.encounters if item["uuid"] == uuid)
            result = json.loads(json.dumps(match))
            if self.wrong_visit_on_read:
                result["visit"]["uuid"] = "other-visit"
            if self.wrong_creator_on_read:
                result["auditInfo"]["creator"]["uuid"] = "doctor-user"
            return result
        raise AssertionError(f"unexpected request: {method} {path}")


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        try:
            self.execution = importlib.import_module("backend.execution")
        except ModuleNotFoundError:
            self.fail("The DNH-06 authoritative execution adapter is not implemented")
        self.now = 0.0
        self.run = Run(
            "run-1",
            resource_refs={"concept-state", "concept-result", "concept-challenge"},
            clock=lambda: self.now,
        )
        self.run.start(expected_version=1)
        self.now = 6.0
        case = FrozenCase(json.loads(FIXTURE.read_text()), fixture=True)
        self.planner = AdaptivePlanner(
            case,
            self.run,
            bindings={
                "state": "concept-state",
                "result": "concept-result",
                "challenge": "concept-challenge",
            },
        )
        self.client = FakeOpenMRS()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.ledger = Path(self.directory.name) / "publication.sqlite3"
        self.binding = {
            "fixture_only": True,
            "run_id": "run-1",
            "patient_uuid": "patient-1",
            "visit_uuid": "visit-1",
            "encounter_type_uuid": "reassessment-type",
            "location_uuid": "observation-location",
            "provider_uuid": "simulation-provider",
            "simulation_user_uuid": "simulation-user",
            "encounter_role_uuid": "clinician-role",
            "concept_uuids": ["concept-state", "concept-result", "concept-challenge"],
        }

    def executor(self, **changes):
        binding = {**self.binding, **changes}
        return self.execution.OpenMRSExecutor(
            self.run,
            self.planner,
            self.client,
            binding=binding,
            ledger_path=self.ledger,
        )

    def resume(self):
        self.run.acknowledge_pause("execution", expected_version=2)
        self.run.acknowledge_pause("audio", expected_version=2)
        self.run.request_resume(expected_version=2)
        self.run.acknowledge_resume("execution", expected_version=3)
        self.run.acknowledge_resume("audio", expected_version=3)

    def test_due_result_is_published_once_and_read_back_from_bound_visit(self):
        executor = self.executor()
        first = executor.publish_due("unresolved-consequence", expected_version=1)
        retry = executor.publish_due("unresolved-consequence", expected_version=1)

        self.assertEqual(first, retry)
        self.assertEqual(self.client.writes, 1)
        self.assertEqual(first["run_id"], "run-1")
        self.assertEqual(first["scenario_event_id"], "unresolved-consequence")
        self.assertEqual(first["visit_uuid"], "visit-1")
        self.assertEqual(first["encounter_uuid"], "encounter-1")
        publications = [event for event in self.run.events() if event["type"] == "clinical_update"]
        self.assertEqual(len(publications), 1)
        self.assertEqual(publications[0]["payload"]["delivery_stage"], "published")

    def test_ambiguous_network_failure_pauses_without_penalty_and_retry_recovers_write(self):
        self.client.fail_after_write = True
        executor = self.executor()
        with self.assertRaisesRegex(self.execution.ExternalPublicationError, "OpenMRS publication failed"):
            executor.publish_due("unresolved-consequence", expected_version=1)

        self.assertEqual(self.run.state, "pause_requested")
        self.assertEqual(self.run.simulation_time_ms, 6000)
        self.assertFalse(any(event["type"] == "evaluation_finding" for event in self.run.events()))
        executor.reconcile("unresolved-consequence")
        self.resume()

        receipt = self.executor().publish_due("unresolved-consequence", expected_version=3)
        self.assertEqual(receipt["encounter_uuid"], "encounter-1")
        self.assertEqual(self.client.writes, 1)

    def test_restart_revalidates_durable_receipt_and_rehydrates_run_evidence(self):
        first = self.executor().publish_due("unresolved-consequence", expected_version=1)
        restarted = Run(
            "run-1",
            resource_refs={"concept-state", "concept-result", "concept-challenge"},
            clock=lambda: self.now,
        )
        restarted.start(expected_version=1)
        case = FrozenCase(json.loads(FIXTURE.read_text()), fixture=True)
        planner = AdaptivePlanner(
            case,
            restarted,
            bindings={
                "state": "concept-state",
                "result": "concept-result",
                "challenge": "concept-challenge",
            },
        )
        executor = self.execution.OpenMRSExecutor(
            restarted,
            planner,
            self.client,
            binding=self.binding,
            ledger_path=self.ledger,
        )

        recovered = executor.publish_due("unresolved-consequence", expected_version=1)

        self.assertEqual(recovered, first)
        self.assertEqual(self.client.writes, 1)
        publications = [event for event in restarted.events() if event["type"] == "clinical_update"]
        self.assertEqual(len(publications), 1)
        self.assertEqual(publications[0]["payload"]["scenario_event_id"], "unresolved-consequence")

    def test_wrong_visit_readback_fails_closed_and_pauses(self):
        self.client.wrong_visit_on_read = True
        with self.assertRaisesRegex(self.execution.ExternalPublicationError, "read-back"):
            self.executor().publish_due("unresolved-consequence", expected_version=1)
        self.assertEqual(self.run.state, "pause_requested")
        self.assertFalse(any(event["type"] == "clinical_update" for event in self.run.events()))

    def test_wrong_creator_readback_fails_closed_and_pauses(self):
        self.client.wrong_creator_on_read = True
        with self.assertRaisesRegex(self.execution.ExternalPublicationError, "read-back"):
            self.executor().publish_due("unresolved-consequence", expected_version=1)
        self.assertEqual(self.run.state, "pause_requested")
        self.assertFalse(any(event["type"] == "clinical_update" for event in self.run.events()))

    def test_paused_stale_wrong_run_and_client_selected_events_cannot_write(self):
        with self.assertRaises(ValueError):
            self.executor().publish_due("delayed-result", expected_version=1)
        self.assertEqual(self.client.writes, 0)

        with self.assertRaises(ValueError):
            self.executor(run_id="other-run")

        self.run.request_pause(expected_version=1)
        with self.assertRaises(ValueError):
            self.executor().publish_due("unresolved-consequence", expected_version=1)
        self.assertEqual(self.client.writes, 0)

    def test_model_failure_pauses_without_scoring_and_speech_interruption_preserves_state(self):
        executor = self.executor()
        snapshot = executor.speech_interrupted(expected_version=1)
        self.assertEqual(snapshot, {"state": "running", "execution_version": 1})
        self.assertEqual(self.run.state, "running")

        executor.external_failure("examiner_model", expected_version=1)
        self.assertEqual(self.run.state, "pause_requested")
        self.assertFalse(any(event["type"] == "evaluation_finding" for event in self.run.events()))

    def test_publication_ledger_does_not_leak_database_connections(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            ledger = self.execution.PublicationLedger(self.ledger)
            for _ in range(20):
                self.assertIsNone(ledger.get("run-1", "not-published"))
            del ledger
            gc.collect()
        self.assertEqual([item for item in caught if item.category is ResourceWarning], [])


if __name__ == "__main__":
    unittest.main()

class ExecutionRaceTests(ExecutionTests):
    def test_pause_during_post_freezes_time_and_waits_for_readback(self):
        from threading import Event, Thread
        admitted, release = Event(), Event()
        original = self.client.request
        def request(method, path, body=None):
            if method == "POST":
                admitted.set()
                if not release.wait(5):
                    raise TimeoutError()
            return original(method, path, body)
        self.client.request = request
        executor = self.executor()
        results = []
        worker = Thread(target=lambda: results.append(executor.publish_due("unresolved-consequence", expected_version=1)))
        worker.start()
        self.assertTrue(admitted.wait(2))
        self.run.request_pause(expected_version=1)
        self.now = 100
        self.assertEqual(self.run.simulation_time_ms, 6000)
        with self.assertRaisesRegex(ValueError, "quiescent"):
            self.run.acknowledge_pause("execution", expected_version=2)
        self.run.acknowledge_pause("audio", expected_version=2)
        release.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(results), 1)
        self.assertFalse(any(e["type"] == "clinical_update" for e in self.run.events()))
        self.run.acknowledge_pause("execution", expected_version=2)
        self.assertEqual(self.run.state, "paused")
        self.assertEqual(self.client.writes, 1)
        with self.assertRaises(ValueError):
            executor.publish_due("delayed-result", expected_version=1)
        self.assertEqual(self.client.writes, 1)

    def test_pause_during_preflight_prevents_post(self):
        original = self.client.all
        def all(resource):
            self.run.request_pause(expected_version=1)
            return original(resource)
        self.client.all = all
        with self.assertRaises(self.execution.ExternalPublicationError):
            self.executor().publish_due("unresolved-consequence", expected_version=1)
        self.assertEqual(self.client.writes, 0)

    def test_uncertain_invisible_write_cannot_be_posted_again(self):
        self.client.fail_after_write = True
        executor = self.executor()
        with self.assertRaises(self.execution.ExternalPublicationError):
            executor.publish_due("unresolved-consequence", expected_version=1)
        self.client.all = lambda _: []  # server is slow/eventually visible
        with self.assertRaises(self.execution.ExternalPublicationError):
            executor.reconcile("unresolved-consequence")
        with self.assertRaises(ValueError):
            self.run.acknowledge_pause("execution", expected_version=2)
        self.assertEqual(self.client.writes, 1)

    def test_concurrent_duplicate_publish_returns_same_receipt(self):
        from concurrent.futures import ThreadPoolExecutor
        executor = self.executor()
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(executor.publish_due, "unresolved-consequence", expected_version=1) for _ in range(2)]
            receipts = [f.result() for f in futures]
        self.assertEqual(receipts[0], receipts[1])
        self.assertEqual(self.client.writes, 1)

    def test_changed_frozen_rubric_rejects_existing_receipt(self):
        self.executor().publish_due("unresolved-consequence", expected_version=1)
        document = json.loads(FIXTURE.read_text())
        document["rubric"]["version"] = "changed"
        self.planner = AdaptivePlanner(FrozenCase(document, fixture=True), self.run,
            bindings={"state": "concept-state", "result": "concept-result", "challenge": "concept-challenge"})
        with self.assertRaises(self.execution.ExternalPublicationError):
            self.executor().publish_due("unresolved-consequence", expected_version=1)
        self.assertEqual(self.client.writes, 1)
