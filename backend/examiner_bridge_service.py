"""Server-side GPT-Live delegation coordinator for the saved Astra examiner."""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from hashlib import sha256
import json
from pathlib import Path
from threading import RLock

from backend.examiner_agent import SessionEvidenceSource
from backend.session_service import APIError


ROOT = Path(__file__).resolve().parents[1]
SAFE_RESPONSES = {
    "acceptable": (
        "That reasoning is supported by the recorded evidence so far. "
        "What will you prioritize and reassess next?"
    ),
    "insufficient_evidence": (
        "Make your reasoning more explicit before we continue: what is your leading "
        "diagnosis, immediate priority, and what evidence would change your plan?"
    ),
    "concern": (
        "Pause. The examiner identified a point that needs evidence review. "
        "We will examine it together before you give a revised plan."
    ),
    "unavailable": (
        "The examiner is temporarily unavailable. The simulation is pausing for a "
        "technical review; this is not a clinical penalty."
    ),
}


def _text(value, *, maximum=256):
    return isinstance(value, str) and 1 <= len(value) <= maximum


def _load_criteria(path):
    try:
        compiled = json.loads(Path(path).read_text())
        values = [item["id"] for item in compiled["rubric"]["criteria"]]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError("Invalid compiled examiner rubric") from error
    if (not 1 <= len(values) <= 50 or len(values) != len(set(values))
            or any(not _text(item, maximum=128) for item in values)):
        raise ValueError("Invalid compiled examiner rubric")
    return tuple(values)


def _request(body):
    required = {
        "type", "run_id", "delegation_id", "offset_ms", "execution_version",
        "transcript", "participant_event_ids",
    }
    if not isinstance(body, dict) or body.keys() != required:
        raise APIError(422, "invalid_live_delegation")
    if (body["type"] != "live_delegation"
            or not _text(body["run_id"])
            or not _text(body["delegation_id"])
            or type(body["offset_ms"]) is not int or body["offset_ms"] < 0
            or type(body["execution_version"]) is not int
            or body["execution_version"] < 1):
        raise APIError(422, "invalid_live_delegation")
    transcript = body["transcript"]
    if not isinstance(transcript, list) or len(transcript) > 300:
        raise APIError(422, "invalid_live_delegation")
    allowed = {"speaker", "text", "partial", "corrected", "source", "start_ms", "end_ms"}
    total_text = 0
    for item in transcript:
        if (not isinstance(item, dict) or not set(item) <= allowed
                or item.get("speaker") not in {"doctor", "assistant"}
                or not _text(item.get("text"), maximum=4000)):
            raise APIError(422, "invalid_live_delegation")
        total_text += len(item["text"].encode("utf-8"))
        for flag in ("partial", "corrected"):
            if flag in item and type(item[flag]) is not bool:
                raise APIError(422, "invalid_live_delegation")
        for timestamp in ("start_ms", "end_ms"):
            value = item.get(timestamp)
            if value is not None and (type(value) not in (int, float) or value < 0):
                raise APIError(422, "invalid_live_delegation")
        if "source" in item and item["source"] != "typed":
            raise APIError(422, "invalid_live_delegation")
    if total_text > 50000:
        raise APIError(413, "delegation_context_too_large")
    evidence = body["participant_event_ids"]
    if (not isinstance(evidence, list) or len(evidence) > 1000
            or len(evidence) != len(set(evidence))
            or any(not _text(item) for item in evidence)):
        raise APIError(422, "invalid_live_delegation")
    return deepcopy(body)


def _delivery_request(body):
    required = {"type", "run_id", "event_id", "execution_version", "stage"}
    if (not isinstance(body, dict) or body.keys() != required
            or body.get("type") != "delivery_ack"
            or not _text(body.get("run_id"))
            or not _text(body.get("event_id"))
            or type(body.get("execution_version")) is not int
            or body["execution_version"] < 1
            or body.get("stage") != "displayed"):
        raise APIError(422, "invalid_delivery_ack")
    return deepcopy(body)


