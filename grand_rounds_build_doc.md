# DO NO HARM

**Build plan: a live emergency-medicine examiner and teaching system**
Updated 13 September 2026, Singapore.

> A doctor manages a synthetic emergency in OpenMRS. Our examiner introduces new information, evaluates the response, and—during coached training—pauses to show and explain mistakes.

Status: proposed architecture and build tasks, not implemented or clinically validated capabilities. This is an educational simulation, not a system for treating real patients or certifying specialist competence. The filename is retained for existing links.

## 1. Product decision

The primary participant is an **emergency medicine specialist at a computer**. DO NO HARM is the examiner, not the doctor and not a replacement EMR.

Keep **OpenMRS 3** as the clinical workspace. Configure it for the emergency-department scenario; do not build another chart application or fork OpenMRS core. Build only the simulation, examiner, voice and evidence-review layer around it.

Retain the original **AI-agent evaluation mode** as a secondary participant adapter. Both modes share case definitions and evidence infrastructure, but the doctor experience is the main hackathon demo.

The proposed competition tracks are **Best use of GPT-Live-1** and **Best use of Agents API**, from the user-supplied prize list. CUA supports the teaching experience; it is not a third track.

## 2. The experience

1. The specialist opens an assigned synthetic patient in the configured OpenMRS ED workspace.
2. GPT-Live-1 introduces the case and conducts a natural spoken conversation. The doctor reviews the chart, explains decisions and acts in OpenMRS.
3. As the doctor acts, Astra reassesses their observed performance and the patient state, then dynamically chooses the next permitted event, its timing and challenge level. Our backend releases the validated update; GPT-Live-1 announces it once publication is confirmed.
4. Astra evaluates what the doctor said and actually did against the frozen case rubric and available evidence.
5. In coached mode, a supported concern triggers an acknowledged simulation pause.
6. CUA opens the relevant chart item or evidence replay in a separate read-only review browser. GPT-Live-1 explains the concern and discusses it with the doctor.
7. The doctor explains a revised plan in their own words. The session resumes with coaching explicitly recorded.

The signature moment is: **“Pause. Here is what changed, here is what you did, and here is why we need to reconsider.”** It must be grounded in the actual recorded run, not a scripted accusation.

## 3. What the examiner is made of

**YAML describes the case. Clinician-written rubrics define what matters. Code controls the simulation. Astra evaluates. GPT-Live-1 converses. CUA shows the evidence.**

| Part | Responsibility | Boundary |
|---|---|---|
| YAML case definition | Initial patient state, available information, event branches, triggers and rubric references | Data, not an executable examiner or proof of clinical validity |
| Clinician-written Markdown and structured rubrics | Clinical assumptions, acceptable alternatives, sources, teaching points and review criteria | Reviewed and versioned before scored use |
| Backend code | Case validation, OpenMRS mapping, clock, events, permissions, evidence and objective checks | Authoritative state; models cannot bypass it |
| Astra through Agents API | Draft cases, evaluate reasoning/actions, adapt permitted events and pacing to performance, and prepare evidence-grounded feedback | Cannot rewrite the answer key or invent observations |
| GPT-Live-1 | Introduce the case, listen and speak naturally, announce confirmed events, explain findings and conduct teach-back | Not the store of hidden rubric or future events |
| CUA, directed by Astra | Navigate the review browser to show where the doctor went wrong | Review only; never acts as the doctor or edits the clinical record |
| OpenMRS 3 | Existing patient chart, forms, results and supported clinical workflows | Synthetic isolated environment, not production care |

