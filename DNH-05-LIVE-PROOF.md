# DNH-05 real-provider proof

Date: 2026-09-13

This acceptance run used a synthetic local session and existing project credentials;
no credential or patient data was copied into the repository.

Observed path:

1. The doctor UI created a real `gpt-live-1` WebRTC session after explicit
   session-only microphone consent.
2. The application appended the approved synthetic STEMI assessment welcome
   only after `session.started`; reconnect suppression is covered separately.
3. A typed synthetic correction reached the application event feed while a
   privacy-safe instruction invalidated stale Live context. The correction text
   itself stayed in the application-owned delegation context.
4. A synthetic spoken request caused GPT-Live to emit a client delegation.
5. The BFF sent that request through `/api/examiner` to the saved
   `DO NO HARM Examiner` Astra agent.
6. Astra completed one verified `get_evidence` call and returned
   `needs_clarification`, grounded in three participant-visible evidence items.
7. The BFF returned only the allowlisted participant-safe response to GPT-Live.
8. The remote audio element remained unmuted and playing at readiness state 4;
   its playback clock advanced from 109.618 seconds to 114.628 seconds during a
   separate five-second observation.

A separate combined local run used the existing synthetic OpenMRS instance and
the authoritative runner. After trusted fixture confirmations, the runner wrote
and REST-read back three `DNH Reassessment` text observations: the anterior STEMI
ECG report, troponin above the local upper reference limit, and persistent pain
with light-headedness and clamminess. The installed O3 UI displayed the new
encounters under **Visits → All encounters**. This proves durable text publication
and chart rendering for this synthetic run; it does not implement automatic O3
refresh or production chart-action detection.

The real Astra call took about 72 seconds. The backend provider deadline is now
bounded at 120 seconds and the BFF deadline at 130 seconds. Timeout still requests
an unscored technical pause and late results cannot add evaluation findings.

Automated suites remain clearly separate from this provider proof. They use fake
provider streams and verify delegation correlation, private-finding filtering,
pause/reconnect behavior, stale-reply rejection, display acknowledgments, and the
separation between UI display and audio playback evidence.

This proves the synthetic voice-to-examiner-to-voice path and synthetic OpenMRS
text-publication path were available in local runs. It does not validate clinical
quality, production authentication, durable provider recovery, or a real patient
workflow.
