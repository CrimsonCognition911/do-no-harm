"""Executable offline control-flow fixture; no clinical case or provider calls."""
import json

from backend.runtime import Run


def run_demo():
    now = 0.0
    run = Run("synthetic-demo", resource_refs={"synthetic-order-1"}, clock=lambda: now)
    blocked = []
    run.start(expected_version=1)

    def record(event_id, payload, producer="browser", version=1):
        return run.record(event_id=event_id, expected_version=version, kind="doctor_action",
                          producer=producer, payload=payload, evidence_ids=[], visibility="participant")

    observation = {"action": "open order form", "phase": "observed", "source": "browser"}
    record("observation-1", observation)
    confirmation = {"action": "submit synthetic order", "phase": "confirmed", "source": "openmrs_backend", "resource_ref": "synthetic-order-1"}
    try:
        record("forged-1", confirmation)
    except ValueError:
        blocked.append("forged_confirmation")
    record("confirmed-1", confirmation, producer="openmrs_backend")
    run.record(event_id="finding-1", expected_version=1, kind="evaluation_finding",
               producer="examiner", visibility="examiner", evidence_ids=["observation-1", "confirmed-1"],
               payload={"criterion_id": "fixture-only", "outcome": "concern", "rationale": "Scripted non-clinical finding to exercise coaching flow", "requires_clinician_review": True})
    now = 3.0
    run.request_pause(expected_version=1)
    try:
        run.begin_coaching(expected_version=2)
    except ValueError:
        blocked.append("premature_review")
    # These are explicit fixture acknowledgments, not real device/write control.
    for component in ("execution", "audio"):
        run.acknowledge_pause(component, expected_version=2)
    run.begin_coaching(expected_version=2)
    now = 50.0
    run.request_resume(expected_version=2)
    for component in ("execution", "audio"):
        run.acknowledge_resume(component, expected_version=3)
    try:
        record("stale-1", observation)
    except ValueError:
        blocked.append("stale_action")
    now = 52.0
    record("observation-2", observation, version=3)
    participant_events = run.events(audience="participant")
    return {
        "mode": "offline_fixture", "final_state": run.state, "assisted": run.assisted,
        "simulation_time_ms": run.simulation_time_ms, "blocked": blocked,
        "hidden_findings": len(run.events()) - len(participant_events),
        "participant_events": participant_events,
    }


if __name__ == "__main__":
    print(json.dumps(run_demo(), indent=2))
