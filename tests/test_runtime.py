import importlib
import unittest
from concurrent.futures import ThreadPoolExecutor


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        try:
            self.runtime = importlib.import_module("backend.runtime")
        except ModuleNotFoundError:
            self.fail("Run controller is not implemented")
        self.clock = Clock()
        self.run = self.runtime.Run("run-1", resource_refs={"order-1", "obs-1"}, clock=self.clock)

    def record(self, event_id="action-1", version=None, **changes):
        args = dict(event_id=event_id, expected_version=self.run.execution_version if version is None else version,
                    kind="doctor_action", producer="browser", visibility="participant", evidence_ids=[],
                    payload={"action": "request labs", "phase": "observed", "source": "browser"})
        args.update(changes)
        return self.run.record(**args)

    def pause(self):
        self.run.request_pause(expected_version=self.run.execution_version)
        version = self.run.execution_version
        self.run.acknowledge_pause("execution", expected_version=version)
        self.run.acknowledge_pause("audio", expected_version=version)

    def test_start_is_required_and_transitions_are_recorded(self):
        with self.assertRaises(ValueError):
            self.record()
        self.run.start(expected_version=1)
        event = self.record()
        self.assertEqual(event["run_id"], "run-1")
        self.assertEqual(event["actor"], "doctor")
        states = [e["payload"]["state"] for e in self.run.events() if e["type"] == "session_state"]
        self.assertEqual(states, ["created", "running"])
        with self.assertRaises(ValueError):
            self.run.start(expected_version=1)

    def test_resource_reference_must_belong_to_this_run(self):
        self.run.start(expected_version=1)
        payload = {"action": "order labs", "phase": "confirmed", "source": "openmrs_backend", "resource_ref": "other-patient-order"}
        with self.assertRaises(ValueError):
            self.record(producer="openmrs_backend", payload=payload)
        payload["resource_ref"] = "order-1"
        self.assertEqual(self.record(producer="openmrs_backend", payload=payload)["payload"], payload)

    def test_client_cannot_write_session_state(self):
        self.run.start(expected_version=1)
        with self.assertRaises(ValueError):
            self.record(kind="session_state", producer="simulation_service", payload={"state": "paused", "mode": "coached", "assisted": False})

    def test_duplicate_delivery_is_idempotent_but_conflicts_are_rejected(self):
        self.run.start(expected_version=1)
        original = self.record()
        self.clock.now = 10
        self.assertEqual(original, self.record())
        self.assertEqual(len(self.run.events()), 3)
        with self.assertRaises(ValueError):
            self.record(payload={"action": "different", "phase": "observed", "source": "browser"})

    def test_concurrent_duplicate_delivery_only_appends_once(self):
        self.run.start(expected_version=1)
        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(lambda _: self.record(), range(12)))
        self.assertTrue(all(event == results[0] for event in results))
        self.assertEqual(len(self.run.events()), 3)

    def test_retry_after_pause_returns_original_receipt_without_new_evidence(self):
        self.run.start(expected_version=1)
        original = self.record(version=1)
        self.pause()
        count = len(self.run.events())
        self.assertEqual(self.record(version=1), original)
        self.assertEqual(len(self.run.events()), count)
        with self.assertRaises(ValueError):
            self.record("new-event", version=1)

    def test_snapshots_cannot_mutate_evidence(self):
        self.run.start(expected_version=1)
        result = self.record()
        result["payload"]["action"] = "tampered"
        history = self.run.events()
        history[-1]["payload"]["action"] = "tampered again"
        self.assertEqual(self.run.events()[-1]["payload"]["action"], "request labs")

    def test_unknown_evidence_and_unsupported_findings_are_rejected(self):
        self.run.start(expected_version=1)
        finding = {"criterion_id": "check-1", "outcome": "concern", "rationale": "Review this decision", "requires_clinician_review": True}
        for refs in ([], ["other-run-event"]):
            with self.subTest(refs=refs), self.assertRaises(ValueError):
                self.record(kind="evaluation_finding", producer="examiner", visibility="examiner", payload=finding, evidence_ids=refs)
        self.record()
        event = self.record("finding-1", kind="evaluation_finding", producer="examiner", visibility="examiner", payload=finding, evidence_ids=["action-1"])
        self.assertEqual(event["evidence_ids"], ["action-1"])
        finding["requires_clinician_review"] = False
        with self.assertRaises(ValueError):
            self.record("finding-2", kind="evaluation_finding", producer="examiner", visibility="examiner", payload=finding, evidence_ids=["action-1"])

    def test_participant_feed_excludes_hidden_findings_and_their_references(self):
        self.run.start(expected_version=1)
        self.record()
        finding = {"criterion_id": "hidden-check", "outcome": "concern", "rationale": "Hidden rubric", "requires_clinician_review": True}
        self.record("finding-1", kind="evaluation_finding", producer="examiner", visibility="examiner", payload=finding, evidence_ids=["action-1"])
        self.assertEqual(len(self.run.events(audience="participant")), 3)
        self.assertEqual(len(self.run.events()), 4)
        with self.assertRaises(ValueError):
            self.record("action-2", evidence_ids=["finding-1"])
        with self.assertRaises(ValueError):
            self.run.events(audience="anonymous")

    def test_pause_freezes_clock_and_requires_both_current_acknowledgments(self):
        self.run.start(expected_version=1)
        self.clock.now = 4
        self.run.request_pause(expected_version=1)
        self.assertEqual(self.run.state, "pause_requested")
        self.assertEqual(self.run.execution_version, 2)
        self.clock.now = 100
        self.assertEqual(self.run.simulation_time_ms, 4000)
        self.assertFalse(self.run.review_allowed)
        with self.assertRaises(ValueError):
            self.run.begin_coaching(expected_version=2)
        with self.assertRaises(ValueError):
            self.record("late-action", version=1)
        with self.assertRaises(ValueError):
            self.record("new-action", version=2)
        with self.assertRaises(ValueError):
            self.run.acknowledge_pause("audio", expected_version=1)
        with self.assertRaises(ValueError):
            self.run.acknowledge_pause("browser", expected_version=2)
        self.run.acknowledge_pause("audio", expected_version=2)
        self.run.acknowledge_pause("audio", expected_version=2)
        self.assertEqual(self.run.state, "pause_requested")
        self.run.acknowledge_pause("execution", expected_version=2)
        self.assertEqual(self.run.state, "paused")
        self.assertFalse(self.run.review_allowed)
        self.run.begin_coaching(expected_version=2)
        self.assertTrue(self.run.review_allowed)

    def test_coached_resume_rejects_stale_work_and_excludes_paused_time(self):
        self.run.start(expected_version=1)
        self.clock.now = 3
        self.pause()
        self.run.begin_coaching(expected_version=2)
        self.assertTrue(self.run.assisted)
        self.assertEqual(self.run.state, "coaching")
        self.clock.now = 50
        self.run.request_resume(expected_version=2)
        self.assertFalse(self.run.review_allowed)
        self.run.acknowledge_resume("execution", expected_version=3)
        self.assertEqual(self.run.state, "resume_requested")
        self.run.acknowledge_resume("audio", expected_version=3)
        self.clock.now = 52
        self.assertEqual(self.run.simulation_time_ms, 5000)
        with self.assertRaises(ValueError):
            self.record("stale", version=1)
        self.assertEqual(self.record("fresh", version=3)["simulation_time_ms"], 5000)

    def test_assessment_coaching_ends_unassisted_portion(self):
        run = self.runtime.Run("assessment-1", resource_refs=set(), mode="assessment", clock=self.clock)
        run.start(expected_version=1)
        run.request_pause(expected_version=1)
        for component in ("audio", "execution"):
            run.acknowledge_pause(component, expected_version=2)
        self.assertFalse(run.review_allowed)
        run.begin_coaching(expected_version=2)
        self.assertEqual(run.state, "debrief")
        self.assertTrue(run.assisted)
        self.assertTrue(run.review_allowed)
        with self.assertRaises(ValueError):
            run.request_resume(expected_version=2)
        run.end(expected_version=2)
        self.assertEqual(run.state, "ended")
        self.assertFalse(run.review_allowed)

    def test_resume_and_end_cannot_skip_pause_barrier(self):
        self.run.start(expected_version=1)
        for method in (self.run.request_resume, self.run.end):
            with self.assertRaises(ValueError):
                method(expected_version=1)

    def test_delivery_acknowledgment_requires_matching_publication(self):
        self.run.start(expected_version=1)
        payload = {"scenario_event_id": "lab-1", "summary": "Synthetic result", "resource_ref": "obs-1", "delivery_stage": "spoken"}
        with self.assertRaises(ValueError):
            self.record("spoken", kind="clinical_update", producer="simulation_service", payload=payload)
        payload["delivery_stage"] = "published"
        self.record("published", kind="clinical_update", producer="simulation_service", payload=payload)
        payload["delivery_stage"] = "spoken"
        self.assertEqual(self.record("spoken", kind="clinical_update", producer="simulation_service", payload=payload)["payload"]["delivery_stage"], "spoken")
        payload["summary"] = "Different result"
        with self.assertRaises(ValueError):
            self.record("mismatch", kind="clinical_update", producer="simulation_service", payload=payload)

    def test_versions_are_strict_integers(self):
        for value in (True, 1.0, "1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.run.start(expected_version=value)