class ExaminerBridgeService:
    """Evaluate meaningful Live turns and return only frozen professor copy."""

    def __init__(
        self, session_service, *, examiner_agent_id, examiner_factory,
        compiled_case=ROOT / "cases" / "stemi" / "compiled.json",
        provider_timeout=120,
    ):
        if session_service is None or not _text(examiner_agent_id, maximum=128):
            raise ValueError("Invalid examiner bridge configuration")
        if not callable(examiner_factory):
            raise ValueError("Invalid examiner factory")
        if (type(provider_timeout) not in (int, float)
                or not 0 < provider_timeout <= 120):
            raise ValueError("Invalid examiner provider timeout")
        self._sessions = session_service
        self._agent_id = examiner_agent_id
        self._factory = examiner_factory
        self._criteria = _load_criteria(compiled_case)
        self._lock = RLock()
        self._cache = {}
        self._deliveries = {}
        self._provider_timeout = provider_timeout
        self._provider_workers = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="dnh-examiner"
        )

    def close(self):
        self._provider_workers.shutdown(wait=False, cancel_futures=True)

    def handle(self, token, body):
        request = _request(body)
        canonical = json.dumps(request, sort_keys=True, separators=(",", ":"))
        digest = sha256(canonical.encode()).hexdigest()
        key = (request["run_id"], request["delegation_id"])
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                previous_digest, response, replay_version = cached
                # Reauthorize the exact run/role against the authoritative version
                # reached by the original result. A concern legitimately advances
                # N to N+1 before its response can be cached.
                self._sessions.examiner_evidence(
                    token, request["run_id"], expected_version=replay_version,
                    evidence_ids=None,
                )
                if previous_digest != digest:
                    raise APIError(409, "delegation_id_conflict")
                return deepcopy(response)
            index = self._authorized_index(token, request)
            response = self._evaluate(token, request, digest, index)
            replay_version = self._sessions.handle(
                "GET", [request["run_id"]], {}, token, None
            )[1]["execution_version"]
            self._cache[key] = (digest, deepcopy(response), replay_version)
            return response

    def _authorized_index(self, token, request):
        index = self._sessions.examiner_evidence(
            token,
            request["run_id"],
            expected_version=request["execution_version"],
            evidence_ids=None,
        )
        public_ids = {
            item["event_id"] for item in index["evidence"]
            if item.get("visibility") == "participant"
        }
        if not set(request["participant_event_ids"]) <= public_ids:
            raise APIError(409, "participant_evidence_mismatch")
        return index

    def _evaluate(self, token, request, request_digest, index):
        source = SessionEvidenceSource(
            self._sessions, run_id=request["run_id"], examiner_token=token
        )
        try:
            examiner = self._factory(source, agent_id=self._agent_id)
            future = self._provider_workers.submit(
                examiner.evaluate,
                run_id=request["run_id"],
                execution_version=request["execution_version"],
                request_id=f"live-{request_digest}",
                criterion_ids=list(self._criteria),
                participant_context=request["transcript"],
            )
            result = future.result(timeout=self._provider_timeout)
        except APIError:
            raise
        except FutureTimeout:
            future.cancel()
            self._operational_pause(token, request, request_digest)
            return {
                "status": "unavailable",
                "spoken_update": SAFE_RESPONSES["unavailable"],
                "evidence_ids": [],
            }
        except Exception:
            self._operational_pause(token, request, request_digest)
            return {
                "status": "unavailable",
                "spoken_update": SAFE_RESPONSES["unavailable"],
                "evidence_ids": [],
            }

        evaluation = result.evaluation
        judgments = evaluation["judgments"]
        referenced = []
        for offset, judgment in enumerate(judgments):
            for evidence_id in judgment["evidence_ids"]:
                if evidence_id in request["participant_event_ids"] and evidence_id not in referenced:
                    referenced.append(evidence_id)
            self._sessions.handle(
                "POST", [request["run_id"], "findings"], {}, token,
                {
                    "event_id": f"astra-{request_digest[:24]}-{offset}",
                    "execution_version": request["execution_version"],
                    "evidence_ids": judgment["evidence_ids"],
                    "payload": {
                        "criterion_id": judgment["criterion_id"],
                        "outcome": judgment["outcome"],
                        "rationale": judgment["rationale"],
                        "requires_clinician_review": True,
                    },
                },
            )
        outcomes = {item["outcome"] for item in judgments}
        if "concern" in outcomes:
            selected = "concern"
            self._clinical_pause(token, request, request_digest)
            status = "pause_requested"
        elif "insufficient_evidence" in outcomes:
            selected, status = "insufficient_evidence", "needs_clarification"
        else:
            selected, status = "acceptable", "complete"
        return {
            "status": status,
            "spoken_update": SAFE_RESPONSES[selected],
            "evidence_ids": referenced,
        }

    def handle_delivery(self, token, body):
        request = _delivery_request(body)
        evidence = self._sessions.examiner_evidence(
            token,
            request["run_id"],
            expected_version=request["execution_version"],
            evidence_ids=[request["event_id"]],
        )["events"][0]
        if (evidence.get("type") != "clinical_update"
                or evidence.get("visibility") != "participant"
                or evidence.get("payload", {}).get("delivery_stage") != "published"):
            raise APIError(409, "unknown_publication")
        canonical = json.dumps(request, sort_keys=True, separators=(",", ":"))
        digest = sha256(canonical.encode()).hexdigest()
        key = (request["run_id"], request["event_id"])
        with self._lock:
            previous = self._deliveries.get(key)
            if previous is not None and previous != digest:
                raise APIError(409, "delivery_ack_conflict")
            self._deliveries[key] = digest
            return {"accepted": True}

    def _clinical_pause(self, token, request, request_digest):
        self._sessions.handle(
            "POST", [request["run_id"], "commands"], {}, token,
            {
                "request_id": f"astra-pause-{request_digest[:32]}",
                "command": "pause",
                "execution_version": request["execution_version"],
            },
        )

    def _operational_pause(self, token, request, request_digest):
        self._clinical_pause(token, request, request_digest)
