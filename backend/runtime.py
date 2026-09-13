"""In-memory run controller for offline integration development.

All methods are trusted server-side calls, not HTTP handlers. Adapters must
authenticate callers, bind resource references to a synthetic run, and authorize
commands before invoking them. Acknowledgments assert real adapter quiescence;
this module neither stops an external write nor controls an audio device itself.
"""
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import RLock
from time import monotonic
from uuid import uuid4

from backend.contracts import ACTORS, validate_event


class Run:
    def __init__(self, run_id, *, resource_refs, mode="coached", clock=monotonic):
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be nonempty")
        if mode not in ("coached", "assessment"):
            raise ValueError("Unknown run mode")
        if isinstance(resource_refs, str):
            raise ValueError("resource_refs must be a collection")
        self._resources = frozenset(resource_refs)
        if any(not isinstance(ref, str) or not ref for ref in self._resources):
            raise ValueError("Invalid resource reference")
        self._run_id, self._mode, self._clock = run_id, mode, clock
        self._lock = RLock()
        self._execution_gate = RLock()
        self._state, self._version, self._assisted = "created", 1, False
        self._elapsed, self._started_at = 0.0, None
        self._events, self._by_id, self._requests = [], {}, {}
        self._acks = set()
        self._publications = {}
        self._state_event()

    @property
    def state(self):
        with self._lock:
            return self._state

    @property
    def execution_version(self):
        with self._lock:
            return self._version

    @property
    def assisted(self):
        with self._lock:
            return self._assisted

    @property
    def simulation_time_ms(self):
        with self._lock:
            elapsed = self._elapsed
            if self._started_at is not None:
                elapsed += self._clock() - self._started_at
            return int(elapsed * 1000)

    @property
    def review_allowed(self):
        """Permission gate only; does not grant a CUA browser write capability."""
        with self._lock:
            return self._state in {"coaching", "debrief"}

    def _check(self, expected_version, states):
        if type(expected_version) is not int or expected_version != self._version:
            raise ValueError("Stale or invalid execution version")
        if self._state not in states:
            raise ValueError(f"Command not allowed while {self._state}")

    def _envelope(self, kind, payload, event_id, version, evidence_ids, visibility):
        return {
            "schema_version": "0.1", "type": kind, "event_id": event_id,
            "run_id": self._run_id, "actor": ACTORS[kind],
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "simulation_time_ms": self.simulation_time_ms, "execution_version": version,
            "evidence_ids": evidence_ids, "visibility": visibility, "payload": payload,
        }

    def _append(self, event):
        self._events.append(event)
        self._by_id[event["event_id"]] = event

    def _state_event(self):
        event = self._envelope("session_state", {"state": self._state, "mode": self._mode, "assisted": self._assisted},
                               f"__state:{uuid4()}", self._version, [], "participant")
        self._append(validate_event(event, producer="simulation_service"))

    def _transition(self, state):
        self._state = state
        self._state_event()

    def start(self, *, expected_version):
        with self._lock:
            self._check(expected_version, {"created"})
            self._started_at = self._clock()
            self._transition("running")

    def request_pause(self, *, expected_version):
        # Wait for a reserved external write to reconcile before exposing pause.
        # Once this returns, no current-version executor can dispatch a late write.
        with self._execution_gate:
            with self._lock:
                self._request_pause_locked(expected_version)

    def _request_pause_locked(self, expected_version):
        self._check(expected_version, {"running"})
        self._elapsed += self._clock() - self._started_at
        self._started_at = None
        self._version += 1
        self._acks.clear()
        self._transition("pause_requested")

    @contextmanager
    def execution_guard(self, *, expected_version):
        """Reserve current-version execution against the pause barrier."""
        with self._execution_gate:
            with self._lock:
                self._check(expected_version, {"running"})
            yield

    def request_technical_pause(self, *, expected_version):
        """Audio faults may stop a run, but cannot coach, resume or acknowledge execution."""
        with self._execution_gate:
            with self._lock:
                self._check(expected_version, {"created", "running", "pause_requested", "paused",
                                               "coaching", "resume_requested", "debrief", "ended", "failed"})
                if self._state == "running":
                    self._request_pause_locked(expected_version)
                elif self._state == "resume_requested":
                    self._version += 1
                    self._acks.clear()
                    self._transition("pause_requested")

    def acknowledge_pause(self, component, *, expected_version):
        """Execution must drain/reconcile writes; audio must stop/flush playback."""
        with self._lock:
            self._acknowledge(component, expected_version, "pause_requested", "paused")

    def _acknowledge(self, component, version, requested, completed):
        self._check(version, {requested, completed})
        if component not in ("execution", "audio"):
            raise ValueError("Unknown acknowledgment component")
        if self._state == completed:
            return
        self._acks.add(component)
        if self._acks == {"execution", "audio"}:
            if completed == "running":
                self._started_at = self._clock()
            self._transition(completed)

    def begin_coaching(self, *, expected_version):
        with self._lock:
            self._check(expected_version, {"paused"})
            self._assisted = True
            # Once assessment feedback is revealed, that scored attempt cannot resume.
            self._transition("debrief" if self._mode == "assessment" else "coaching")

    def request_resume(self, *, expected_version):
        with self._lock:
            self._check(expected_version, {"paused", "coaching"})
            self._version += 1
            self._acks.clear()
            self._transition("resume_requested")

    def acknowledge_resume(self, component, *, expected_version):
        """Adapters assert readiness under the new version before time restarts."""
        with self._lock:
            if self._state == "running" and not self._acks:
                raise ValueError("No resume was requested")
            self._acknowledge(component, expected_version, "resume_requested", "running")

    def end(self, *, expected_version):
        with self._lock:
            self._check(expected_version, {"created", "paused", "coaching", "debrief"})
            self._version += 1
            self._transition("ended")

    def record(self, *, event_id, expected_version, kind, producer, payload,
               evidence_ids, visibility):
        """Append validated adapter evidence; identifiers/timestamps are server-owned.

        Exact retries return the old receipt even after pausing; they never execute
        work again. A new event must have the current version and allowed state.
        Clinical updates here are evidence receipts, not an event-injection API.
        """
        with self._lock:
            if kind not in ("doctor_action", "clinical_update", "evaluation_finding"):
                raise ValueError("Unsupported ingest event type")
            if not isinstance(event_id, str) or not event_id or event_id.startswith("__state:"):
                raise ValueError("Invalid or reserved event ID")
            event = validate_event(self._envelope(kind, payload, event_id, expected_version, evidence_ids, visibility), producer=producer)
            request = {"version": expected_version, "kind": kind, "producer": producer,
                       "payload": event["payload"], "evidence_ids": event["evidence_ids"], "visibility": visibility}
            if event_id in self._by_id:
                if self._requests.get(event_id) != request:
                    raise ValueError("Event ID conflicts with earlier evidence")
                return deepcopy(self._by_id[event_id])
            allowed = {"running", "paused", "coaching", "debrief"} if kind == "evaluation_finding" else {"running"}
            self._check(expected_version, allowed)
            payload, evidence_ids = event["payload"], event["evidence_ids"]
            if "resource_ref" in payload and payload["resource_ref"] not in self._resources:
                raise ValueError("Resource is not registered to this synthetic run")
            for ref in evidence_ids:
                if ref not in self._by_id:
                    raise ValueError("Evidence must already exist in this run")
                if visibility == "participant" and self._by_id[ref]["visibility"] != "participant":
                    raise ValueError("Participant event cannot reference hidden evidence")
            if kind == "evaluation_finding":
                if not payload["requires_clinician_review"]:
                    raise ValueError("Automated findings must remain provisional")
                if payload["outcome"] != "insufficient_evidence" and not evidence_ids:
                    raise ValueError("A judgment requires evidence references")
            if kind == "clinical_update":
                self._check_delivery(payload, visibility)
            self._append(event)
            self._requests[event_id] = deepcopy(request)
            return deepcopy(event)

    def _check_delivery(self, payload, visibility):
        event_key = payload["scenario_event_id"]
        publication = {key: value for key, value in payload.items() if key != "delivery_stage"}
        publication["visibility"] = visibility
        if payload["delivery_stage"] == "published":
            if event_key in self._publications:
                raise ValueError("Scenario event was already published; retry its original event ID")
            self._publications[event_key] = publication
        elif self._publications.get(event_key) != publication:
            raise ValueError("Delivery acknowledgment requires matching published content")

    def events(self, *, audience="examiner"):
        """Detached ledger snapshot. Caller must authorize audience server-side."""
        with self._lock:
            if audience not in ("examiner", "participant"):
                raise ValueError("Unknown event audience")
            return deepcopy([event for event in self._events
                             if audience == "examiner" or event["visibility"] == "participant"])
