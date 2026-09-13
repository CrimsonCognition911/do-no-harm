"""Live delegation coordinator tests; no provider connection is opened."""

from types import SimpleNamespace
from time import sleep
import unittest

from backend.examiner_bridge_service import ExaminerBridgeService
from backend.session_service import APIError, SessionService


OPERATOR = "synthetic-bridge-operator-token-32-characters"
CRITERIA = [
    "mi.initial_assessment", "mi.ecg_interpretation", "mi.reperfusion_escalation",
    "mi.initial_treatment_safety", "mi.reassessment", "mi.communication",
]


class FakeExaminer:
    def __init__(self, factory):
        self.factory = factory

    def evaluate(self, **kwargs):
        self.factory.calls.append(kwargs)
        if self.factory.error:
            raise RuntimeError("private provider detail")
        judgments = [{
            "criterion_id": criterion,
            "outcome": self.factory.outcome,
            "evidence_ids": ["observed-1"] if self.factory.outcome != "insufficient_evidence" else [],
            "rationale": "private model rationale",
            "uncertainty": "low",
            "requires_clinician_review": True,
        } for criterion in CRITERIA]
        return SimpleNamespace(evaluation={
            "run_id": kwargs["run_id"],
            "execution_version": kwargs["execution_version"],
            "status": "insufficient_evidence"
            if self.factory.outcome == "insufficient_evidence" else "evaluated",
            "judgments": judgments,
        })


class FakeFactory:
    def __init__(self, outcome="acceptable", *, error=False):
        self.outcome, self.error, self.calls, self.sources = outcome, error, [], []

    def __call__(self, source, *, agent_id):
        self.sources.append((source, agent_id))
        return FakeExaminer(self)


