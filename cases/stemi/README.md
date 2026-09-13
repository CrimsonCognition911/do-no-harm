# MI demo: The ECG cannot wait

**Draft for Dr. Sithira's clinical review. Not approved or enabled for scored use.**
All patient details, deterioration values and event timings are fictional design
choices. This is an educational simulation, not treatment guidance or proof of
specialist competence. [case.yaml](case.yaml) is an **authoring draft**, not input
accepted by the current compiled JSON planner. Clinical review, compilation and
OpenMRS mappings remain required.

## The story the audience sees

A 58-year-old synthetic patient arrives with chest pressure, sweating and nausea.
The doctor requests/reviews an ECG. Its released report indicates anterior STEMI.
The main decision is easy to understand: arrange urgent specialist reperfusion
care, or mistakenly wait for a blood test before escalating.

In the coaching demonstration, use an **actually recorded** delay decision, not
a prewritten accusation. After the acknowledged pause, CUA opens the separate
read-only ECG/timeline, and Live explains the concern and asks for teach-back.
The next attempt is visibly assisted. The ECG currently exists only as an
authored report: **no ECG image has been supplied or clinically reviewed**, so
do not claim demonstrated ECG-image understanding.

## Clinical anchors and local decisions

- A suspected ACS presentation warrants rapid ECG acquisition and interpretation;
  the 2025 US guideline specifies a ten-minute target. In an ECG-established
  STEMI, biomarkers should not hold up reperfusion. These are the core teaching
  anchors, not a substitute for contextual specialist judgment.
  [2025 ACC/AHA/ACEP/NAEMSP/SCAI ACS guideline](https://www.jacc.org/doi/10.1016/j.jacc.2024.11.009)
- Eligibility for reperfusion should be assessed immediately, and reperfusion
  delivered promptly. PCI is preferred when feasible within the relevant pathway;
  contraindications, expected delays and local capability affect the decision.
  Check aspirin eligibility and use the approved local treatment pathway. This
  demo deliberately excludes exact drug-dose and complex fibrinolysis scoring.
  [NICE NG185, recommendations 1.1.1–1.1.6](https://www.nice.org.uk/guidance/ng185/chapter/Recommendations)

Do not mix different guideline clock definitions into a single automatic score.
The local clinical reviewer must specify first-medical-contact, diagnosis,
activation, transfer and treatment timestamps, as well as acceptable alternatives.
The six-minute deterioration and five-minute synthetic laboratory turnaround in
the draft are **authored case mechanics**, not clinical predictions or guideline
thresholds. A single low blood pressure does not itself establish the mechanism
of shock. Calling cardiology does not restore coronary flow.

Source check: official publisher/NICE indexed recommendation text was checked on
13 September 2026. Direct full-text/PDF retrieval returned access errors during
this build; do not interpret this draft as a full guideline appraisal. The local
clinical reviewer must check the complete relevant guidance and local protocol
before signing off.

## Adaptive paths

| Observed situation | Examiner's permitted response | What must not happen |
|---|---|---|
| Recognizes STEMI and activates the supported pathway | Optional transfer-contingency question after its preconditions are met | Do not undo a confirmed transfer or fabricate treatment failure to make it harder. |
| Explicitly proposes waiting for troponin despite the available diagnostic ECG | Record a provisional, evidence-linked concern; coached mode may pause and teach | Do not accuse the doctor solely because a backend integration failed to emit a receipt. |
| Evidence is incomplete or ambiguous | Retain the current clinical path; seek neutral clarification if appropriate | No guessed failure, hidden hint or increase in difficulty based on uncertainty. |
| Unreperfused patient develops the predeclared hypotensive episode while still in the ED | Publish the reviewed clinical event when due and evaluate reassessment | No automatic punishment for a low score; correct initial management does not make later complications impossible. |

Assessment mode withholds feedback during the unassisted portion. Teaching ends
that portion and moves to debrief. Path/difficulty are reported; raw scores from
different paths are not assumed comparable.

## Draft rubric for the examiner

Use `acceptable`, `concern` or `insufficient_evidence`, with rationale, uncertainty,
evidence IDs and clinician-review requirement. No numeric pass/fail or licensing
claim. Store what was observable **at that moment**, not hindsight from a later
result. The table describes formative review areas, not a complete ACS protocol.

| Criterion | Evidence to inspect | Judgment and acceptable alternatives |
|---|---|---|
| `mi.initial_assessment` | Initial assessment, monitoring and ECG acquisition timeline | Appropriate urgency; distinguish requested ECG from completed acquisition. Account for workflow limitations and prehospital work. |
| `mi.ecg_interpretation` | Released ECG/report, display evidence and actual explanation/note | Recognizes the provided STEMI information and its significance. If no reviewed image exists, assess report interpretation only. |
| `mi.reperfusion_escalation` | Available ECG information, spoken plan and confirmed pathway activation | Appropriate urgent escalation; record whether an alternative is justified by the setting rather than demanding one exact phrase. Confirmed activation is not confirmed reperfusion. |
| `mi.initial_treatment_safety` | Relevant history, contraindications, proposed treatment and supported execution | Uses the approved local protocol with appropriate safety checks. Medication choice/dose details outside the reviewed scope are not automatically graded. Intent is never logged as administration. |
| `mi.reassessment` | New vitals and delivery timeline, examination, repeat information requests and escalation | Responds to changing symptoms/haemodynamics with a reasoned plan. Do not infer awareness solely from server publication or score an unseen result. |
| `mi.communication` | Explanation, actual handoff and teach-back | Clear urgency and shared plan. Record assistance; do not overwrite the pre-coaching trajectory. |

### Objective checks are not clinical verdicts

The backend may calculate ECG acquisition delay, event-delivery delay and pathway
activation delay from trusted timestamps. It may report missing evidence, stale
requests or duplicate delivery. These are observations. Astra's judgment about
clinical appropriateness stays separate and provisional; absent tooling must
yield unsupported/insufficient evidence, not an invented medical failure.

### Coaching evidence template

1. Identify the exact released ECG/report and the relevant doctor action/utterance.
2. Check whether the doctor had access to it and whether context explains the plan.
3. Pause and wait for write/audio quiescence acknowledgments.
4. Show that evidence in the read-only review surface; label live versus historical
   chart content correctly. If the evidence cannot be reached, disclose that.
5. Explain the supported concern, discuss alternatives and ask for teach-back.
6. Record assistance and resume only through a fresh execution version in coached
   mode; assessment proceeds to debrief instead.

Do not generate the doctor's alleged quotation. The teaching text must be filled
from observed evidence. Patient deterioration, clinician harm and an incorrect
decision must not be invented just to force a dramatic demo.

## Required before enabling this case

- [ ] Named clinical reviewer signs the case, rubric and adaptation-policy hashes.
- [ ] Review the ECG content; supply a licensed synthetic/teaching ECG image if visual interpretation is demonstrated.
- [ ] Select and version the local STEMI pathway, including availability, time definitions and alternatives.
- [ ] Approve fictional vitals, progression, relative timing and result values/assay/units.
- [ ] Resolve OpenMRS metadata and supported action/activation/handoff workflows.
- [ ] Compile the authoring YAML into a supported runtime contract; relative result delays and event effects are not yet supported by the current simple compiled planner.
- [ ] Verify durable publication, actual display, audio delivery and correct patient/visit binding.
- [ ] Verify both performance paths and a missing-evidence path against the frozen answer key.
- [ ] Verify operational failures pause rather than penalize, and CUA cannot write.

Review decision: **pending**. No reviewer, approved hash, clinical sign-off or
runtime integration has been fabricated.
