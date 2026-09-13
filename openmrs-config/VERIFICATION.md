# DNH-02 red/green evidence

Verified locally on 2026-09-13 against the installation in `versions.lock.json`. This is software workflow verification with synthetic data, not clinical validation.

| Gate | Red evidence | Final green evidence |
|---|---|---|
| Offline ED behavior | Six tests failed before the configurator existed | Seven ED tests plus six existing scaffold tests pass |
| Metadata drift protection | Wrong-parent test failed against the first implementation | Wrong-parent metadata now raises instead of being accepted |
| Live ED workflow | Missing seed caused seven errors across four test methods; published-schema test already passed | All five live methods pass, including all four encounter/form types |
| Actual browser | Missing read permissions caused medication 403 and FHIR vitals 500 responses | Temperature, triage/assessment saves, single haemoglobin result, separate review session and active-visit list all pass with no failed browser responses |
| Repeatability | Not assumed from metadata names | Second configure returned an identical manifest and unchanged location, visit type, encounter type, form, role, user, patient and visit counts |
| Result publication | Not assumed from a successful POST | Repeated publication resolves the same encounter; one result renders in O3 |
| Adapter reuse | Old deployment health not assumed | 36 upstream adapter tests pass; live probe verifies patient search, active patient and FHIR result value/unit/patient binding as review |
| Version pinning | Release tag alone not treated as installed inventory | Installed module/import/dictionary/image inventory matches the lock; standalone Compose validates |

Review denial probes use valid payloads for encounter creation, observation creation, visit update and patient deletion. Only HTTP 401/403 counts as denial, not a validation failure. The account also succeeds at authenticated patient/form/result reads. Doctor and simulation encounter creators are separately verified through server read-back.

The upstream adapter was checked at `323cc14ac09678bbdefa4950d4c584fba9e50b46`. Its five test files / 36 tests were run with its installed Vitest binary from `packages/adapters/openmrs`. Live adapter coverage is limited to the read operations listed above.

Local evidence is in ignored `runs/openmrs/`: runtime and metadata manifests, `doctor-results.png`, `review-results.png`, and `active-visits.png`. No credentials or patient/run records are committed. Tests intentionally preserve their synthetic encounter history.

Reproduction commands and supported API mappings are in [README.md](README.md). Clinical approval, order linkage, simulation timing/pause and custom queue transitions remain outside this synthetic fixture, as disclosed there.
