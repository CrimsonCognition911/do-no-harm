# Hosted Astra examiner boundary

Status: narrow synthetic provider slice, verified 13 September 2026. This is not a clinical release contract.

## What exists

`backend/provision_examiner.py` creates a reusable, project-scoped Agents API agent
named `DO NO HARM Examiner` with:

- model `gpt-6-astra`;
- `environment={"type":"openai_hosted"}`;
- high reasoning effort;
- a strict structured-output schema; and
- exactly one application function, `get_evidence`.

`HostedAstraExaminer` requires the saved agent ID and creates each hosted evaluation
session with `agent_id`; it does not redefine or override the agent inline. The
session is created without task input. A subsequent streamed turn receives a bounded
synthetic evaluation request and an idempotency key. Success requires one top-level
completed turn, one successful `get_evidence` call and one structured result.
Session-level errors, failed or cancelled root turns, missing or repeated tool calls,
invented criterion/evidence references, mismatched run/version IDs and
non-provisional judgments fail closed.

`get_evidence` is read-only. Its arguments are only:

```json
{
  "execution_version": 1,
  "evidence_ids": ["observed-1"]
}
```

The model cannot choose an authoritative run ID, role, token, producer, visibility or evidence audience. `SessionEvidenceSource` binds the run in trusted application code and calls `SessionService.examiner_evidence(...)`. That method reauthorizes the examiner capability on every read, requires the current execution version and returns only explicitly requested records from the bound run. The initial evidence index contains identities and metadata but no event payloads. Limits are 200 indexed current-version records and 20 requested evidence IDs.

The structured result may contain only the supplied run/version and criterion IDs, and may cite only evidence returned by the tool. Outcomes are `acceptable`, `concern` or `insufficient_evidence`; every judgment must retain `requires_clinician_review: true`. The adapter returns the result to trusted examiner code. It does not itself append a finding, publish an event, pause the run, update OpenMRS or send text to GPT-Live-1; those application decisions belong to the coordinator below.

Before returning that result, `HostedAstraExaminer` calls the Agents API session-delete operation and requires `deleted: true`. A failed or unconfirmed deletion changes the otherwise successful evaluation into `provider_session_cleanup_failed`. After a provider session ID has been accepted, stream, tool, protocol and output-validation errors also trigger a best-effort deletion attempt. If cleanup then fails, the original error remains primary and receives a cleanup-failure note; there is no background cleanup retry.

## Provider proof versus offline tests

The unit tests inject fake session streams. They verify the hosted-Astra request shape, run authorization, current-version evidence selection, supported JSON Schema subset, grounding validation and failure handling without network access. They are not proof of OpenAI access.

On 13 September 2026, the real Agents API created the reusable `DO NO HARM
Examiner` in the configured OpenAI project. A separate bounded smoke then created an
OpenAI-hosted session from that saved agent ID, completed one synthetic
`get_evidence` call, returned a locally validated provisional result and deleted the
hosted session before returning. The saved agent remains available in the project;
the evaluation session does not. This demonstrates that exact saved-agent seam for
that run only. It is not evidence of clinical validity, medical-specialist
performance, production readiness, OpenMRS publication, Live audio or CUA behavior.

The smoke session was deleted with the Agents API after its result was captured. The current adapter now performs and verifies that deletion itself before returning success, while retaining `session_id`, `turn_id` and `environment_id` in the result only for correlation/proof. A disconnected stream does not imply deletion; it enters the error path, where deletion is attempted. Durable session/turn/call tracking, reconciliation after disconnect, persisted idempotent tool results and retry of an unsuccessful cleanup remain future work.

## Participant-safe Live projection

`backend/live_bridge.py` provides a pure projection for the planned GPT-Live-1 client-delegation boundary. It does not import a Live SDK or create a network connection.

Each `LiveCoachingBridge` instance is scoped to one trusted Live connection. Before
coaching, application code must register the complete provider-shaped
`session.delegation.created` event. Only a delegation with `target="client"` is
accepted. A server event ID cannot be rebound, and a delegation ID cannot be
registered again with conflicting data.

`LiveCoachingBridge.prepare(...)` accepts only a receipt ID and a previously
registered delegation ID. It does not accept an `ExaminerResult`, raw model output,
run/version fields or a caller-selected `review_allowed` flag. Instead, an injected
trusted resolver atomically consumes one immutable `TrustedExaminerReceipt`. The
receipt binds:

- the receipt, connection, client delegation and run IDs;
- the criterion and outcome selected for allowlisted coaching copy;
- immutable request and evidence snapshot IDs; and
- evaluation at execution version N to an authoritative pause/coaching snapshot at
  exactly N+1 with `review_allowed=True`.

A missing, replayed, forged, unlinked, stale or otherwise invalid receipt fails
closed. The repository defines this consume-once resolver interface but does not yet
provide its durable persistence implementation.

It never copies the examiner's rationale, evidence IDs or criterion ID into participant content. It selects trusted copy and emits only this detached command shape:

