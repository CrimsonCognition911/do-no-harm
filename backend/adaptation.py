"""Frozen, bounded adaptive planning; never performs an OpenMRS write.

Version 0.1 remains available for non-clinical fixtures. Version 0.2 adds
reviewed state-relative scheduling and publication effects for compiled cases.
Durable publication adapters remain separate and fail closed.
"""
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
from threading import RLock


def require(condition, message):
    if not condition:
        raise ValueError(message)


def text(value):
    return isinstance(value, str) and bool(value.strip())


def integer(value, minimum=0):
    return type(value) is int and value >= minimum


def keys(value, expected):
    require(isinstance(value, dict) and value.keys() == set(expected), "Missing or unknown case fields")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def hash_value(value):
    return sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True, init=False)
class FrozenCase:
    _json: str
    fixture: bool

    def __init__(self, document, *, fixture=False, approved_sha256=None):
        """Approval hash must come from a trusted clinical-review registry.

        Never derive approval from a model/client's claim or auto-approve the hash
        computed here. This primitive compares content; it cannot verify who
        reviewed it. ``fixture=True`` explicitly bypasses scored-use eligibility.
        """
        require(type(fixture) is bool, "fixture must be boolean")
        doc = deepcopy(document)
        keys(doc, {"format", "id", "version", "description", "rubric", "policy", "initial_state", "actions", "events"})
        require(doc["format"] in ("dnh.compiled-case/0.1", "dnh.compiled-case/0.2"), "Unsupported compiled case version")
        for field in ("id", "version", "description"):
            require(text(doc[field]), "Case metadata must be nonempty")
        rubric = doc["rubric"]
        rubric_fields = {"id", "version", "criteria"}
        if doc["format"] == "dnh.compiled-case/0.2":
            rubric_fields |= {"outcomes", "requires_clinician_review", "numeric_pass_fail_score_enabled", "alternatives_policy"}
        keys(rubric, rubric_fields)
        require(text(rubric["id"]) and text(rubric["version"]), "Invalid rubric identity")
        require(isinstance(rubric["criteria"], list) and rubric["criteria"], "Empty rubric")
        if doc["format"] == "dnh.compiled-case/0.2":
            require(rubric["outcomes"] == ["acceptable", "concern", "insufficient_evidence"], "Invalid rubric outcomes")
            require(rubric["requires_clinician_review"] is True, "Clinical findings must require review")
            require(rubric["numeric_pass_fail_score_enabled"] is False, "Numeric competence scoring is not supported")
            require(text(rubric["alternatives_policy"]), "Clinical alternatives policy is required")
        criteria = set()
        for criterion in rubric["criteria"]:
            keys(criterion, {"id", "description"})
            require(text(criterion["id"]) and text(criterion["description"]), "Invalid criterion")
            require(criterion["id"] not in criteria, "Duplicate criterion")
            criteria.add(criterion["id"])
        policy = doc["policy"]
        keys(policy, {"version", "min_difficulty", "max_difficulty", "max_pending_challenges"})
        require(text(policy["version"]), "Invalid policy version")
        require(all(integer(policy[field], 1) for field in ("min_difficulty", "max_difficulty", "max_pending_challenges")), "Invalid policy bounds")
        require(policy["min_difficulty"] <= policy["max_difficulty"], "Reversed difficulty bounds")
        state = doc["initial_state"]
        require(isinstance(state, dict) and state and all(text(k) and type(v) is bool for k, v in state.items()), "State must contain named booleans")
        require(isinstance(doc["actions"], list), "Actions must be a list")
        actions = set()
        for action in doc["actions"]:
            keys(action, {"action", "set_state"})
            require(text(action["action"]) and action["action"] not in actions, "Invalid or duplicate action")
            actions.add(action["action"])
            self._check_state(action["set_state"], state)
        require(isinstance(doc["events"], list) and doc["events"], "Empty event set")
        event_ids = set()
        for event in doc["events"]:
            expected = {"id", "kind", "summary", "resource_key", "earliest_ms", "latest_ms", "requires", "difficulty", "criterion_id", "outcome", "allowed_modes"}
            if doc["format"] == "dnh.compiled-case/0.2":
                expected |= {"schedule", "set_state"}
            keys(event, expected)
            require(all(text(event[field]) for field in ("id", "summary", "resource_key")), "Invalid event identity/content")
            require(event["id"] not in event_ids, "Duplicate event")
            event_ids.add(event["id"])
            require(event["kind"] in ("clinical_consequence", "optional_challenge"), "Unsupported event kind")
            require(integer(event["earliest_ms"]) and integer(event["latest_ms"]) and event["earliest_ms"] <= event["latest_ms"], "Invalid timing window")
            self._check_state(event["requires"], state)
            modes = event["allowed_modes"]
            require(isinstance(modes, list) and modes and all(m in ("coached", "assessment") for m in modes), "Invalid allowed modes")
            require(integer(event["difficulty"]), "Invalid difficulty")
            if doc["format"] == "dnh.compiled-case/0.2":
                schedule = event["schedule"]
                keys(schedule, {"basis", "state_ref", "delay_ms"})
                require(schedule["basis"] in ("simulation_time", "state_transition", "examiner_selection"), "Invalid schedule basis")
                require(integer(schedule["delay_ms"]), "Invalid schedule delay")
                if schedule["basis"] == "state_transition":
                    require(schedule["state_ref"] in state, "Unknown schedule state")
                else:
                    require(schedule["state_ref"] is None, "Unexpected schedule state")
                self._check_state(event["set_state"], state)
            if event["kind"] == "optional_challenge":
                require(isinstance(event["criterion_id"], str) and event["criterion_id"] in criteria, "Unknown rubric criterion")
                require(event["outcome"] in ("acceptable", "concern"), "Uncertainty must retain current path")
                require(policy["min_difficulty"] <= event["difficulty"] <= policy["max_difficulty"], "Difficulty outside policy")
                if doc["format"] == "dnh.compiled-case/0.2":
                    require(event["schedule"]["basis"] == "examiner_selection", "Optional challenges require examiner selection")
                    require(event["set_state"] == {}, "Educational challenges cannot mutate clinical state")
            else:
                require(event["criterion_id"] is None and event["outcome"] is None and event["difficulty"] == 0, "Clinical consequences cannot depend on performance")
                if doc["format"] == "dnh.compiled-case/0.2":
                    require(event["schedule"]["basis"] != "examiner_selection", "Clinical events require deterministic schedules")
        encoded = canonical(doc)
        case_hash = sha256(encoded.encode()).hexdigest()
        require(fixture or approved_sha256 == case_hash, "Clinical approval for this exact case hash is required")
        object.__setattr__(self, "_json", encoded)
        object.__setattr__(self, "fixture", fixture)

    @staticmethod
    def _check_state(changes, state):
        require(isinstance(changes, dict) and all(k in state and type(v) is bool for k, v in changes.items()), "Unknown state reference or non-boolean value")

    @property
    def document(self):
        return json.loads(self._json)

    @property
    def case_hash(self):
        return sha256(self._json.encode()).hexdigest()

    @property
    def rubric_hash(self):
        return hash_value(self.document["rubric"])

    @property
    def policy_hash(self):
        return hash_value(self.document["policy"])