class ExaminerBridgeServiceTests(unittest.TestCase):
    def setUp(self):
        self.sessions = SessionService(OPERATOR)
        _, created = self.sessions.handle(
            "POST", [], {}, OPERATOR, {"request_id": "bridge-run", "mode": "coached"}
        )
        self.run_id, self.tokens = created["run_id"], created["tokens"]
        self.sessions.handle(
            "POST", [self.run_id, "commands"], {}, self.tokens["examiner"],
            {"request_id": "start", "command": "start", "execution_version": 1},
        )
        self.sessions.handle(
            "POST", [self.run_id, "actions"], {}, self.tokens["doctor"],
            {
                "event_id": "observed-1",
                "execution_version": 1,
                "payload": {"action": "review ECG", "source": "browser", "phase": "observed"},
            },
        )

    def body(self, **changes):
        value = {
            "type": "live_delegation",
            "run_id": self.run_id,
            "delegation_id": "live-delegation-1",
            "offset_ms": 100,
            "execution_version": 1,
            "transcript": [{
                "speaker": "doctor", "text": "I think this is a STEMI", "partial": False,
                "start_ms": 0, "end_ms": 90,
            }],
            "participant_event_ids": ["observed-1"],
        }
        value.update(changes)
        return value

    def bridge(self, factory):
        return ExaminerBridgeService(
            self.sessions,
            examiner_agent_id="agent-saved-1",
            examiner_factory=factory,
        )

    def test_supported_reasoning_gets_professor_question_without_private_output(self):
        factory = FakeFactory("acceptable")
        result = self.bridge(factory).handle(self.tokens["examiner"], self.body())

        self.assertEqual(set(result), {"status", "spoken_update", "evidence_ids"})
        self.assertEqual(result["status"], "complete")
        self.assertIn("What will you prioritize", result["spoken_update"])
        self.assertNotIn("private model rationale", str(result))
        self.assertEqual(result["evidence_ids"], ["observed-1"])
        self.assertEqual(factory.sources[0][1], "agent-saved-1")
        self.assertEqual(factory.calls[0]["criterion_ids"], CRITERIA)
        self.assertEqual(factory.calls[0]["participant_context"], self.body()["transcript"])
        events = self.sessions.handle(
            "GET", [self.run_id, "events"], {}, self.tokens["examiner"], None
        )[1]["events"]
        self.assertEqual(len([item for item in events if item["type"] == "evaluation_finding"]), 6)

    def test_insufficient_evidence_asks_for_clarification_without_pause(self):
        result = self.bridge(FakeFactory("insufficient_evidence")).handle(
            self.tokens["examiner"], self.body()
        )
        self.assertEqual(result["status"], "needs_clarification")
        self.assertIn("leading diagnosis", result["spoken_update"])
        snapshot = self.sessions.handle(
            "GET", [self.run_id], {}, self.tokens["examiner"], None
        )[1]
        self.assertEqual((snapshot["state"], snapshot["execution_version"]), ("running", 1))

    def test_concern_records_findings_then_requests_pause(self):
        factory = FakeFactory("concern")
        bridge = self.bridge(factory)
        result = bridge.handle(
            self.tokens["examiner"], self.body()
        )
        self.assertEqual(result["status"], "pause_requested")
        self.assertIn("Pause", result["spoken_update"])
        snapshot = self.sessions.handle(
            "GET", [self.run_id], {}, self.tokens["examiner"], None
        )[1]
        self.assertEqual((snapshot["state"], snapshot["execution_version"]),
                         ("pause_requested", 2))
        self.assertEqual(bridge.handle(self.tokens["examiner"], self.body()), result)
        self.assertEqual(len(factory.calls), 1)

    def test_provider_failure_is_unscored_and_requests_operational_pause(self):
        result = self.bridge(FakeFactory(error=True)).handle(
            self.tokens["examiner"], self.body()
        )
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("not a clinical penalty", result["spoken_update"])
        snapshot = self.sessions.handle(
            "GET", [self.run_id], {}, self.tokens["examiner"], None
        )[1]
        self.assertEqual(snapshot["state"], "pause_requested")
        events = self.sessions.handle(
            "GET", [self.run_id, "events"], {}, self.tokens["examiner"], None
        )[1]["events"]
        self.assertFalse(any(item["type"] == "evaluation_finding" for item in events))

    def test_authority_evidence_and_exact_retry_are_bound(self):
        factory = FakeFactory("acceptable")
        bridge = self.bridge(factory)
        first = bridge.handle(self.tokens["examiner"], self.body())
        self.assertEqual(bridge.handle(self.tokens["examiner"], self.body()), first)
        self.assertEqual(len(factory.calls), 1)
        with self.assertRaisesRegex(APIError, "forbidden"):
            bridge.handle(self.tokens["doctor"], self.body())
        with self.assertRaisesRegex(APIError, "delegation_id_conflict"):
            bridge.handle(self.tokens["examiner"], self.body(offset_ms=101))
        with self.assertRaises(APIError):
            bridge.handle(self.tokens["doctor"], self.body(delegation_id="wrong-role"))
        with self.assertRaisesRegex(APIError, "participant_evidence_mismatch"):
            bridge.handle(
                self.tokens["examiner"],
                self.body(delegation_id="invented", participant_event_ids=["not-real"]),
            )

        self.sessions.handle(
            "POST", [self.run_id, "commands"], {}, self.tokens["examiner"],
            {"request_id": "pause-after-cache", "command": "pause", "execution_version": 1},
        )
        with self.assertRaisesRegex(APIError, "stale_execution_version"):
            bridge.handle(self.tokens["examiner"], self.body())

    def test_provider_failure_does_not_claim_pause_when_pause_request_fails(self):
        bridge = self.bridge(FakeFactory(error=True))

        def fail_pause(*_args):
            raise APIError(409, "pause_failed")

        bridge._clinical_pause = fail_pause
        with self.assertRaisesRegex(APIError, "pause_failed"):
            bridge.handle(self.tokens["examiner"], self.body())

    def test_display_receipt_requires_current_published_participant_event(self):
        factory = FakeFactory("acceptable")
        bridge = self.bridge(factory)
        publication = {
            "schema_version": "0.1",
            "type": "clinical_update",
            "event_id": "published-1",
            "run_id": self.run_id,
            "actor": "simulation_service",
            "occurred_at": "2026-09-13T00:00:00Z",
            "simulation_time_ms": 1,
            "execution_version": 1,
            "evidence_ids": [],
            "visibility": "participant",
            "payload": {
                "scenario_event_id": "result-1",
                "summary": "Troponin available",
                "resource_ref": "observation-1",
                "delivery_stage": "published",
            },
        }
        run = self.sessions._runs[self.run_id]["run"]
        run._resources = frozenset({"observation-1"})
        run.record(
            event_id=publication["event_id"], expected_version=1,
            kind="clinical_update", producer="simulation_service",
            payload=publication["payload"], evidence_ids=[], visibility="participant",
        )
        body = {
            "type": "delivery_ack",
            "run_id": self.run_id,
            "event_id": "published-1",
            "execution_version": 1,
            "stage": "displayed",
        }
        self.assertEqual(
            bridge.handle_delivery(self.tokens["examiner"], body), {"accepted": True}
        )
        self.assertEqual(
            bridge.handle_delivery(self.tokens["examiner"], body), {"accepted": True}
        )
        with self.assertRaises(APIError):
            bridge.handle_delivery(
                self.tokens["examiner"], {**body, "event_id": "observed-1"}
            )

    def test_provider_deadline_pauses_without_late_scoring(self):
        class SlowExaminer(FakeExaminer):
            def evaluate(self, **kwargs):
                sleep(0.05)
                return super().evaluate(**kwargs)

        class SlowFactory(FakeFactory):
            def __call__(self, source, *, agent_id):
                self.sources.append((source, agent_id))
                return SlowExaminer(self)

        factory = SlowFactory("acceptable")
        bridge = ExaminerBridgeService(
            self.sessions,
            examiner_agent_id="agent-saved-1",
            examiner_factory=factory,
            provider_timeout=0.01,
        )
        result = bridge.handle(self.tokens["examiner"], self.body())
        self.assertEqual(result["status"], "unavailable")
        sleep(0.06)
        events = self.sessions.handle(
            "GET", [self.run_id, "events"], {}, self.tokens["examiner"], None
        )[1]["events"]
        self.assertFalse(any(item["type"] == "evaluation_finding" for item in events))


if __name__ == "__main__":
    unittest.main()
