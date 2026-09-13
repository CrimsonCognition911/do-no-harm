# OpenMRS emergency department — @tijoseymathew

Configuration work for [#2](https://github.com/CrimsonCognition911/do-no-harm/issues/2). This directory is a handoff area, **not an applied configuration pack**.

Keep OpenMRS 3. Inventory the installed distribution/modules first, and reuse compatible `emr-webmcp` code without assuming the old deployment still works.

Deliver versioned metadata and a smoke-test checklist covering:

- ED parent plus Triage, Resuscitation and Observation locations.
- Emergency Visit and Triage/ED Assessment/Reassessment/Disposition encounters/forms.
- One synthetic patient, its required concepts/units/results/orderables, and ED queue or active-visit view.
- Doctor, simulation-service and read-only review identities, with tested server-side permissions.
- Verified patient/visit/location/concept/resource mappings for @CrimsonSithria's event injection backend.

Use the detailed configuration and exit checks in section 5 of `grand_rounds_build_doc.md`. Do not invent UUIDs, triage thresholds or unsupported module capabilities. Keep credentials and patient/run data out of source. Never wipe a shared database. No changes to the existing deployment are made by this scaffold.
