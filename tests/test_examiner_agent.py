"""Provider-boundary tests. Fake streams are not proof of Agents API access."""

from copy import deepcopy
import json
from types import SimpleNamespace
import unittest

from backend.session_service import SessionService


OPERATOR = "synthetic-test-operator-token-32-characters"


def event(kind, **values):
    return SimpleNamespace(type=kind, event_id=values.pop("event_id", kind), **values)


class FakeStream:
    def __init__(self, events, tool_handlers, tool_arguments):
        self.events = events
        self.tool_handlers = tool_handlers
        self.tool_arguments = tool_arguments
        self.tool_result = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def __iter__(self):
        if self.tool_arguments is not None:
            self.tool_result = self.tool_handlers["get_evidence"](self.tool_arguments)
        return iter(self.events)


class FakeSessions:
    def __init__(self, events, *, tool_arguments=None, deleted=True, agent_changes=None):
        self.events = events
        self.tool_arguments = tool_arguments
        self.deleted = deleted
        self.create_call = None
        self.stream_call = None
        self.delete_call = None
        self.last_stream = None
        self.agent_changes = agent_changes or {}

    def create(self, **kwargs):
        self.create_call = kwargs
        from backend import examiner_agent
        agent = {
            "id": kwargs.get("agent_id"),
            "name": examiner_agent.AGENT_NAME,
            "model": examiner_agent.MODEL,
            "instructions": examiner_agent.INSTRUCTIONS,
            "reasoning": {"effort": "high"},
            "multi_agent": {"enabled": False},
            "service_tier": "auto",
            "text": {
                "format": {
                    "type": "json_schema",
                    "schema": deepcopy(examiner_agent.EVALUATION_SCHEMA),
                },
                "verbosity": "medium",
            },
            "tools": [{
                **deepcopy(examiner_agent.GET_EVIDENCE_TOOL),
                "defer_loading": False,
            }],
        }
        agent.update(deepcopy(self.agent_changes))
        return SimpleNamespace(
            id="sess-1", status="idle", environment=SimpleNamespace(id="env-1"),
            agent=agent,
        )

    def stream(self, session_id, **kwargs):
        self.stream_call = {"session_id": session_id, **kwargs}
        self.last_stream = FakeStream(self.events, kwargs["tool_handlers"], self.tool_arguments)
        return self.last_stream

    def delete(self, session_id):
        self.delete_call = session_id
        return SimpleNamespace(deleted=self.deleted)


class FakeAgents:
    def __init__(self, sessions):
        self.sessions = sessions
        self.create_call = None

    def create(self, **kwargs):
        self.create_call = kwargs
        return SimpleNamespace(id="agent-saved-1", model=kwargs["model"], name=kwargs["name"])


class FakeClient:
    def __init__(self, sessions):
        self.beta = SimpleNamespace(agents=FakeAgents(sessions))


def completed_events(output):
    text = json.dumps(output)
    return [
        event("agent.session.turn.created", session_id="sess-1", turn_id="turn-1",
              turn=SimpleNamespace(subagent_id=None)),
        event("agent.session.turn.output_text.done", session_id="sess-1", turn_id="turn-1",
              item_id="message-final", output_index=0, content_index=0, text=text),
        event("agent.session.turn.item.done", session_id="sess-1", turn_id="turn-1",
              output_index=0, item=SimpleNamespace(
                  id="message-final", type="message", role="assistant", status="completed",
                  phase="final_answer",
                  content=[SimpleNamespace(type="output_text", text=text)],
              )),
        event("agent.session.turn.completed", session_id="sess-1", turn_id="turn-1",
              turn=SimpleNamespace(status="completed")),
        event("agent.session.idle", session_id="sess-1"),
    ]