Use **gpt-6-astra** as the examiner model in a managed **Agents API** session. The Agents API provides the agent runtime; our application implements and authorizes its tools. It is not interchangeable with the Agents SDK or a standalone Responses request. See [Agents API overview](https://developers.openai.com/api/docs/guides/agents-api/overview) and [function tools](https://developers.openai.com/api/docs/guides/agents-api/tools/functions).

## 4. Architecture

```text
Doctor <---- speech ----> GPT-Live-1
   |                           |
   | uses                      | client delegation / approved spoken updates
   v                           v
Configured OpenMRS ED <--> DO NO HARM backend <--> Astra examiner / Agents API
   ^                         |       |
   | synthetic events        |       +--> objective checks + rubric ratings
   |                         +----------> immutable evidence timeline
   |
   +---- backend-owned case state, clock and run-scoped API access

During acknowledged pause or debrief only:
Astra --> CUA --> separate read-only OpenMRS / evidence review browser
                         |
                         +--> GPT-Live-1 explains the displayed evidence
```

OpenMRS remains the EMR. The evaluator's small session/control/evidence interface is not a replacement patient chart.

The existing emr-webmcp repository contains an OpenMRS adapter and synthetic-data tooling that may be reused after compatibility checks. Source inspection is not proof that its earlier deployment is currently healthy. Do not reuse stale hostnames, credentials or assumptions without verification.

## 5. OpenMRS emergency department configuration

### 5.1 Deliverable and scope

Add an early build workstream: **configure a synthetic OpenMRS 3 instance as the ED examination environment**.

Deliver a versioned ED configuration pack: distribution/module versions, metadata manifest, location/visit/encounter mappings, forms and concept references, role matrix, one synthetic case seed and a smoke-test checklist. These are planned deliverables; this document does not create or apply them.

Use existing O3 configuration and installed modules. Service queues are documented for outpatient workflows, while ward/bed tooling targets inpatient workflows; their suitability for this ED simulation must be verified rather than assumed. See [patient management configuration](https://o3-docs.openmrs.org/en-US/docs/configure-o3/configure-patient-management/).

### 5.2 Configuration tasks

| Task | Planned ED configuration | MVP limit / verification |
|---|---|---|
| Pin the environment | Record OpenMRS backend/frontend, REST/FHIR support, form engine, queue/ward modules and concept dictionary versions | Check actual installed capabilities and API resources; no core fork |
| Define locations | Emergency Department parent; Triage, Resuscitation and Observation child locations | One patient and one treatment space; no full hospital bed system |
| Define the visit | Emergency Visit type, linked to the correct synthetic patient and location | Resolve installed UUIDs; do not invent IDs or create duplicate metadata on rerun |
| Define encounters | Triage, ED Assessment, Reassessment and Disposition with clinician-reviewed forms | Save each encounter under the same active visit with correct author/time |
| Configure the ED list | Identifier, arrival time, presenting complaint, recorded acuity, location, responsible clinician and workflow status | Use service queues if compatible; otherwise a configured active-visit list |
| Configure chart navigation | History, allergies, current medication, serial vitals, examinations, results, supported orders, reassessment and disposition | Expose the installed modules needed for the single case |
| Configure clinical content | Required concepts, units, forms, result types and supported orderables | Only the demo case's content; clinically reviewed mappings |
| Separate identities | Doctor, simulation service and read-only examiner-review account | No shared admin login; prove backend write denial for the review account |
| Connect scenario events | Map approved observations/results to supported OpenMRS API writes | Read back durable state and verify O3 displays the update exactly once |
| Capture review evidence | Run-scoped action log, timestamps, chart references and screenshots | Preserve historical evidence; do not claim OpenMRS natively rewinds the chart |

Proposed local workflow labels are **waiting → being assessed → observation → disposition**. Map them to supported queue/visit state; these are not asserted to be native OpenMRS enum values. Resuscitation is a treatment location, not an invented triage category.

O3 service queues support configurable priorities, statuses, columns and vital-sign references. Configure them for the agreed scenario rather than treating a renamed queue as a complete ED triage implementation. See [service queue configuration](https://o3-docs.openmrs.org/en-US/docs/configure-o3/configure-service-queues/).

O3 also supports configurable chart navigation and forms, including use of an existing triage form. Confirm the schema against the pinned distribution before applying examples. See [patient chart configuration](https://openmrs.atlassian.net/wiki/spaces/docs/pages/151060769).

Do not invent acuity thresholds, drug doses or treatment protocols in configuration. Record clinician-assigned acuity for the MVP; adopting an automated triage scheme is a separate clinical decision requiring a named, reviewed specification. OpenMRS is not being claimed as a bedside physiological monitor.

### 5.3 Event and permission integration

The simulation backend, **not CUA**, injects reviewed events. Each write must resolve the run's patient, visit, encounter, location, concept, units and relevant order/result linkage. Keep observation time, publication time and simulation time distinct.

Record separately: requested action, submitted order, executed simulated action where supported, resulted test, displayed finding and spoken announcement. A request to order a test is not a result; saying a treatment was given is not evidence of administration.

The existing emr-webmcp event-pump source emits periodic event labels through a callback. That is reusable scaffolding, not proof of a working ED physiology or result-injection engine. Implement and verify the required event mapping.

Restrict tools to the current run's synthetic patient and visit. OpenMRS role privileges alone must not be assumed to enforce per-run patient isolation: use a synthetic-only instance plus explicit run allowlisting in our controlled access path.

A hidden button is not a permission boundary. Test server-side denial for examiner-review writes. The doctor's browser must also use the controlled path so pause can block in-flight/late simulation writes; stock O3 does not supply our pause protocol.

Never reset a shared database. Start a new run with fresh synthetic patient/visit identifiers and preserve the previous run's evidence.

### 5.4 Exit checks

- ED list and configured chart open with the correct synthetic patient, locations and required forms.
- Triage and ED assessment save against the correct active visit.
- One delayed result appears once, with correct concept, units, timestamps and visible chart state.
- Doctor actions and simulation-service events have separate authorship in the evidence log.
- Pause freezes the simulation clock and rejects stale writes through the controlled access path.
- CUA can navigate to linked evidence through the read-only account, but a write attempt is denied.
- Historical screenshots/events remain available after the chart changes; current chart state is not mislabeled as past state.
- A clinician approves the demo case, forms and rubric; missing workflows are labeled unsupported.
- Restart creates a separate run without wiping earlier evidence.

If advanced queue/bed configuration delays the demo, use OpenMRS's active-visit list and configured location labels. Do not respond by building a new EMR.

## 6. Case generation and performance-adaptive live events

Astra creates a **case draft before the run**, using an approved emergency template. The draft includes initial information, permissible branches, new results or deterioration, and references to reviewed rubric criteria.

The backend validates schema, references, concept mappings, supported actions and event consistency. Clinical review is a separate gate: passing software checks does not establish medical correctness.

**The case is adaptive, not a fixed script.** Astra uses recorded decisions, actions, expressed reasoning, response timing and current patient state to decide what happens next. It reassesses at confirmed actions, delivered events and scheduled review checkpoints, rather than treating every partial transcript as a performance signal.

| Observed performance | Permitted response in coached training |
|---|---|
| Managing the case well | Let appropriate actions have their reviewed effects; introduce a harder approved branch or a new decision once its prerequisites are met |
| Struggling or missing important information | Adjust optional challenge pacing; let the reviewed clinical consequences unfold where justified; pause for evidence-based coaching when the intervention criterion is met |
| Evidence is incomplete or ambiguous | Keep the current clinical path, seek clarification if needed, and avoid escalating difficulty on an uncertain judgment |

Freeze the approved case, rubric, **adaptation policy**, possible branches, timing bounds, versions and hashes before starting—not the exact sequence of optional challenges. Astra chooses within those boundaries during the run; the backend checks clinical/state preconditions, difficulty limits and pending events before publishing each update idempotently.

Separate **patient response** from **educational difficulty**. Deterioration or recovery follows reviewed case rules and the doctor's actual actions, not an arbitrary good/bad score. Struggling does not automatically make the patient worse, and a successful action is not retrospectively canceled just to make the exam harder. Adjusting challenge pacing must not silently postpone an already-due clinical consequence.

Record each adaptation's evidence IDs, reason, selected event, timing, difficulty and policy version. Keep the grading criteria unchanged. In assessment mode, follow the predeclared adaptive policy without coaching; report the actual difficulty/path and do not compare raw totals from unequal paths as equivalent scores.

For the MVP, use one synthetic emergency with incomplete initial information, one meaningful deterioration and one new result. No free-form, unreviewed clinical changes halfway through the exam.

## 7. GPT-Live-1 integration

Use GPT-Live-1 for full-duplex conversation: the doctor can interrupt, correct or ask questions while backend work proceeds. Make it a two-way clinical discussion, not just a narration track. See [GPT-Live overview](https://developers.openai.com/api/docs/guides/live).

**GPT-Live-1 accepts audio/text, not image or video input.** Astra interprets relevant screenshots alongside authoritative application events. Live receives only the grounded information it needs to speak. See the [model capability page](https://developers.openai.com/api/docs/models/gpt-live-1).

Select **client delegation** for Live. Our application collects transcript events and relevant state, sends work to the Astra Agents API session, validates the result and returns permitted updates to Live. Selecting a Responses backend in Live would not create an Agents API session. See [Live delegation](https://developers.openai.com/api/docs/guides/live-delegation).

Maintain transcript timestamps, corrections, speaker attribution and conversation context: delegation metadata does not itself contain the task text. A partial transcript can be wrong or incomplete; ask for clarification before treating an ambiguous statement as a decision.

During active assessment, Live receives only participant-visible facts and procedural instructions. Keep rubric answers, unreleased events and examiner findings in the examiner backend. Switch explicitly into coach mode only after an acknowledged pause or at debrief.

Backend-originated events must reach Live through the application's supported session-control channel, even if the doctor is not currently speaking. Track publication, notification and actual audio delivery separately; a backend acknowledgment is not proof the doctor heard or understood an update.

Use browser WebRTC on a trusted origin with server-controlled session creation. Keep provider keys out of the browser. Obtain consent for recording and define access, retention and deletion for doctor audio/transcripts.

### 7.1 Pause is a state transition, not just a sentence

```text
running -> pause_requested -> paused -> coaching -> resume_requested -> running
                                  |
                                  +-> assessment ended / debrief
```

On pause request, our backend stops simulation-time progression, gates writes, and cancels or rejects stale queued work using run state/version checks. Wait for in-flight operations to resolve or be safely rejected before acknowledging the pause.

Control pending audio playback so stale clinical instructions do not continue after a pause. Only then should Live announce that the simulation is paused and CUA begin review.

Interrupting speech does **not** cancel backend work. Likewise, validating a backend result does not approve every word Live might speak. Test playback, tool cancellation, reconnects and stale-result handling explicitly.

A network/model failure causes an operational pause and an incomplete/technical status—not an automatic clinical penalty. Record wall-clock latency separately from simulation time; do not promise instantaneous evaluation.

## 8. Coaching versus assessment

| Mode | Feedback policy | Meaning of the result |
|---|---|---|
| Coached training — default | Pause on a supported concern, show evidence, explain, discuss, ask for teach-back, resume | Preserve pre-coaching performance; subsequent performance is assisted |
| Assessment | Withhold hints and examiner findings during the scored portion; debrief afterwards | Unassisted performance only until teaching starts; any teaching intervention ends that portion |

Do not quietly turn a coached run into an exam score. Record the intervention reason, evidence, timing and assistance given.

An operational pause is distinct from a clinical finding. A trainee-requested explanation that reveals an answer must be recorded as assistance rather than ignored.

Formative education is the initial use. Consequential grading requires independent clinical review and validation; the prototype does not certify competence, licensing readiness or patient safety.

## 9. Astra as evaluator

Astra is the evaluator, not merely a narrator of a numeric score.

| Evidence or judgment | Owner | Required handling |
|---|---|---|
| Recorded actions, timing, state changes and fixed rule checks | Backend code | Objective observations tied to immutable event IDs |
| Reasoning, communication and adaptation | Astra against the frozen rubric | Structured rating, rationale, evidence IDs, uncertainty and review flag |
| Nuanced or consequential clinical conclusion | Qualified clinician review | Confirm or correct the provisional model judgment |

Evaluate what was observable and what the doctor actually said. Do not invent unspoken reasoning or infer execution from a proposed action. An incorrect spoken suggestion may support a reasoning finding without being logged as an executed order.

Every finding must identify the rubric criterion, available information at the time, participant action/utterance, relevant evidence and plausible acceptable alternatives. Missing evidence means **insufficient evidence**, not a guessed failure.

Clinical judgments and objective checks remain separate in the report. Deterministic code can enforce a clinically wrong rule; Astra can misinterpret a clinically reasonable alternative. Both require review.

Preserve case/rubric hashes, model/session identifiers, event path, assistance, timing and result versions. Never overwrite the raw trajectory or silently replace a published evaluation.

## 10. CUA: show the mistake, do not take the exam

CUA is **the examiner's hands**. After pause acknowledgment or during debrief, Astra selects an evidence item and CUA navigates the separate read-only OpenMRS/review browser to show it.

Examples: open the relevant result, scroll to an allergy entry, focus a changed vital-sign trend, or navigate the timestamped action replay. GPT-Live-1 explains the significance and asks the doctor what they would change.

CUA does not operate a nurse workstation, release results, execute spoken orders, make clinical decisions for the doctor, correct the live record or take over the doctor's active browser.

Limit CUA to allowlisted run-scoped review surfaces. Record review actions as examiner actions, separate from participant actions, so they cannot improve or worsen the doctor's score.

Our evidence UI may draw a callout around a finding; a callout is application functionality, not a presumed built-in computer-use feature. A fallback direct link or screenshot must be labeled as such, not reported as successful CUA execution.

If the screenshot is stale or the record cannot be reached, say so. Do not substitute unrelated evidence. Showing a corrected workflow would require an isolated practice copy and is outside the MVP.

See [computer-use guidance](https://developers.openai.com/api/docs/guides/tools-computer-use): the application supplies the controlled environment, executes actions and verifies their results.

## 11. Minimal case contract

Illustrative YAML, **not an OpenAI API request or a complete clinically validated case**. References below are placeholders that must resolve to reviewed content before a scored run. The backend assigns immutable run bindings at compilation.

```yaml
schema_version: 3
id: emergency-reassessment-demo
participant_mode: doctor
session_mode: coached
environment: openmrs_ed
template_ref: emergency_reassessment_v1
rubric_ref: emergency_reassessment_v1
initial_state_ref: template.initial_state
clinical_review_required: true
adaptation:
  policy_ref: template.reviewed_adaptive_policy
  checkpoints: [confirmed_action, event_delivered, scheduled_review]
  controls: [event_selection, timing_within_bounds, difficulty_within_bounds]
  require_evidence: true
  max_pending_challenges: 1
  insufficient_evidence_action: retain_current_path
compiled_bindings:
  case_hash: assigned_by_compiler
  rubric_hash: assigned_by_compiler
  patient_uuid: assigned_by_seeder
  visit_uuid: assigned_by_seeder
events:
  - id: deterioration
    trigger:
      kind: state_and_elapsed_time
      condition_ref: template.unresolved_clinical_problem
      timing_ref: template.reviewed_progression_bounds
    payload_ref: template.reviewed_deterioration
  - id: next_challenge
    trigger:
      kind: examiner_selection
      eligibility_ref: template.ready_for_next_challenge
      timing_ref: template.reviewed_challenge_window
    payload_ref: template.reviewed_additional_challenge
  - id: delayed_result
    trigger:
      kind: recorded_action
      action_ref: template.supported_test_order
    payload_ref: template.reviewed_result
intervention:
  policy_ref: rubric.reviewed_coaching_policy
  require_evidence: true
  require_pause_ack: true
cua:
  surface: read_only_review
  allowed_states: [paused, coaching, debrief]
  clinical_writes: false
```

The compiler must also resolve adaptation rules, event latency/preconditions, review approval and backend resource mappings. This excerpt is not enough to seed or run the case on its own.

## 12. Proposed examiner tools and evidence contract

These are our application tools exposed through Agents API, not built-in provider endpoints.

| Tool | Application-enforced responsibility |
|---|---|
| validate_case | Validate the draft and review references; compile immutable case/rubric bindings |
| start_run | Bind the participant, synthetic OpenMRS visit, mode and approved case |
| get_evidence | Return authorized observations, actions, screenshots and stable evidence IDs |
| propose_event | Validate performance evidence, event choice, difficulty and timing against the frozen adaptation policy/current state; let the engine publish it |
| pause_run | Freeze the controlled simulation and return acknowledgment only when quiescent |
| show_evidence | Authorize bounded CUA navigation to the run's read-only review surface |
| resume_run | Verify allowed mode/state, record assistance and issue a fresh execution version |
| submit_evaluation | Validate rubric/evidence references and append a provisional result version |

Each call carries run identity, authorization context and retry/idempotency information as appropriate. Tool handlers enforce scope and transitions; model instructions alone do not.

Astra has no arbitrary database-write or grader-edit tool. The doctor or candidate never receives examiner credentials, unreleased events or hidden scoring instructions.

The evidence timeline records actor, action, source, simulation and wall-clock timestamps, before/after references, case version and delivery status. Use server-recorded state as the execution source of truth; screenshots supplement it.

## 13. Secondary mode: evaluating AI agents

Later, substitute a browser/API agent for the human participant while retaining the configured OpenMRS environment, case engine, evidence contract and examiner.

Give the candidate a separate session and permissions—even if it also uses Astra. It cannot access hidden case state, examiner messages or grading tools.

Computer use by an AI candidate is separate from our examiner's CUA evidence-review feature. Do not conflate the two.

For the hackathon, define the adapter boundary but prioritize the complete doctor loop. Label any AI-agent stub or scripted participant; do not present it as a completed independent evaluation.

Do not compare raw human and agent scores without accounting for input modality, notification delivery, interaction latency, assistance and case path.

## 14. MVP and five-hour build sequence

One synthetic case, one specialist, one meaningful deterioration, one delayed result, one evidenced coaching intervention, one CUA navigation and one two-way teach-back.

No multi-model leaderboard, ten-case generator, hospital-wide deployment, custom EMR or research-level validation in the MVP.

| Window | Work | Exit evidence |
|---|---|---|
| 0:00–0:45 | Verify access; pin/configure minimum OpenMRS ED locations, visit/forms and accounts; establish real Live and Agents API connections | Synthetic chart reachable; voice exchange and actual examiner tool round trip |
| 0:45–1:45 | Finish the single ED case mapping, seed, event injection and evidence capture | Triage/assessment saved; one result durably published and visible |
| 1:45–2:45 | Add Astra rubric evaluation, pause protocol and client-delegation updates | Evidence-linked finding; clock/write/audio pause verified |
| 2:45–3:45 | Add read-only CUA navigation, spoken explanation, teach-back and resume | Complete doctor → event → pause → show → teach → resume loop |
| 3:45–5:00 | Run smoke checks, handle disconnect/stale work, deploy and record | Working deployed prototype and 90-second demonstration |

The [event listing](https://luma.com/fdzbrq5b) specifies a five-hour build window, 10:30–15:30 Singapore on 13 September 2026, and a deployed prototype plus 90-second video. Treat this as a target sequence, not a claim that setup or clinical review is already complete.

Use Astra throughout development as required by the event, and retain actual build evidence. Confirm model/runtime access early. If a core API is unavailable, disclose that dependency rather than substituting another runtime and claiming the same integration.

## 15. Demo and verification

The doctor uses OpenMRS; the evaluator panel shows running/paused state and the evidence timeline. Keep hidden rubric details off the doctor's active assessment view.

Suggested 90-second demo:

- 0–15s: introduce the doctor-first purpose and show the configured OpenMRS ED case.
- 15–35s: doctor speaks/acts; a confirmed new result or deterioration arrives.
- 35–55s: Astra identifies an evidence-supported concern and the simulation pauses.
- 55–75s: CUA opens the exact evidence; Live explains and the doctor responds.
- 75–90s: show teach-back/resume, the assisted label and examiner session/tool trace.

Do not depend on an unsuspecting participant making a particular error. A disclosed scripted demonstration of a known mistake is acceptable demo staging, not independent performance evidence.

Verify the OpenMRS exit checks in section 5 plus: uninterrupted two-way voice, delayed event delivery, no hidden-rubric leakage, model/tool failure pause, rejected stale writes, read-only CUA denial and separate pre/post-coaching evidence.

Verify adaptation with two recorded paths from the same starting case: effective management and a struggling participant. Show different justified event/pacing choices, unchanged rubric criteria, and clinical consequences consistent with each path. Label scripted test paths as test fixtures, not evidence of real doctors' performance.

A connected session, passing unit test or screenshot alone is not end-to-end proof. Capture the real API/session flow, recorded action, resulting chart state and spoken explanation.

## 16. Boundaries and remaining decisions

Settled: OpenMRS 3 stays; doctors are primary; AI agents are secondary; Astra on Agents API evaluates; GPT-Live-1 converses; CUA shows evidence only during review.

Before building, confirm the installed OpenMRS distribution, available order/result workflows, approved clinical case/rubric, model access and recording policy. Unsupported features stay visibly out of scope.

A complete ED information system, validated automated triage, physiological simulation, exam certification and production deployment are not implied by this prototype.

Next implementation step: configure and verify the single synthetic OpenMRS ED workflow, then connect the live examiner loop. This plan update itself makes no live-system changes.
