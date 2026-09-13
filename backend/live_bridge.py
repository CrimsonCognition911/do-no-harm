"""Provider-independent trust boundary for GPT-Live client delegation.

The bridge is scoped to one Live connection. It accepts provider event objects or
plain mappings, but retains only validated correlation metadata. Examiner output is
never accepted here: a trusted consume-once resolver supplies an immutable receipt,
which is projected to pre-approved participant copy.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
import secrets
from typing import Protocol


MAX_APPEND_TOKENS = 500
# One UTF-8 byte per possible token is deliberately conservative when no provider
# tokenizer is present, so every accepted template is within the 500-token limit.
MAX_APPEND_UTF8_BYTES = MAX_APPEND_TOKENS
OUTCOMES = {"acceptable", "concern", "insufficient_evidence"}


class LiveBridgeError(ValueError):
    """A trust, correlation, or Live event invariant failed closed."""


def _identifier(value, name, *, minimum=1):
    if not isinstance(value, str) or not minimum <= len(value) <= 128:
        raise LiveBridgeError(f"invalid_{name}")
    return value


def _version(value):
    if type(value) is not int or value < 1:
        raise LiveBridgeError("invalid_execution_version")
    return value


def _timeline_ms(value, name):
    if type(value) is not int or value < 0:
        raise LiveBridgeError(f"invalid_{name}")
    return value


def _content(value):
    if (not isinstance(value, str) or not value.strip()
            or len(value.encode("utf-8")) > MAX_APPEND_UTF8_BYTES):
        raise LiveBridgeError("invalid_coaching_content")
    return value


def _event_mapping(value, error_code):
    if isinstance(value, Mapping):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            result = dump(mode="json", exclude_none=True)
        except Exception as error:
            raise LiveBridgeError(error_code) from error
        if isinstance(result, dict):
            return result
    raise LiveBridgeError(error_code)


@dataclass(frozen=True)
class CoachingTemplate:
    """Participant-safe copy selected only by trusted criterion and outcome."""

    criterion_id: str
    outcome: str
    content: str

    def __post_init__(self):
        _identifier(self.criterion_id, "criterion_id")
        if self.outcome not in OUTCOMES:
            raise LiveBridgeError("invalid_outcome")
        _content(self.content)


@dataclass(frozen=True)
class CoachingAuthorizationSnapshot:
    """Authoritative post-pause snapshot attached by the trusted receipt store."""

    receipt_id: str
    connection_id: str
    delegation_id: str
    run_id: str
    execution_version: int
    criterion_id: str
    request_snapshot_id: str
    evidence_snapshot_id: str
    pause_request_id: str
    review_allowed: bool

    def __post_init__(self):
        for name in (
            "receipt_id", "connection_id", "delegation_id", "run_id",
            "criterion_id", "request_snapshot_id", "evidence_snapshot_id",
            "pause_request_id",
        ):
            _identifier(getattr(self, name), name)
        _version(self.execution_version)
        if type(self.review_allowed) is not bool:
            raise LiveBridgeError("invalid_review_allowed")


@dataclass(frozen=True)
class TrustedExaminerReceipt:
    """Immutable receipt persisted after a verified examiner evaluation at N."""

    receipt_id: str
    connection_id: str
    delegation_id: str
    run_id: str
    origin_execution_version: int
    criterion_id: str
    outcome: str
    request_snapshot_id: str
    evidence_snapshot_id: str
    authorization: CoachingAuthorizationSnapshot

    def __post_init__(self):
        for name in (
            "receipt_id", "connection_id", "delegation_id", "run_id",
            "criterion_id", "request_snapshot_id", "evidence_snapshot_id",
        ):
            _identifier(getattr(self, name), name)
        _version(self.origin_execution_version)
        if self.outcome not in OUTCOMES:
            raise LiveBridgeError("invalid_outcome")
        if type(self.authorization) is not CoachingAuthorizationSnapshot:
            raise LiveBridgeError("invalid_coaching_authorization")


class TrustedReceiptResolver(Protocol):
    """Persistence seam that atomically authorizes and consumes one receipt.

    The implementation must first match the stored receipt to ``receipt_id``,
    ``connection_id`` and ``delegation_id`` without consuming on a mismatch. It
    must then read the receipt's bound run and consume/return the receipt only when
    that run is currently at ``authorization.execution_version``, in the required
    state, and has the required review flag. It must invoke ``project`` before the
    final consume and commit only if projection succeeds. These checks, projection
    and consumption are one transaction; a historical authorization snapshot alone
    is not sufficient.
    """

    def consume_if_current(
        self,
        *,
        receipt_id: str,
        connection_id: str,
        delegation_id: str,
        required_state: str,
        required_review_allowed: bool,
        project: Callable[[TrustedExaminerReceipt], "_CoachingDelivery"],
    ) -> "_CoachingDelivery" | None:
        ...


@dataclass(frozen=True)
class _DelegationBinding:
    delegation_id: str
    server_event_id: str
    offset_ms: int


@dataclass(frozen=True)
class _CoachingDelivery:
    receipt_id: str
    append_event_id: str
    delegation_id: str
    content: str
    receipt_state: str = "verified"
    append_state: str = "pending"
    playback_state: str = "pending"
    _append_server_event_id: str | None = field(default=None, repr=False)
    _append_start_ms: int | None = field(default=None, repr=False)
    _append_end_ms: int | None = field(default=None, repr=False)
    _playback_id: str | None = field(default=None, repr=False)

    def __post_init__(self):
        _identifier(self.receipt_id, "receipt_id")
        _identifier(self.append_event_id, "append_event_id", minimum=16)
        _identifier(self.delegation_id, "delegation_id")
        _content(self.content)
        if self.receipt_state != "verified":
            raise LiveBridgeError("invalid_receipt_state")
        if self.append_state not in {"pending", "accepted"}:
            raise LiveBridgeError("invalid_append_state")
        append_values = (
            self._append_server_event_id,
            self._append_start_ms,
            self._append_end_ms,
        )
        if ((self.append_state == "pending") != all(value is None for value in append_values)
                or (self.append_state == "accepted" and any(value is None for value in append_values))):
            raise LiveBridgeError("invalid_append_acknowledgment")
        if self.playback_state not in {"pending", "completed"}:
            raise LiveBridgeError("invalid_playback_state")
        if ((self.playback_state == "pending") != (self._playback_id is None)):
            raise LiveBridgeError("invalid_playback_acknowledgment")

    def participant_command(self):
        return {
            "type": "session.commentary.append",
            "event_id": self.append_event_id,
            "delegation_id": self.delegation_id,
            "content": self.content,
        }

    def acknowledge_append(self, *, event):
        data = _event_mapping(event, "invalid_append_event")
        if data.get("type") == "error":
            correlation = _validate_error_event(data)
            if correlation == self.append_event_id:
                raise LiveBridgeError("append_rejected")
            raise LiveBridgeError("append_event_mismatch")
        server_event_id, start_ms, end_ms = _validate_commentary_ack(
            data, expected_client_event_id=self.append_event_id
        )
        if self.append_state == "accepted":
            if (server_event_id, start_ms, end_ms) != (
                self._append_server_event_id,
                self._append_start_ms,
                self._append_end_ms,
            ):
                raise LiveBridgeError("append_acknowledgment_conflict")
            return self
        return replace(
            self,
            append_state="accepted",
            _append_server_event_id=server_event_id,
            _append_start_ms=start_ms,
            _append_end_ms=end_ms,
        )

    def acknowledge_playback(self, *, receipt_id, playback_id):
        receipt_id = _identifier(receipt_id, "receipt_id")
        playback_id = _identifier(playback_id, "playback_id")
        if receipt_id != self.receipt_id:
            raise LiveBridgeError("playback_acknowledgment_mismatch")
        if self.playback_state == "completed":
            if playback_id != self._playback_id:
                raise LiveBridgeError("playback_acknowledgment_conflict")
            return self
        return replace(self, playback_state="completed", _playback_id=playback_id)


class LiveCoachingBridge:
    """One-connection registry and verified-receipt projection boundary."""

    def __init__(
        self,
        *,
        connection_id,
        templates,
        receipt_resolver: TrustedReceiptResolver,
        event_id_factory: Callable[[], str] | None = None,
    ):
        self._connection_id = _identifier(connection_id, "connection_id")
        consume = getattr(receipt_resolver, "consume_if_current", None)
        if not callable(consume):
            raise LiveBridgeError("invalid_receipt_resolver")
        if event_id_factory is not None and not callable(event_id_factory):
            raise LiveBridgeError("invalid_event_id_factory")
        self._receipt_resolver = receipt_resolver
        self._event_id_factory = event_id_factory or _secure_append_event_id
        self._allowlist = _coaching_allowlist(templates)
        self._delegations = {}
        self._delegation_event_ids = set()
        self._consumed_receipt_ids = set()
        self._issued_append_event_ids = set()

    def register_delegation(self, event):
        data = _event_mapping(event, "invalid_delegation_event")
        allowed = {"type", "event_id", "offset_ms", "delegation", "client_event_id"}
        if set(data) - allowed or not {"type", "event_id", "offset_ms", "delegation"} <= set(data):
            raise LiveBridgeError("invalid_delegation_event")
        if data["type"] != "session.delegation.created":
            raise LiveBridgeError("invalid_delegation_event")
        server_event_id = _identifier(data["event_id"], "delegation_event_id")
        offset_ms = _timeline_ms(data["offset_ms"], "delegation_offset_ms")
        if data.get("client_event_id") is not None:
            _identifier(data["client_event_id"], "client_event_id")
        delegation = data["delegation"]
        if not isinstance(delegation, Mapping):
            raise LiveBridgeError("invalid_delegation_event")
        delegation = dict(delegation)
        if (set(delegation) - {"id", "type", "target", "response_id"}
                or not {"id", "type", "target"} <= set(delegation)
                or delegation["type"] != "delegation"
                or delegation["target"] != "client"
                or delegation.get("response_id") is not None):
            raise LiveBridgeError("invalid_delegation_event")
        delegation_id = _identifier(delegation["id"], "delegation_id")
        binding = _DelegationBinding(delegation_id, server_event_id, offset_ms)
        existing = self._delegations.get(delegation_id)
        if existing is not None:
            if existing != binding:
                raise LiveBridgeError("delegation_registration_conflict")
            return delegation_id
        if server_event_id in self._delegation_event_ids:
            raise LiveBridgeError("delegation_registration_conflict")
        self._delegations[delegation_id] = binding
        self._delegation_event_ids.add(server_event_id)
        return delegation_id

    def prepare(self, *, receipt_id, delegation_id):
        receipt_id = _identifier(receipt_id, "receipt_id")
        delegation_id = _identifier(delegation_id, "delegation_id")
        if delegation_id not in self._delegations:
            raise LiveBridgeError("delegation_not_registered")
        if receipt_id in self._consumed_receipt_ids:
            raise LiveBridgeError("receipt_already_consumed")
        projected = []

        def project(receipt):
            if projected:
                raise LiveBridgeError("duplicate_receipt_projection")
            criterion_id, outcome = _trusted_projection(
                receipt,
                receipt_id=receipt_id,
                connection_id=self._connection_id,
                delegation_id=delegation_id,
            )
            content = self._allowlist.get((criterion_id, outcome))
            if content is None:
                raise LiveBridgeError("coaching_projection_not_allowlisted")
            delivery = _CoachingDelivery(
                receipt_id=receipt_id,
                append_event_id=self._next_append_event_id(),
                delegation_id=delegation_id,
                content=content,
            )
            projected.append(delivery)
            return delivery

        try:
            delivery = self._receipt_resolver.consume_if_current(
                receipt_id=receipt_id,
                connection_id=self._connection_id,
                delegation_id=delegation_id,
                required_state="paused",
                required_review_allowed=True,
                project=project,
            )
        except LiveBridgeError:
            raise
        except Exception as error:
            raise LiveBridgeError("receipt_resolution_failed") from error
        if delivery is None:
            raise LiveBridgeError("receipt_not_current_or_bound")
        if len(projected) != 1 or delivery is not projected[0]:
            raise LiveBridgeError("invalid_receipt_projection")
        self._consumed_receipt_ids.add(receipt_id)
        return delivery

    def _next_append_event_id(self):
        try:
            event_id = self._event_id_factory()
        except Exception as error:
            raise LiveBridgeError("event_id_generation_failed") from error
        event_id = _identifier(event_id, "append_event_id", minimum=16)
        if event_id in self._issued_append_event_ids:
            raise LiveBridgeError("duplicate_append_event_id")
        self._issued_append_event_ids.add(event_id)
        return event_id


def _secure_append_event_id():
    return f"dnh_append_{secrets.token_urlsafe(24)}"


def _coaching_allowlist(templates):
    if isinstance(templates, (str, bytes)):
        raise LiveBridgeError("invalid_coaching_allowlist")
    try:
        templates = tuple(templates)
    except TypeError as error:
        raise LiveBridgeError("invalid_coaching_allowlist") from error
    if not 1 <= len(templates) <= 50:
        raise LiveBridgeError("invalid_coaching_allowlist")
    result = {}
    for template in templates:
        if type(template) is not CoachingTemplate:
            raise LiveBridgeError("invalid_coaching_template")
        key = (template.criterion_id, template.outcome)
        if key in result:
            raise LiveBridgeError("duplicate_coaching_template")
        result[key] = template.content
    return result


def _trusted_projection(receipt, *, receipt_id, connection_id, delegation_id):
    if type(receipt) is not TrustedExaminerReceipt:
        raise LiveBridgeError("trusted_examiner_receipt_required")
    authorization = receipt.authorization
    if type(authorization) is not CoachingAuthorizationSnapshot:
        raise LiveBridgeError("invalid_coaching_authorization")
    shared = (
        "receipt_id", "connection_id", "delegation_id", "run_id", "criterion_id",
        "request_snapshot_id", "evidence_snapshot_id",
    )
    if (receipt.receipt_id != receipt_id
            or receipt.connection_id != connection_id
            or receipt.delegation_id != delegation_id
            or any(getattr(receipt, name) != getattr(authorization, name) for name in shared)
            or authorization.execution_version != receipt.origin_execution_version + 1
            or authorization.review_allowed is not True):
        raise LiveBridgeError("receipt_authorization_binding_mismatch")
    _identifier(authorization.pause_request_id, "pause_request_id")
    return receipt.criterion_id, receipt.outcome


def _validate_commentary_ack(data, *, expected_client_event_id):
    allowed = {"type", "event_id", "client_event_id", "start_ms", "end_ms"}
    if set(data) - allowed or set(data) != allowed:
        raise LiveBridgeError("invalid_append_event")
    if data["type"] != "session.commentary.appended":
        raise LiveBridgeError("invalid_append_event")
    server_event_id = _identifier(data["event_id"], "append_server_event_id")
    client_event_id = _identifier(data["client_event_id"], "client_event_id")
    if client_event_id != expected_client_event_id:
        raise LiveBridgeError("append_event_mismatch")
    start_ms = _timeline_ms(data["start_ms"], "append_start_ms")
    end_ms = _timeline_ms(data["end_ms"], "append_end_ms")
    if end_ms < start_ms:
        raise LiveBridgeError("invalid_append_timeline")
    return server_event_id, start_ms, end_ms


def _validate_error_event(data):
    allowed = {"type", "event_id", "error", "client_event_id"}
    if set(data) - allowed or not {"type", "event_id", "error"} <= set(data):
        raise LiveBridgeError("invalid_error_event")
    if data["type"] != "error":
        raise LiveBridgeError("invalid_error_event")
    _identifier(data["event_id"], "error_event_id")
    if data.get("client_event_id") is not None:
        _identifier(data["client_event_id"], "client_event_id")
    error = data["error"]
    if not isinstance(error, Mapping):
        raise LiveBridgeError("invalid_error_event")
    error = dict(error)
    allowed_error = {"type", "code", "message", "client_event_id", "param"}
    if (set(error) - allowed_error
            or not {"type", "code", "message"} <= set(error)):
        raise LiveBridgeError("invalid_error_event")
    for name in ("type", "code", "message"):
        if not isinstance(error[name], str) or not error[name]:
            raise LiveBridgeError("invalid_error_event")
    correlation = error.get("client_event_id")
    if correlation is not None:
        _identifier(correlation, "client_event_id")
    param = error.get("param")
    if param is not None and not isinstance(param, str):
        raise LiveBridgeError("invalid_error_event")
    return correlation