```json
{
  "type": "session.commentary.append",
  "event_id": "dnh_append_<opaque server-generated value>",
  "delegation_id": "item-delegation-1",
  "content": "Allowlisted coaching text"
}
```

Content is conservatively limited to 500 UTF-8 bytes because no provider tokenizer
is used. The append event ID is generated internally and cannot carry caller text.
The returned delivery begins with `receipt_state="verified"`,
`append_state="pending"` and `playback_state="pending"`. Append acceptance requires
the complete correlated `session.commentary.appended` event, including server and
client event IDs plus a valid start/end timeline. A correlated provider `error`
fails without copying its message into the bridge error. Playback remains a distinct
application acknowledgment; neither append acceptance nor playback state proves the
other. Repeated matching acknowledgments are idempotent, while conflicting
identifiers fail closed.

These are application-side value objects and tests only. There is no Live session, client-delegation request, transcript input, network send, provider receipt, generated audio or playback observation. Consequently there is no GPT-Live-1 provider proof yet.

## Running professor delegation

`POST /api/examiner` is the authenticated, server-to-server endpoint consumed by the
doctor BFF. It requires the existing run-scoped examiner capability. The request
contains the Live client-delegation ID, current run/version, bounded transcript
context and participant-visible evidence IDs. The application reauthorizes the run
and evidence before provider I/O and chooses all reviewed STEMI rubric criteria
server-side; the caller cannot select a rubric or pass raw clinical records.

`ExaminerBridgeService` calls the saved Astra agent, records every returned judgment
as an examiner-only provisional finding and returns exactly `status`,
`spoken_update` and participant-safe `evidence_ids`. Transcript is explicitly
unconfirmed context, not proof that care occurred. Returned language is frozen
application copy rather than model rationale:

- supported reasoning receives a brief acknowledgement and the next-priority question;
- insufficient evidence receives a focused clarification question;
- a concern returns `pause_requested` and requests a pause without racing a Live append against audio shutdown; and
- provider failure requests an unscored technical pause.

Exact delegation retries are cached within the process and conflicting reuse fails
closed. The cache and findings remain in memory. A concern reaches
`pause_requested`; teaching after the execution/audio pause acknowledgments must use
the stricter coaching projection and is not yet wired end to end. Provider evaluation
has a 50-second application deadline below the BFF's 60-second timeout; deadline or
provider failure creates no finding and requests an unscored technical pause. The
provider transport also disables automatic retries and uses a shorter read timeout.

`POST /api/examiner/delivery` records an in-process display receipt only after the
same examiner capability reauthorizes the current run/version and the referenced
participant event is a published clinical update. It does not prove speech or audio
playback.

## Credentials and dependency

The Python dependency is constrained to `openai>=3.13.0,<4`, whose `beta.agents`
client supports reusable agents and sessions created from `agent_id`. Provider
callers construct and inject the client; the adapter does not import credentials or
load dotenv files.

Use either the SDK-standard environment variable or the existing local alias explicitly:

```python
import os
from openai import OpenAI

api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_KEY")
if not api_key:
    raise RuntimeError("OpenAI API key is not configured")
client = OpenAI(api_key=api_key)
```

With the key exported into the server process, create the reusable agent once:

```sh
python3 -m backend.provision_examiner
```

The command prints a `DNH_EXAMINER_AGENT_ID=...` assignment. Store that non-secret,
project-specific ID in ignored local/server configuration. Evaluations can then use
`HostedAstraExaminer.from_environment(...)`. Running the provisioning command again
creates another reusable agent; it is not an idempotent update command.

Do not print the value, put it in provider metadata, expose it to the browser, commit `.env`, or include it in provider inputs/tool outputs. Merely having a key does not establish model or Agents API access; report access failures without changing model or silently substituting another API.

## Deliberately absent

- `/api/examiner` starts a bounded saved-agent evaluation, but no public endpoint exposes an Astra session or raw result.
- The reusable examiner agent is saved in the OpenAI project, but no local provider/session mapping or evidence ledger survives process restart.
- No durable reconnect/resume worker or retry queue for a failed hosted-session deletion exists.
- No raw Astra output is authorized for the participant feed.
- The BFF can submit bounded transcript context to `/api/examiner`, but no complete GPT-Live-1 browser/audio/client-delegation provider proof exists; the hardened post-pause command projection remains offline.
- No OpenMRS order/result publication or read-back occurs.
- A reviewed STEMI case is compiled and hash-bound, but it is not enabled for scored runtime use.
- No clinical validation, certification, pass/fail decision or autonomous treatment recommendation is claimed.

The next safe slice is to implement durable, atomic receipt creation/consumption and
send the allowlisted projection through a bounded real GPT-Live-1 client-delegation
session, verifying provider append acceptance and observed playback separately. It
must never forward raw examiner rationale, hidden rubric data, future case events or
the examiner evidence feed.
