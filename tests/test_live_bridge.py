"""Pure Live bridge tests; no test opens a provider connection."""

from dataclasses import replace
import unittest

try:
    from openai.types.live import CommentaryAppendedEvent, DelegationCreatedEvent
    from openai.types.live.delegation_created_event import Delegation
    from openai.types.live import Error, ErrorEvent
except ImportError:
    CommentaryAppendedEvent = DelegationCreatedEvent = Delegation = None
    Error = ErrorEvent = None


class OneTimeReceiptResolver:
    def __init__(self, *receipts):
        self._receipts = {receipt.receipt_id: receipt for receipt in receipts}
        self.calls = []
        self.requests = []
        self._run_state = {
            receipt.run_id: {
                "execution_version": receipt.authorization.execution_version,
                "state": "paused",
                "review_allowed": True,
            }
            for receipt in receipts
        }

    def set_run_state(self, run_id, *, execution_version, state, review_allowed):
        self._run_state[run_id] = {
            "execution_version": execution_version,
            "state": state,
            "review_allowed": review_allowed,
        }

    def consume_if_current(
        self, *, receipt_id, connection_id, delegation_id,
        required_state, required_review_allowed, project,
    ):
        self.calls.append(receipt_id)
        self.requests.append({
            "receipt_id": receipt_id,
            "connection_id": connection_id,
            "delegation_id": delegation_id,
            "required_state": required_state,
            "required_review_allowed": required_review_allowed,
        })
        receipt = self._receipts.get(receipt_id)
        if (receipt is None
                or receipt.connection_id != connection_id
                or receipt.delegation_id != delegation_id):
            return None
        current = self._run_state.get(receipt.run_id)
        if (current is None
                or current["execution_version"] != receipt.authorization.execution_version
                or current["state"] != required_state
                or current["review_allowed"] is not required_review_allowed):
            return None
        result = project(receipt)
        self._receipts.pop(receipt_id)
        return result