class ExaminerAgentTests(unittest.TestCase):
    def setUp(self):
        try:
            from backend import examiner_agent
        except ImportError:
            self.fail("Hosted Astra examiner adapter is not implemented")
        self.module = examiner_agent
        self.service = SessionService(OPERATOR)
        _, created = self.service.handle("POST", [], {}, OPERATOR,
                                         {"request_id": "create-1", "mode": "coached"})
        self.run_id = created["run_id"]
        self.tokens = created["tokens"]
        self.service.handle("POST", [self.run_id, "commands"], {}, self.tokens["examiner"],
                            {"request_id": "start-1", "command": "start", "execution_version": 1})
        self.service.handle("POST", [self.run_id, "actions"], {}, self.tokens["doctor"], {
            "event_id": "observed-1",
            "execution_version": 1,
            "payload": {"action": "inspect ECG tab", "source": "browser", "phase": "observed"},
        })

    def source(self, token=None):
        return self.module.SessionEvidenceSource(
            self.service, run_id=self.run_id, examiner_token=token or self.tokens["examiner"]
        )

    def output(self, **changes):
        result = {
            "run_id": self.run_id,
            "execution_version": 1,
            "status": "evaluated",
            "judgments": [{
                "criterion_id": "fixture-criterion",
                "outcome": "acceptable",
                "evidence_ids": ["observed-1"],
                "rationale": "The recorded browser action supports this fixture judgment.",
                "uncertainty": "low",
                "requires_clinician_review": True,
            }],
        }
        result.update(changes)
        return result

    def examiner(self, output=None, *, tool_arguments="default", events=None):
        sessions = FakeSessions(
            events if events is not None else completed_events(output or self.output()),
            tool_arguments={"execution_version": 1, "evidence_ids": ["observed-1"]}
            if tool_arguments == "default" else tool_arguments,
        )
        adapter = self.module.HostedAstraExaminer(
            FakeClient(sessions), self.source(), agent_id="agent-saved-1"
        )
        return adapter, sessions

    def test_creates_reusable_platform_agent_and_uses_saved_agent_id(self):
        sessions = FakeSessions(completed_events(self.output()))
        client = FakeClient(sessions)

        agent_id = self.module.create_saved_examiner_agent(client)

        self.assertEqual(agent_id, "agent-saved-1")
        create = client.beta.agents.create_call
        self.assertEqual(create["name"], "DO NO HARM Examiner")
        self.assertEqual(create["model"], "gpt-6-astra")
        self.assertEqual(create["tools"], [self.module.GET_EVIDENCE_TOOL])
        self.assertEqual(create["text"]["format"]["type"], "json_schema")
        self.assertEqual(create["metadata"]["dnh_component"], "examiner")

        adapter = self.module.HostedAstraExaminer.from_environment(
            client,
            self.source(),
            environ={"DNH_EXAMINER_AGENT_ID": agent_id},
        )
        self.assertEqual(adapter._agent_id, agent_id)
        with self.assertRaises(self.module.ExaminerProtocolError):
            self.module.HostedAstraExaminer.from_environment(
                client, self.source(), environ={}
            )

    def test_binds_run_to_hosted_astra_and_returns_grounded_result(self):
        adapter, sessions = self.examiner()

        result = adapter.evaluate(
            run_id=self.run_id,
            execution_version=1,
            request_id="evaluation-1",
            criterion_ids=["fixture-criterion"],
        )

        self.assertEqual(result.session_id, "sess-1")
        self.assertEqual(result.turn_id, "turn-1")
        self.assertEqual(result.environment_id, "env-1")
        self.assertEqual(result.successful_tool_calls, 1)
        self.assertEqual(result.evaluation, self.output())
        create = sessions.create_call
        self.assertEqual(create["environment"], {"type": "openai_hosted"})
        self.assertEqual(create["agent_id"], "agent-saved-1")
        self.assertNotIn("agent", create)
        self.assertEqual(create["metadata"], {"dnh_run_id": self.run_id})
        self.assertNotIn("input", create)
        self.assertEqual(sessions.stream_call["idempotency_key"], "evaluation-1")
        self.assertEqual(sessions.delete_call, "sess-1")
        self.assertEqual(sessions.last_stream.tool_result["run_id"], self.run_id)
        self.assertEqual(
            [item["event_id"] for item in sessions.last_stream.tool_result["events"]],
            ["observed-1"],
        )

    def test_rejects_mutated_saved_agent_snapshot_before_streaming_evidence(self):
        mutations = [
            {"id": "agent-other"},
            {"name": "Other agent"},
            {"model": "other-model"},
            {"instructions": "changed"},
            {"reasoning": {"effort": "low"}},
            {"multi_agent": {"enabled": True}},
            {"service_tier": "flex"},
            {"text": {"format": {"type": "text"}, "verbosity": "medium"}},
            {"tools": []},
            {"tools": [{**deepcopy(self.module.GET_EVIDENCE_TOOL),
                         "defer_loading": False},
                       {"type": "web_search"}]},
        ]
        for index, mutation in enumerate(mutations):
            sessions = FakeSessions(
                completed_events(self.output()),
                tool_arguments={"execution_version": 1, "evidence_ids": ["observed-1"]},
                agent_changes=mutation,
            )
            adapter = self.module.HostedAstraExaminer(
                FakeClient(sessions), self.source(), agent_id="agent-saved-1"
            )
            with self.subTest(mutation=mutation), self.assertRaises(
                self.module.ExaminerProtocolError
            ):
                adapter.evaluate(
                    run_id=self.run_id,
                    execution_version=1,
                    request_id=f"evaluation-mutated-agent-{index}",
                    criterion_ids=["fixture-criterion"],
                )
            self.assertIsNone(sessions.stream_call)
            self.assertEqual(sessions.delete_call, "sess-1")

    def test_provider_schemas_use_the_agents_api_supported_subset(self):
        schemas = json.dumps([
            self.module.GET_EVIDENCE_TOOL["parameters"],
            self.module.EVALUATION_SCHEMA,
        ])
        self.assertNotIn('"uniqueItems"', schemas)

    def test_evidence_source_requires_examiner_capability_and_current_version(self):
        with self.assertRaises(self.module.ExaminerBoundaryError):
            self.module.SessionEvidenceSource(
                self.service, run_id=self.run_id, examiner_token=self.tokens["doctor"]
            ).read(execution_version=1, evidence_ids=["observed-1"])
        with self.assertRaises(self.module.ExaminerBoundaryError):
            self.source().read(execution_version=2, evidence_ids=["observed-1"])
        with self.assertRaises(self.module.ExaminerBoundaryError):
            self.source().read(execution_version=1, evidence_ids=["unknown"])
        with self.assertRaises(self.module.ExaminerBoundaryError):
            self.source().read(execution_version=1, evidence_ids=["observed-1", "observed-1"])
        selected = self.source().read(execution_version=1, evidence_ids=["observed-1"])
        self.assertEqual([item["event_id"] for item in selected["events"]], ["observed-1"])

    def test_rejects_invented_evidence_and_non_provisional_judgment(self):
        bad = self.output()
        bad["judgments"][0]["evidence_ids"] = ["invented"]
        adapter, _ = self.examiner(bad)
        with self.assertRaises(self.module.ExaminerProtocolError):
            adapter.evaluate(run_id=self.run_id, execution_version=1,
                             request_id="evaluation-2", criterion_ids=["fixture-criterion"])

        bad = self.output()
        bad["judgments"][0]["requires_clinician_review"] = False
        adapter, _ = self.examiner(bad)
        with self.assertRaises(self.module.ExaminerProtocolError):
            adapter.evaluate(run_id=self.run_id, execution_version=1,
                             request_id="evaluation-3", criterion_ids=["fixture-criterion"])

    def test_insufficient_evidence_can_cite_the_records_that_were_examined(self):
        result = self.output(status="insufficient_evidence")
        result["judgments"][0].update({
            "outcome": "insufficient_evidence",
            "uncertainty": "high",
        })
        adapter, _ = self.examiner(result)

        evaluated = adapter.evaluate(
            run_id=self.run_id,
            execution_version=1,
            request_id="evaluation-insufficient",
            criterion_ids=["fixture-criterion"],
        )

        self.assertEqual(evaluated.evaluation["judgments"][0]["evidence_ids"], ["observed-1"])

    def test_requires_every_criterion_and_consistent_overall_status(self):
        adapter, _ = self.examiner()
        with self.assertRaises(self.module.ExaminerProtocolError):
            adapter.evaluate(
                run_id=self.run_id,
                execution_version=1,
                request_id="evaluation-missing-criterion",
                criterion_ids=["fixture-criterion", "omitted-criterion"],
            )

        result = self.output(status="evaluated")
        result["judgments"][0]["outcome"] = "insufficient_evidence"
        result["judgments"][0]["evidence_ids"] = []
        adapter, _ = self.examiner(result)
        with self.assertRaises(self.module.ExaminerProtocolError):
            adapter.evaluate(
                run_id=self.run_id,
                execution_version=1,
                request_id="evaluation-inconsistent-status",
                criterion_ids=["fixture-criterion"],
            )

    def test_ignores_commentary_and_accepts_only_completed_final_answer_item(self):
        commentary = self.output(status="insufficient_evidence")
        commentary["judgments"][0].update({"outcome": "insufficient_evidence", "evidence_ids": []})
        events = completed_events(self.output())
        events.insert(1, event(
            "agent.session.turn.output_text.done",
            session_id="sess-1",
            turn_id="turn-1",
            item_id="message-commentary",
            output_index=0,
            content_index=0,
            text=json.dumps(commentary),
        ))
        events.insert(2, event(
            "agent.session.turn.item.done",
            session_id="sess-1",
            turn_id="turn-1",
            output_index=0,
            item=SimpleNamespace(
                id="message-commentary", type="message", role="assistant", status="completed",
                phase="commentary",
                content=[SimpleNamespace(type="output_text", text=json.dumps(commentary))],
            ),
        ))
        adapter, _ = self.examiner(events=events)

        result = adapter.evaluate(
            run_id=self.run_id,
            execution_version=1,
            request_id="evaluation-final-phase",
            criterion_ids=["fixture-criterion"],
        )

        self.assertEqual(result.evaluation, self.output())

    def test_tool_cannot_read_an_event_outside_the_frozen_index(self):
        class ExpandingSource:
            def index(source_self, *, execution_version):
                return {
                    "run_id": self.run_id,
                    "execution_version": execution_version,
                    "evidence": [{"event_id": "observed-1"}],
                }

            def read(source_self, *, execution_version, evidence_ids):
                return {
                    "run_id": self.run_id,
                    "execution_version": execution_version,
                    "events": [{"event_id": "later-event"}],
                }

        output = self.output()
        output["judgments"][0]["evidence_ids"] = ["later-event"]
        sessions = FakeSessions(
            completed_events(output),
            tool_arguments={"execution_version": 1, "evidence_ids": ["later-event"]},
        )
        adapter = self.module.HostedAstraExaminer(
            FakeClient(sessions), ExpandingSource(), agent_id="agent-saved-1"
        )

        with self.assertRaises(self.module.ExaminerProviderError):
            adapter.evaluate(
                run_id=self.run_id,
                execution_version=1,
                request_id="evaluation-frozen-index",
                criterion_ids=["fixture-criterion"],
            )

    def test_requires_verified_tool_call_and_completed_top_level_turn(self):
        adapter, _ = self.examiner(tool_arguments=None, events=completed_events(self.output()))
        with self.assertRaises(self.module.ExaminerProtocolError):
            adapter.evaluate(run_id=self.run_id, execution_version=1,
                             request_id="evaluation-4", criterion_ids=["fixture-criterion"])

        failed = [
            event("agent.session.turn.created", session_id="sess-1", turn_id="turn-1",
                  turn=SimpleNamespace(subagent_id=None)),
            event("agent.session.turn.failed", session_id="sess-1", turn_id="turn-1",
                  turn=SimpleNamespace(status="failed")),
            event("agent.session.idle", session_id="sess-1"),
        ]
        adapter, _ = self.examiner(events=failed)
        with self.assertRaises(self.module.ExaminerProviderError):
            adapter.evaluate(run_id=self.run_id, execution_version=1,
                             request_id="evaluation-5", criterion_ids=["fixture-criterion"])

        self.assertEqual(adapter.client.beta.agents.sessions.delete_call, "sess-1")

    def test_session_cleanup_is_required_before_returning_success(self):
        sessions = FakeSessions(
            completed_events(self.output()),
            tool_arguments={"execution_version": 1, "evidence_ids": ["observed-1"]},
            deleted=False,
        )
        adapter = self.module.HostedAstraExaminer(
            FakeClient(sessions), self.source(), agent_id="agent-saved-1"
        )

        with self.assertRaisesRegex(
            self.module.ExaminerProviderError, "provider_session_cleanup_failed"
        ):
            adapter.evaluate(run_id=self.run_id, execution_version=1,
                             request_id="evaluation-cleanup", criterion_ids=["fixture-criterion"])


if __name__ == "__main__":
    unittest.main()
