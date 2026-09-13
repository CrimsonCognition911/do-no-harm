"""Non-clinical fixture tests for policy enforcement, never clinical validation."""
from copy import deepcopy
import importlib
import json
from pathlib import Path
import unittest

from backend.runtime import Run


FIXTURE = Path(__file__).resolve().parents[1] / "contracts" / "adaptive-fixture.json"


class AdaptationTests(unittest.TestCase):
    def setUp(self):
        try:
            self.module = importlib.import_module("backend.adaptation")
        except ModuleNotFoundError:
            self.fail("Frozen adaptive-policy planner is not implemented")
        self.assertTrue(FIXTURE.exists(), "Non-clinical adaptive fixture is missing")
        self.document = json.loads(FIXTURE.read_text())
        self.case = self.module.FrozenCase(self.document, fixture=True)
        self.now = 0.0
        self.run = Run("adaptive-1", resource_refs={"fixture-result", "fixture-state", "fixture-challenge"}, clock=lambda: self.now)
        self.run.start(expected_version=1)
        self.run.record(event_id="observed", expected_version=1, kind="doctor_action", producer="browser",
                        payload={"action": "inspect fixture", "source": "browser", "phase": "observed"},
                        evidence_ids=[], visibility="participant")
        self.planner = self.module.AdaptivePlanner(self.case, self.run,
            bindings={"result": "fixture-result", "state": "fixture-state", "challenge": "fixture-challenge"})

    def finding(self, outcome="acceptable", event_id="finding-1"):
        self.run.record(event_id=event_id, expected_version=self.run.execution_version, kind="evaluation_finding",
                        producer="examiner", payload={"criterion_id": "fixture-reassessment", "outcome": outcome,
                        "rationale": "Scripted non-clinical fixture finding", "requires_clinician_review": True},
                        evidence_ids=[] if outcome == "insufficient_evidence" else ["observed"], visibility="examiner")
        return event_id

    def propose(self, event_id="harder-challenge", finding_id="finding-1", **kwargs):
        args = dict(request_id="proposal-1", event_id=event_id, finding_id=finding_id,
                    at_ms=1000, difficulty=2, reason="Fixture branch selection", expected_version=self.run.execution_version)
        args.update(kwargs)
        return self.planner.propose(**args)

    def test_unreviewed_case_cannot_start_as_a_clinically_approved_case(self):
        with self.assertRaises(ValueError):
            self.module.FrozenCase(self.document)
        with self.assertRaises(ValueError):
            self.module.FrozenCase(self.document, approved_sha256="not-the-case-hash")
        compiled = self.module.FrozenCase(self.document, approved_sha256=self.case.case_hash)
        self.assertEqual(compiled.case_hash, self.case.case_hash)

    def test_case_and_rubric_are_frozen_against_caller_mutation(self):
        original_case, original_rubric = self.case.case_hash, self.case.rubric_hash
        self.document["rubric"]["criteria"][0]["description"] = "Changed answer key"
        exposed = self.case.document
        exposed["events"][0]["summary"] = "Changed event"
        self.assertEqual(self.case.case_hash, original_case)
        self.assertEqual(self.case.rubric_hash, original_rubric)
        self.assertNotEqual(self.case.document["events"][0]["summary"], "Changed event")

    def test_invalid_references_and_unsafe_policy_shapes_are_rejected(self):
        mutations = [
            lambda doc: doc["events"][0].update(requires={"unknown_state": True}),
            lambda doc: doc["events"][0].update(latest_ms=-1),
            lambda doc: doc["events"][0].update(kind="execute_python"),
            lambda doc: doc["events"].append(deepcopy(doc["events"][0])),
            lambda doc: doc["policy"].update(max_pending_challenges=True),
            lambda doc: doc["events"][-1].update(criterion_id="missing-criterion"),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                doc = deepcopy(self.document)
                mutate(doc)
                self.module.FrozenCase(doc, fixture=True)

    def test_good_and_struggling_paths_select_different_bounded_challenges(self):
        self.finding()
        good = self.propose()
        self.assertEqual(good["event_id"], "harder-challenge")
        self.assertEqual(good["difficulty"], 2)
        self.assertEqual(good.get("run_id"), "adaptive-1")
        other = self.module.AdaptivePlanner(self.case, self.run,
            bindings={"result": "fixture-result", "state": "fixture-state", "challenge": "fixture-challenge"})
        self.finding("concern", "concern-1")
        struggling = other.propose(request_id="struggling", event_id="paced-challenge", finding_id="concern-1",
                                  at_ms=2000, difficulty=1, reason="Reduce optional challenge load", expected_version=1)
        self.assertEqual(struggling["event_id"], "paced-challenge")
        self.assertEqual(struggling["rubric_hash"], good["rubric_hash"])
        self.assertEqual(struggling["policy_hash"], good["policy_hash"])

    def test_insufficient_or_wrong_evidence_cannot_increase_difficulty(self):
        self.finding("insufficient_evidence")
        for finding_id in ("finding-1", "observed", "another-run-finding"):
            with self.subTest(finding_id=finding_id), self.assertRaises(ValueError):
                self.propose(finding_id=finding_id)
        self.assertEqual(self.planner.audit(), [])

    def test_timing_difficulty_and_pending_limit_are_enforced(self):
        self.finding()
        for values in ({"at_ms": 999}, {"at_ms": 11000}, {"difficulty": 3}, {"difficulty": True}, {"expected_version": 0}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.propose(**values)
        result = self.propose()
        self.assertEqual(self.propose(), result)
        with self.assertRaises(ValueError):
            self.propose(at_ms=2000)
        self.finding("concern", "concern-1")
        with self.assertRaises(ValueError):
            self.propose("paced-challenge", "concern-1", request_id="second", difficulty=1)

    def test_due_consequence_is_not_postponed_by_optional_challenge(self):
        self.finding()
        self.propose(at_ms=8000)
        self.now = 6
        due = self.planner.due_events(expected_version=1)
        self.assertEqual([item["event_id"] for item in due], ["unresolved-consequence"])
        self.assertEqual(due[0]["scheduled_at_ms"], 5000)
        self.assertEqual(due[0]["status"], "planned_not_published")
        self.assertFalse(any(e["type"] == "clinical_update" for e in self.run.events()))

    def test_only_confirmed_actions_change_state_preconditions(self):
        payload = {"action": "resolve fixture problem", "source": "speech", "phase": "intent"}
        self.run.record(event_id="said-resolved", expected_version=1, kind="doctor_action", producer="speech",
                        payload=payload, evidence_ids=[], visibility="participant")
        self.now = 6
        self.assertEqual(len(self.planner.due_events(expected_version=1)), 1)
        payload.update(source="openmrs_backend", phase="confirmed", resource_ref="fixture-state")
        self.run.record(event_id="resolved", expected_version=1, kind="doctor_action", producer="openmrs_backend",
                        payload=payload, evidence_ids=[], visibility="participant")
        self.assertEqual(self.planner.due_events(expected_version=1), [])

    def test_pause_and_stale_proposals_do_not_advance_case(self):
        self.finding()
        self.propose()
        self.run.request_pause(expected_version=1)
        with self.assertRaises(ValueError):
            self.planner.due_events(expected_version=2)
        for component in ("execution", "audio"):
            self.run.acknowledge_pause(component, expected_version=2)
        self.run.request_resume(expected_version=2)
        for component in ("execution", "audio"):
            self.run.acknowledge_resume(component, expected_version=3)
        self.now = 2
        self.assertEqual(self.planner.due_events(expected_version=3), [])
        self.assertEqual(self.planner.audit()[-1]["status"], "invalidated_execution_version")
        with self.assertRaises(ValueError):
            self.propose(request_id="reused-old-finding", at_ms=3000)

    def test_duplicate_proposal_cannot_bypass_strict_version_validation(self):
        self.finding()
        self.propose()
        with self.assertRaises(ValueError):
            self.propose(expected_version=True)

    def test_matching_publication_receipt_prevents_replanning_event(self):
        self.now = 6
        plan = self.planner.due_events(expected_version=1)[0]
        self.run.record(event_id="published-1", expected_version=1, kind="clinical_update", producer="simulation_service",
                        payload={"scenario_event_id": plan["event_id"], "summary": plan["summary"],
                        "resource_ref": plan["resource_ref"], "delivery_stage": "published"}, evidence_ids=[], visibility="participant")
        self.assertEqual(self.planner.due_events(expected_version=1), [])

    def test_missing_resource_bindings_fail_closed(self):
        with self.assertRaises(ValueError):
            self.module.AdaptivePlanner(self.case, self.run, bindings={})