class LiveBridgeTests(unittest.TestCase):
    def setUp(self):
        from backend import live_bridge
        self.module = live_bridge
        self.safe_content = "Let us review the evidence for that decision together."
        self.receipt = self.make_receipt()
        self.resolver = OneTimeReceiptResolver(self.receipt)
        generated_ids = iter(["dnh_append_4f40c167d5274be18d71d71a"])
        self.bridge = live_bridge.LiveCoachingBridge(
            connection_id="live-connection-1",
            templates=[live_bridge.CoachingTemplate(
                "fixture-criterion", "concern", self.safe_content
            )],
            receipt_resolver=self.resolver,
            event_id_factory=generated_ids.__next__,
        )

    def make_receipt(self, **changes):
        module = getattr(self, "module", None)
        if module is None:
            from backend import live_bridge as module
        authorization = module.CoachingAuthorizationSnapshot(
            receipt_id="examiner-receipt-1",
            connection_id="live-connection-1",
            delegation_id="item_9tA2bF3h7K9m2P5q8R1s4",
            run_id="run-1",
            execution_version=3,
            criterion_id="fixture-criterion",
            request_snapshot_id="request-snapshot-sha256-1",
            evidence_snapshot_id="evidence-snapshot-sha256-1",
            pause_request_id="pause-request-1",
            review_allowed=True,
        )
        values = dict(
            receipt_id="examiner-receipt-1",
            connection_id="live-connection-1",
            delegation_id="item_9tA2bF3h7K9m2P5q8R1s4",
            run_id="run-1",
            origin_execution_version=2,
            criterion_id="fixture-criterion",
            outcome="concern",
            request_snapshot_id="request-snapshot-sha256-1",
            evidence_snapshot_id="evidence-snapshot-sha256-1",
            authorization=authorization,
        )
        values.update(changes)
        return module.TrustedExaminerReceipt(**values)

    @staticmethod
    def delegation_event(**changes):
        event = {
            "type": "session.delegation.created",
            "event_id": "event_delegation_1",
            "offset_ms": 1000,
            "delegation": {
                "id": "item_9tA2bF3h7K9m2P5q8R1s4",
                "type": "delegation",
                "target": "client",
            },
        }
        event.update(changes)
        return event

    @staticmethod
    def appended_event(client_event_id, **changes):
        event = {
            "type": "session.commentary.appended",
            "event_id": "event_commentary_appended_1",
            "client_event_id": client_event_id,
            "start_ms": 1100,
            "end_ms": 1200,
        }
        event.update(changes)
        return event

    def prepare(self):
        self.bridge.register_delegation(self.delegation_event())
        return self.bridge.prepare(
            receipt_id="examiner-receipt-1",
            delegation_id="item_9tA2bF3h7K9m2P5q8R1s4",
        )

    def test_full_client_delegation_and_internal_event_id(self):
        self.bridge.register_delegation(self.delegation_event())
        delivery = self.bridge.prepare(
            receipt_id="examiner-receipt-1",
            delegation_id="item_9tA2bF3h7K9m2P5q8R1s4",
        )
        self.assertEqual(delivery.participant_command(), {
            "type": "session.commentary.append",
            "event_id": "dnh_append_4f40c167d5274be18d71d71a",
            "delegation_id": "item_9tA2bF3h7K9m2P5q8R1s4",
            "content": self.safe_content,
        })
        self.assertEqual(self.resolver.calls, ["examiner-receipt-1"])
        self.assertEqual(self.resolver.requests[0], {
            "receipt_id": "examiner-receipt-1",
            "connection_id": "live-connection-1",
            "delegation_id": "item_9tA2bF3h7K9m2P5q8R1s4",
            "required_state": "paused",
            "required_review_allowed": True,
        })

    @unittest.skipUnless(DelegationCreatedEvent, "openai dependency unavailable")
    def test_actual_openai_3_13_delegation_model(self):
        event = DelegationCreatedEvent(
            type="session.delegation.created", event_id="event_delegation_1",
            offset_ms=1000,
            delegation=Delegation(id="item_9tA2bF3h7K9m2P5q8R1s4",
                                  type="delegation", target="client"),
        )
        self.assertEqual(self.bridge.register_delegation(event),
                         "item_9tA2bF3h7K9m2P5q8R1s4")

    def test_rejects_bad_or_unregistered_delegations(self):
        invalid = [
            {"type": "session.delegation.created"},
            self.delegation_event(type="session.started"),
            self.delegation_event(offset_ms=-1),
            self.delegation_event(offset_ms=True),
            self.delegation_event(delegation={"id": "item_1", "type": "delegation",
                                              "target": "responses"}),
            self.delegation_event(delegation={"id": "item_1", "type": "bad",
                                              "target": "client"}),
        ]
        for event in invalid:
            with self.subTest(event=event), self.assertRaises(self.module.LiveBridgeError):
                self.bridge.register_delegation(event)
        with self.assertRaisesRegex(self.module.LiveBridgeError, "delegation_not_registered"):
            self.bridge.prepare(receipt_id="examiner-receipt-1",
                                delegation_id="item_9tA2bF3h7K9m2P5q8R1s4")
        self.assertEqual(self.resolver.calls, [])

    def test_trusted_receipt_allows_origin_N_to_coaching_N_plus_1(self):
        delivery = self.prepare()
        self.assertEqual((delivery.receipt_state, delivery.append_state,
                          delivery.playback_state), ("verified", "pending", "pending"))

    def test_rejects_unlinked_or_unauthorized_snapshot(self):
        base = self.make_receipt()
        auth = base.authorization
        invalid = [
            replace(auth, execution_version=2), replace(auth, execution_version=4),
            replace(auth, review_allowed=False), replace(auth, receipt_id="other"),
            replace(auth, run_id="other"), replace(auth, criterion_id="other"),
            replace(auth, request_snapshot_id="other"),
            replace(auth, evidence_snapshot_id="other"),
            replace(auth, delegation_id="item_other"),
            replace(auth, connection_id="other"),
        ]
        for bad_auth in invalid:
            receipt = replace(base, authorization=bad_auth)
            bridge = self._bridge_for(receipt)
            bridge.register_delegation(self.delegation_event())
            with self.subTest(bad_auth=bad_auth), self.assertRaises(self.module.LiveBridgeError):
                bridge.prepare(receipt_id=receipt.receipt_id,
                               delegation_id=receipt.delegation_id)

    def _bridge_for(self, receipt, generated="dnh_append_2a6ea920e59b41238b497d4d"):
        return self.module.LiveCoachingBridge(
            connection_id="live-connection-1",
            templates=[self.module.CoachingTemplate(
                "fixture-criterion", "concern", self.safe_content
            )],
            receipt_resolver=OneTimeReceiptResolver(receipt),
            event_id_factory=lambda: generated,
        )

    def test_receipt_is_one_time_and_no_raw_result_or_review_flag_is_accepted(self):
        self.prepare()
        with self.assertRaisesRegex(self.module.LiveBridgeError, "receipt_already_consumed"):
            self.bridge.prepare(receipt_id="examiner-receipt-1",
                                delegation_id="item_9tA2bF3h7K9m2P5q8R1s4")
        self.assertEqual(self.resolver.calls, ["examiner-receipt-1"])
        with self.assertRaises(TypeError):
            self.bridge.prepare(receipt_id="new", delegation_id="item_other",
                                result={"raw": "untrusted"}, review_allowed=True)

    def test_wrong_delegation_cannot_burn_a_valid_receipt(self):
        other_delegation = "item_other_9tA2bF3h7K9m2P5q8R1s4"
        self.bridge.register_delegation(self.delegation_event(
            event_id="event_delegation_other",
            delegation={
                "id": other_delegation,
                "type": "delegation",
                "target": "client",
            },
        ))
        self.bridge.register_delegation(self.delegation_event())

        with self.assertRaisesRegex(
            self.module.LiveBridgeError, "receipt_not_current_or_bound"
        ):
            self.bridge.prepare(
                receipt_id="examiner-receipt-1", delegation_id=other_delegation
            )

        delivery = self.bridge.prepare(
            receipt_id="examiner-receipt-1",
            delegation_id="item_9tA2bF3h7K9m2P5q8R1s4",
        )
        self.assertEqual(delivery.receipt_state, "verified")
        self.assertEqual(self.resolver.calls,
                         ["examiner-receipt-1", "examiner-receipt-1"])

    def test_receipt_is_rejected_after_run_resumes_past_authorized_version(self):
        self.bridge.register_delegation(self.delegation_event())
        self.resolver.set_run_state(
            "run-1", execution_version=4, state="running", review_allowed=False
        )

        with self.assertRaisesRegex(
            self.module.LiveBridgeError, "receipt_not_current_or_bound"
        ):
            self.bridge.prepare(
                receipt_id="examiner-receipt-1",
                delegation_id="item_9tA2bF3h7K9m2P5q8R1s4",
            )

        self.assertIn("examiner-receipt-1", self.resolver._receipts)

    def test_untrusted_value_and_unallowlisted_outcome_fail_closed(self):
        class BadResolver:
            def consume_if_current(self, **values):
                return values["project"]({"raw_examiner_output": "SECRET RAW"})
        bridge = self.module.LiveCoachingBridge(
            connection_id="live-connection-1",
            templates=[self.module.CoachingTemplate(
                "fixture-criterion", "concern", self.safe_content
            )], receipt_resolver=BadResolver(),
            event_id_factory=lambda: "dnh_append_dc288f83523e4a68bcf2e57e")
        bridge.register_delegation(self.delegation_event())
        with self.assertRaises(self.module.LiveBridgeError) as raised:
            bridge.prepare(receipt_id="examiner-receipt-1",
                           delegation_id="item_9tA2bF3h7K9m2P5q8R1s4")
        self.assertNotIn("SECRET RAW", str(raised.exception))

        receipt = self.make_receipt(outcome="acceptable")
        bridge = self._bridge_for(receipt)
        bridge.register_delegation(self.delegation_event())
        with self.assertRaises(self.module.LiveBridgeError):
            bridge.prepare(receipt_id=receipt.receipt_id,
                           delegation_id=receipt.delegation_id)

    @unittest.skipUnless(CommentaryAppendedEvent, "openai dependency unavailable")
    def test_actual_openai_3_13_commentary_ack_model(self):
        delivery = self.prepare()
        command_id = delivery.participant_command()["event_id"]
        event = CommentaryAppendedEvent(
            type="session.commentary.appended", event_id="event_ack_1",
            client_event_id=command_id, start_ms=1100, end_ms=1200)
        self.assertEqual(delivery.acknowledge_append(event=event).append_state,
                         "accepted")

    def test_full_ack_timing_correlation_and_independent_playback(self):
        delivery = self.prepare()
        command_id = delivery.participant_command()["event_id"]
        invalid = [
            self.appended_event(command_id, type="session.thinking.appended"),
            self.appended_event("other"), self.appended_event(command_id, start_ms=-1),
            self.appended_event(command_id, start_ms=True),
            self.appended_event(command_id, start_ms=1201, end_ms=1200),
            {"type": "session.commentary.appended", "client_event_id": command_id},
        ]
        for event in invalid:
            with self.subTest(event=event), self.assertRaises(self.module.LiveBridgeError):
                delivery.acknowledge_append(event=event)
        played = delivery.acknowledge_playback(
            receipt_id="examiner-receipt-1", playback_id="audio-1")
        appended = played.acknowledge_append(event=self.appended_event(command_id))
        self.assertEqual((appended.append_state, appended.playback_state),
                         ("accepted", "completed"))
        self.assertEqual(appended.acknowledge_append(
            event=self.appended_event(command_id)), appended)
        with self.assertRaises(self.module.LiveBridgeError):
            appended.acknowledge_append(event=self.appended_event(
                command_id, event_id="different-server-ack"))

    @unittest.skipUnless(ErrorEvent, "openai dependency unavailable")
    def test_correlated_openai_error_fails_without_exposing_message(self):
        delivery = self.prepare()
        command_id = delivery.participant_command()["event_id"]
        event = ErrorEvent(
            type="error", event_id="event_error_1",
            error=Error(type="invalid_request_error", code="invalid_value",
                        message="SECRET PROVIDER DETAIL", param="content",
                        client_event_id=command_id))
        with self.assertRaisesRegex(self.module.LiveBridgeError, "append_rejected") as raised:
            delivery.acknowledge_append(event=event)
        self.assertNotIn("SECRET PROVIDER DETAIL", str(raised.exception))
        self.assertEqual(delivery.append_state, "pending")

    def test_bad_generated_id_and_content_limit_fail_closed(self):
        for generated in ("", 7, "x" * 129):
            receipt = self.make_receipt()
            bridge = self._bridge_for(receipt, generated)
            bridge.register_delegation(self.delegation_event())
            with self.subTest(generated=generated), self.assertRaises(self.module.LiveBridgeError):
                bridge.prepare(receipt_id=receipt.receipt_id,
                               delegation_id=receipt.delegation_id)
        self.module.CoachingTemplate("fixture-criterion", "concern", "a" * 500)
        with self.assertRaises(self.module.LiveBridgeError):
            self.module.CoachingTemplate("fixture-criterion", "concern", "a" * 501)

    def test_projection_failure_does_not_consume_valid_receipt(self):
        generated = iter(["bad", "dnh_append_2a6ea920e59b41238b497d4d"])
        bridge = self.module.LiveCoachingBridge(
            connection_id="live-connection-1",
            templates=[self.module.CoachingTemplate(
                "fixture-criterion", "concern", self.safe_content
            )],
            receipt_resolver=self.resolver,
            event_id_factory=generated.__next__,
        )
        bridge.register_delegation(self.delegation_event())
        with self.assertRaisesRegex(self.module.LiveBridgeError, "invalid_append_event_id"):
            bridge.prepare(
                receipt_id=self.receipt.receipt_id,
                delegation_id=self.receipt.delegation_id,
            )
        self.assertIn(self.receipt.receipt_id, self.resolver._receipts)
        self.assertEqual(
            bridge.prepare(
                receipt_id=self.receipt.receipt_id,
                delegation_id=self.receipt.delegation_id,
            ).receipt_state,
            "verified",
        )

    def test_resolver_cannot_substitute_unallowlisted_delivery(self):
        module = self.module

        class SubstitutingResolver:
            def consume_if_current(self, **values):
                values["project"](self.receipt)
                return module._CoachingDelivery(
                    receipt_id=self.receipt.receipt_id,
                    append_event_id="dnh_append_attacker_supplied_123",
                    delegation_id=self.receipt.delegation_id,
                    content="RAW EXAMINER OUTPUT",
                )

        resolver = SubstitutingResolver()
        resolver.receipt = self.receipt
        bridge = module.LiveCoachingBridge(
            connection_id="live-connection-1",
            templates=[module.CoachingTemplate(
                "fixture-criterion", "concern", self.safe_content
            )],
            receipt_resolver=resolver,
            event_id_factory=lambda: "dnh_append_safe_generated_12345",
        )
        bridge.register_delegation(self.delegation_event())
        with self.assertRaisesRegex(module.LiveBridgeError, "invalid_receipt_projection"):
            bridge.prepare(
                receipt_id=self.receipt.receipt_id,
                delegation_id=self.receipt.delegation_id,
            )

    def test_projection_contains_no_examiner_or_receipt_bindings(self):
        delivery = self.prepare()
        command = delivery.participant_command()
        command["content"] = "tampered"
        self.assertEqual(delivery.participant_command()["content"], self.safe_content)
        exposed = repr(delivery) + repr(delivery.participant_command())
        for forbidden in ("fixture-criterion", "request-snapshot", "evidence-snapshot",
                          "pause-request", "origin_execution_version"):
            self.assertNotIn(forbidden, exposed)


if __name__ == "__main__":
    unittest.main()
