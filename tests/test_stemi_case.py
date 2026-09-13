"""DNH-03 acceptance tests for the reviewed STEMI case."""
from copy import deepcopy
import importlib
import json
from pathlib import Path
import tempfile
import unittest

from backend.runtime import Run


ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "cases" / "stemi" / "case.yaml"
RUBRIC = ROOT / "cases" / "stemi" / "README.md"
REVIEW = ROOT / "cases" / "stemi" / "review.json"
COMPILED = ROOT / "cases" / "stemi" / "compiled.json"


class STEMICaseTests(unittest.TestCase):
    def compiler(self):
        try:
            return importlib.import_module("backend.case_compiler")
        except ModuleNotFoundError:
            self.fail("The reviewed STEMI case compiler is not implemented")

    def compile(self):
        return self.compiler().compile_reviewed_case(CASE, RUBRIC, REVIEW)

    def test_approved_sources_compile_deterministically_to_frozen_artifact(self):
        document = self.compile()
        self.assertEqual(document, json.loads(COMPILED.read_text()))
        case = self.compiler().load_reviewed_case(COMPILED, REVIEW)
        record = json.loads(REVIEW.read_text())
        self.assertEqual(case.case_hash, record["compiled_runtime_artifact_sha256"])
        self.assertFalse(record["runtime_enabled"])
        self.assertFalse(record["scored_use_allowed"])
        self.assertEqual(case.document["rubric"]["id"], "stemi-clinical-rubric")
        self.assertEqual(
            case.document["rubric"].get("outcomes"),
            ["acceptable", "concern", "insufficient_evidence"],
        )
        self.assertTrue(case.document["rubric"].get("requires_clinician_review"))
        self.assertFalse(case.document["rubric"].get("numeric_pass_fail_score_enabled", True))
        self.assertIn("alternatives", case.document["rubric"].get("alternatives_policy", "").lower())
        self.assertEqual(
            {criterion["id"] for criterion in case.document["rubric"]["criteria"]},
            {
                "mi.initial_assessment",
                "mi.ecg_interpretation",
                "mi.reperfusion_escalation",
                "mi.initial_treatment_safety",
                "mi.reassessment",
                "mi.communication",
            },
        )

    def test_changed_approved_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            changed = Path(directory) / "case.yaml"
            changed.write_bytes(CASE.read_bytes() + b"\n# changed after approval\n")
            with self.assertRaisesRegex(ValueError, "approved authoring hash"):
                self.compiler().compile_reviewed_case(changed, RUBRIC, REVIEW)

    def test_unbound_release_gates_remain_fail_closed(self):
        document = self.compile()
        self.assertEqual(document["format"], "dnh.compiled-case/0.2")
        self.assertNotIn("patient_uuid", json.dumps(document))
        record = json.loads(REVIEW.read_text())
        self.assertIn("ECG", " ".join(record["remaining_release_gates"]))
        self.assertIn("OpenMRS", " ".join(record["remaining_release_gates"]))

    def test_educational_challenges_cannot_mutate_clinical_state(self):
        adaptation = importlib.import_module("backend.adaptation")
        document = self.compile()
        optional = next(event for event in document["events"] if event["kind"] == "optional_challenge")
        optional["set_state"] = {"reperfusion_completed": True}
        with self.assertRaises(ValueError):
            adaptation.FrozenCase(document, fixture=True)

        document = self.compile()
        automatic = next(event for event in document["events"] if event["kind"] == "clinical_consequence")
        automatic["schedule"] = {"basis": "examiner_selection", "state_ref": None, "delay_ms": 0}
        with self.assertRaises(ValueError):
            adaptation.FrozenCase(document, fixture=True)

    def make_planner(self, run_id):
        case = self.compiler().load_reviewed_case(COMPILED, REVIEW)
        now = [0.0]
        refs = {"ecg", "troponin", "vitals", "transfer"}
        run = Run(run_id, resource_refs=refs, clock=lambda: now[0])
        run.start(expected_version=1)
        run.record(
            event_id=f"{run_id}-observed",
            expected_version=1,
            kind="doctor_action",
            producer="browser",
            payload={"action": "review patient", "phase": "observed", "source": "browser"},
            evidence_ids=[],
            visibility="participant",
        )
        planner = importlib.import_module("backend.adaptation").AdaptivePlanner(
            case,
            run,
            bindings={
                "diagnostic_ecg": "ecg",
                "troponin_result": "troponin",
                "reassessment_vitals": "vitals",
                "transfer_update": "transfer",
            },
        )
        return now, run, planner, case

    @staticmethod
    def confirm(run, event_id, action, resource_ref):
        return run.record(
            event_id=event_id,
            expected_version=1,
            kind="doctor_action",
            producer="openmrs_backend",
            payload={
                "action": action,
                "phase": "confirmed",
                "source": "openmrs_backend",
                "resource_ref": resource_ref,
            },
            evidence_ids=[],
            visibility="participant",
        )

    @staticmethod
    def publish(run, event_id, plan):
        run.record(
            event_id=event_id,
            expected_version=1,
            kind="clinical_update",
            producer="simulation_service",
            payload={
                "scenario_event_id": plan["event_id"],
                "summary": plan["summary"],
                "resource_ref": plan["resource_ref"],
                "delivery_stage": "published",
            },
            evidence_ids=[],
            visibility="participant",
        )

    def release_ecg(self, run, planner):
        self.confirm(run, "ecg-acquired", "confirm_ecg_acquisition", "ecg")
        plan = next(item for item in planner.due_events(expected_version=1) if item["event_id"] == "diagnostic_ecg")
        self.publish(run, "ecg-published", plan)

    def finding(self, run, run_id, outcome):
        return run.record(
            event_id=f"{run_id}-finding",
            expected_version=1,
            kind="evaluation_finding",
            producer="examiner",
            payload={
                "criterion_id": "mi.reperfusion_escalation",
                "outcome": outcome,
                "rationale": "Evidence-linked fixture judgment for path verification",
                "requires_clinician_review": True,
            },
            evidence_ids=[f"{run_id}-observed"],
            visibility="examiner",
        )

    def test_good_and_struggling_paths_keep_one_answer_key(self):
        good_now, good_run, good_planner, good_case = self.make_planner("good")
        self.release_ecg(good_run, good_planner)
        self.confirm(good_run, "pathway-active", "confirm_reperfusion_pathway_activation", "transfer")
        self.finding(good_run, "good", "acceptable")
        good_now[0] = 120.0
        challenge = good_planner.propose(
            request_id="good-path",
            event_id="transfer_logistics_challenge",
            finding_id="good-finding",
            at_ms=120000,
            difficulty=2,
            reason="Reviewed good-management branch",
            expected_version=1,
        )

        struggling_now, struggling_run, struggling_planner, struggling_case = self.make_planner("struggling")
        self.release_ecg(struggling_run, struggling_planner)
        self.finding(struggling_run, "struggling", "concern")
        struggling_now[0] = 360.0
        with self.assertRaises(ValueError):
            struggling_planner.propose(
                request_id="unsupported-harder-path",
                event_id="transfer_logistics_challenge",
                finding_id="struggling-finding",
                at_ms=120000,
                difficulty=2,
                reason="Concern must not increase difficulty",
                expected_version=1,
            )
        due = struggling_planner.due_events(expected_version=1)

        self.assertEqual(challenge["event_id"], "transfer_logistics_challenge")
        self.assertIn("persistent_ischaemia_with_hypotension", {item["event_id"] for item in due})
        self.assertEqual(good_case.rubric_hash, struggling_case.rubric_hash)
        self.assertEqual(good_case.policy_hash, struggling_case.policy_hash)

    def test_relative_result_delay_starts_from_confirmed_collection(self):
        now, run, planner, _ = self.make_planner("relative")
        now[0] = 60.0
        self.confirm(run, "blood-collected", "confirm_blood_sample_collection", "troponin")
        now[0] = 359.999
        self.assertNotIn("troponin_result", {item["event_id"] for item in planner.due_events(expected_version=1)})
        now[0] = 360.0
        result = next(item for item in planner.due_events(expected_version=1) if item["event_id"] == "troponin_result")
        self.assertEqual(result["scheduled_at_ms"], 360000)


if __name__ == "__main__":
    unittest.main()
