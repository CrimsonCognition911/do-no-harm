"""Boundary tests for the application event contract, not provider APIs."""
import importlib
import unittest


def action_event():
    return {
        "schema_version": "0.1", "type": "doctor_action", "event_id": "action-1",
        "run_id": "run-1", "actor": "doctor", "occurred_at": "2026-09-13T03:00:00Z",
        "simulation_time_ms": 0, "execution_version": 1, "evidence_ids": [],
        "visibility": "participant",
        "payload": {"action": "request labs", "phase": "observed", "source": "browser"},
    }


class ContractTests(unittest.TestCase):
    def setUp(self):
        try:
            self.contracts = importlib.import_module("backend.contracts")
        except ModuleNotFoundError:
            self.fail("Event boundary validator is not implemented")

    def validate(self, event, producer="browser"):
        return self.contracts.validate_event(event, producer=producer)

    def test_valid_event_is_returned_as_an_independent_snapshot(self):
        event = action_event()
        snapshot = self.validate(event)
        event["payload"]["action"] = "changed"
        self.assertEqual(snapshot["payload"]["action"], "request labs")

    def test_confirmed_action_requires_backend_source_and_resource(self):
        event = action_event()
        event["payload"]["phase"] = "confirmed"
        with self.assertRaises(ValueError):
            self.validate(event)
        event["payload"]["source"] = "openmrs_backend"
        with self.assertRaises(ValueError):
            self.validate(event, "openmrs_backend")
        event["payload"]["resource_ref"] = "synthetic-order-1"
        self.assertEqual(self.validate(event, "openmrs_backend"), event)

    def test_browser_cannot_claim_backend_authority_even_with_valid_shape(self):
        event = action_event()
        event["payload"].update(phase="confirmed", source="openmrs_backend", resource_ref="order-1")
        with self.assertRaises(ValueError):
            self.validate(event, "browser")

    def test_unknown_producer_is_rejected(self):
        with self.assertRaises(ValueError):
            self.validate(action_event(), "anonymous")

    def test_missing_run_id_is_rejected(self):
        event = action_event()
        del event["run_id"]
        with self.assertRaises(ValueError):
            self.validate(event)

    def test_actor_type_confusion_is_rejected(self):
        mismatches = [
            ("doctor_action", "examiner", {"action": "x", "phase": "observed", "source": "browser"}, "browser"),
            ("clinical_update", "doctor", {"scenario_event_id": "lab-1", "summary": "Result", "resource_ref": "obs-1", "delivery_stage": "published"}, "simulation_service"),
            ("evaluation_finding", "doctor", {"criterion_id": "c", "outcome": "concern", "rationale": "x", "requires_clinician_review": True}, "examiner"),
            ("session_state", "examiner", {"state": "paused", "mode": "coached", "assisted": False}, "simulation_service"),
        ]
        for kind, actor, payload, producer in mismatches:
            with self.subTest(kind=kind, actor=actor), self.assertRaises(ValueError):
                event = action_event()
                event.update(type=kind, actor=actor, payload=payload)
                if kind == "evaluation_finding":
                    event["visibility"] = "examiner"
                self.validate(event, producer)

    def test_bad_envelopes_are_rejected(self):
        changes = [
            {"extra": 1}, {"schema_version": "0.2"}, {"actor": "examiner"},
            {"run_id": ""}, {"execution_version": True}, {"simulation_time_ms": -1},
            {"simulation_time_ms": 0.5}, {"evidence_ids": ["x", "x"]},
            {"occurred_at": "2026-09-13"}, {"occurred_at": "2026-09-13T03:00:00"},
            {"occurred_at": "2026-09-13T03:00:00+00:60"},
            {"occurred_at": "2026-02-30T00:00:00Z"}, {"visibility": "public"},
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                event = action_event()
                event.update(change)
                self.validate(event)

    def test_missing_and_unknown_payload_fields_are_rejected(self):
        for payload in [{"action": "x"}, {"action": "x", "phase": "intent", "source": "browser", "extra": 1}]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                event = action_event()
                event["payload"] = payload
                self.validate(event)

    def test_findings_are_examiner_only_and_require_boolean_review_flag(self):
        event = action_event()
        event.update(type="evaluation_finding", actor="examiner", visibility="examiner")
        event["payload"] = {"criterion_id": "review-labs", "outcome": "concern", "rationale": "Provisional", "requires_clinician_review": True}
        self.assertEqual(self.validate(event, "examiner"), event)
        event["visibility"] = "participant"
        with self.assertRaises(ValueError):
            self.validate(event, "examiner")
        event["visibility"] = "examiner"
        event["payload"]["requires_clinician_review"] = 1
        with self.assertRaises(ValueError):
            self.validate(event, "examiner")

    def test_simulation_types_require_simulation_producer(self):
        payloads = {
            "clinical_update": {"scenario_event_id": "lab-1", "summary": "Synthetic result available", "resource_ref": "obs-1", "delivery_stage": "published"},
            "session_state": {"state": "paused", "mode": "coached", "assisted": False},
        }
        for kind, payload in payloads.items():
            with self.subTest(kind=kind):
                event = action_event()
                event.update(type=kind, actor="simulation_service", payload=payload)
                self.assertEqual(self.validate(event, "simulation_service"), event)
                with self.assertRaises(ValueError):
                    self.validate(event, "browser")
