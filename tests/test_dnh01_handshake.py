"""DNH-01 acceptance: shared samples, rejected confusion, no client leaks, voice handshake."""
import json
from pathlib import Path
import unittest

from backend.contracts import (
    project_permitted_voice_update,
    validate_event,
    validate_voice_update,
)
from backend.runtime import Run


ROOT = Path(__file__).resolve().parents[1]
HANDSHAKE = json.loads((ROOT / "contracts" / "samples" / "handshake.json").read_text())
PRODUCERS = {
    "doctor_action": "browser",
    "clinical_update": "simulation_service",
    "evaluation_finding": "examiner",
    "session_state": "simulation_service",
}
SECRET_MARKERS = ("sk-", "api_key", "OPENAI", "Bearer ", "DNH_OPERATOR_TOKEN")
HIDDEN_MARKERS = ("criterion_id", "rationale", "requires_clinician_review", "authored_not_published", "troponin_result")


class DNH01HandshakeTests(unittest.TestCase):
    def test_sample_messages_use_the_frozen_field_names(self):
        self.assertEqual(HANDSHAKE["contract_version"], "0.1")
        self.assertEqual(HANDSHAKE["status"], "frozen")
        for kind, event in HANDSHAKE["events"].items():
            with self.subTest(kind=kind):
                self.assertEqual(validate_event(event, producer=PRODUCERS[kind]), event)
        for event in HANDSHAKE["participant_feed"]:
            self.assertEqual(event["visibility"], "participant")
            self.assertNotEqual(event["type"], "evaluation_finding")
            validate_event(event, producer=PRODUCERS[event["type"]])
        for update in HANDSHAKE["permitted_voice_updates"]:
            self.assertEqual(validate_voice_update(update), update)

    def test_invalid_state_transitions_are_rejected(self):
        run = Run("run-1", resource_refs=set())
        with self.assertRaises(ValueError):
            run.request_pause(expected_version=1)
        run.start(expected_version=1)
        with self.assertRaises(ValueError):
            run.start(expected_version=1)
        with self.assertRaises(ValueError):
            run.begin_coaching(expected_version=1)
        with self.assertRaises(ValueError):
            run.request_resume(expected_version=1)

    def test_client_facing_fixtures_omit_secrets_hidden_rubric_and_unreleased_events(self):
        public = json.dumps({
            "participant_feed": HANDSHAKE["participant_feed"],
            "permitted_voice_updates": HANDSHAKE["permitted_voice_updates"],
            "handshake": HANDSHAKE["handshake"],
        })
        for marker in SECRET_MARKERS + HIDDEN_MARKERS:
            self.assertNotIn(marker, public)
        self.assertNotIn("evaluation_finding", json.dumps(HANDSHAKE["participant_feed"]))
        self.assertEqual(HANDSHAKE["unreleased_events"][0]["delivery_stage"], "authored_not_published")

    def test_action_to_evidence_to_permitted_voice_update(self):
        published = HANDSHAKE["events"]["clinical_update"]
        finding = HANDSHAKE["events"]["evaluation_finding"]
        update = project_permitted_voice_update(published)
        self.assertEqual(update["say"], published["payload"]["summary"])
        self.assertEqual(update["source_event_id"], published["event_id"])
        with self.assertRaises(ValueError):
            project_permitted_voice_update(finding)
        with self.assertRaises(ValueError):
            project_permitted_voice_update(HANDSHAKE["events"]["doctor_action"])
        unpublished = dict(published)
        unpublished["payload"] = {**published["payload"], "delivery_stage": "spoken"}
        with self.assertRaises(ValueError):
            project_permitted_voice_update(unpublished)
        self.assertEqual(
            project_permitted_voice_update(HANDSHAKE["events"]["session_state"])["say"],
            "Simulation paused. Wait for examiner instructions.",
        )


if __name__ == "__main__":
    unittest.main()