class AdaptivePlanner:
    def __init__(self, case, run, *, bindings):
        self._case, self._run = case, run
        self._run_id = run.events()[0]["run_id"]
        self._doc = case.document
        resource_keys = {event["resource_key"] for event in self._doc["events"]}
        require(isinstance(bindings, dict) and bindings.keys() == resource_keys and all(text(v) for v in bindings.values()), "Missing or unknown resource bindings")
        self._bindings = deepcopy(bindings)
        self._events = {event["id"]: event for event in self._doc["events"]}
        self._pending, self._requests, self._audit = {}, {}, []
        self._lock = RLock()

    def _context(self, version):
        require(type(version) is int and version == self._run.execution_version and self._run.state == "running", "Run is paused or execution version is stale")
        ledger = self._run.events()
        state = deepcopy(self._doc["initial_state"])
        state_changed_at = {key: 0 if value else None for key, value in state.items()}
        changes = {action["action"]: action["set_state"] for action in self._doc["actions"]}
        published = set()
        for item in ledger:
            payload = item["payload"]
            if item["type"] == "doctor_action" and payload["phase"] == "confirmed":
                for key, value in changes.get(payload["action"], {}).items():
                    if state[key] != value:
                        state[key] = value
                        state_changed_at[key] = item["simulation_time_ms"]
            elif item["type"] == "clinical_update" and payload["delivery_stage"] == "published":
                event = self._events.get(payload["scenario_event_id"])
                if event and payload["summary"] == event["summary"] and payload["resource_ref"] == self._bindings[event["resource_key"]]:
                    published.add(event["id"])
                    for key, value in event.get("set_state", {}).items():
                        if state[key] != value:
                            state[key] = value
                            state_changed_at[key] = item["simulation_time_ms"]
        return ledger, state, state_changed_at, published, self._run.simulation_time_ms

    @staticmethod
    def _scheduled_at(event, state_changed_at):
        schedule = event.get("schedule")
        if not schedule or schedule["basis"] == "simulation_time":
            return event["earliest_ms"]
        if schedule["basis"] == "state_transition":
            changed_at = state_changed_at[schedule["state_ref"]]
            return None if changed_at is None else changed_at + schedule["delay_ms"]
        return None

    @staticmethod
    def _eligible(event, state, mode):
        return mode in event["allowed_modes"] and all(state[k] == v for k, v in event["requires"].items())

    def _prune(self, version, state, published, now, mode):
        for event_id, plan in list(self._pending.items()):
            event = self._events[event_id]
            reason = None
            if plan["execution_version"] != version:
                reason = "invalidated_execution_version"
            elif event_id in published:
                reason = "publication_receipt_observed"
            elif not self._eligible(event, state, mode):
                reason = "precondition_no_longer_met"
            elif now > event["latest_ms"]:
                reason = "optional_window_expired"
            if reason:
                self._audit.append({**deepcopy(plan), "status": reason})
                del self._pending[event_id]

    def _plan(self, event, at_ms, version, evidence_ids, reason):
        return {"run_id": self._run_id, "event_id": event["id"], "kind": event["kind"], "summary": event["summary"],
                "resource_ref": self._bindings[event["resource_key"]], "scheduled_at_ms": at_ms,
                "difficulty": event["difficulty"], "execution_version": version,
                "evidence_ids": evidence_ids, "reason": reason, "case_hash": self._case.case_hash,
                "rubric_hash": self._case.rubric_hash, "policy_hash": self._case.policy_hash,
                "fixture": self._case.fixture, "status": "planned_not_published"}

    def propose(self, *, request_id, event_id, finding_id, at_ms, difficulty, reason, expected_version):
        with self._lock:
            require(all(text(v) for v in (request_id, event_id, finding_id, reason)), "Proposal identity/reason is required")
            require(integer(at_ms) and integer(difficulty, 1), "Invalid proposal timing or difficulty")
            require(integer(expected_version, 1), "Invalid execution version")
            request = (event_id, finding_id, at_ms, difficulty, reason, expected_version)
            if request_id in self._requests:
                old, receipt = self._requests[request_id]
                require(old == request, "Conflicting proposal request ID")
                return deepcopy(receipt)
            ledger, state, _state_changed_at, published, now = self._context(expected_version)
            mode = ledger[0]["payload"]["mode"]
            self._prune(expected_version, state, published, now, mode)
            event = self._events.get(event_id)
            require(event is not None and event["kind"] == "optional_challenge", "Examiner may select only optional challenges")
            require(event_id not in published and event_id not in self._pending, "Event already published or pending")
            require(len(self._pending) < self._doc["policy"]["max_pending_challenges"], "Pending challenge limit reached")
            require(self._eligible(event, state, mode), "Event preconditions are not met")
            require(now <= at_ms and event["earliest_ms"] <= at_ms <= event["latest_ms"], "Timing outside reviewed window")
            require(difficulty == event["difficulty"], "Difficulty does not match frozen event")
            finding = next((item for item in ledger if item["event_id"] == finding_id), None)
            require(finding is not None and finding["type"] == "evaluation_finding", "A same-run examiner finding is required")
            require(finding["execution_version"] == expected_version, "Finding predates the current execution version")
            require(finding["payload"]["criterion_id"] == event["criterion_id"] and finding["payload"]["outcome"] == event["outcome"], "Finding does not support this branch")
            require(finding["evidence_ids"], "Branch selection needs observed evidence")
            plan = self._plan(event, at_ms, expected_version, [finding_id, *finding["evidence_ids"]], reason)
            self._pending[event_id] = deepcopy(plan)
            self._requests[request_id] = (request, deepcopy(plan))
            self._audit.append(deepcopy(plan))
            return plan

    def due_events(self, *, expected_version):
        """Return planned work only. Executor must revalidate before any real write.

        Clinical consequences that are already due are never delayed by optional
        pacing or dropped merely because their latest time passed. A matching
        authoritative publication receipt, not an announcement, marks delivery.
        """
        with self._lock:
            ledger, state, state_changed_at, published, now = self._context(expected_version)
            mode = ledger[0]["payload"]["mode"]
            self._prune(expected_version, state, published, now, mode)
            result = []
            for event in self._doc["events"]:
                scheduled_at = self._scheduled_at(event, state_changed_at)
                if event["kind"] == "clinical_consequence" and event["id"] not in published and scheduled_at is not None and now >= scheduled_at and self._eligible(event, state, mode):
                    result.append(self._plan(event, scheduled_at, expected_version, [], "Frozen state/time rule; independent of performance"))
            result.sort(key=lambda item: item["scheduled_at_ms"])
            result.extend(deepcopy(plan) for plan in self._pending.values() if plan["scheduled_at_ms"] <= now)
            return result

    def audit(self):
        with self._lock:
            return deepcopy(self._audit)
