# Synthetic OpenMRS ED configuration (DNH-02)

This pack configures O3 3.7.1 for a **synthetic workflow fixture**. Clinical review remains pending, as agreed for DNH-02. Forms record clinician-entered text without automated acuity, diagnosis, treatment, doses or scoring rules.

The workflow uses Emergency Department with Triage, Resuscitation and Observation child locations; an Emergency Visit; and DNH Triage, ED Assessment, Reassessment and Disposition encounters with published JSON forms. Disposition records a plan and does not automatically end a visit. The installed active-visit list is the ED list fallback; complaint and acuity remain in the Triage form, with no custom list columns or invented queue statuses.

## Setup

Python 3.11+ and Docker Compose are required. `compose.yml` pins all four images by digest and binds the gateway to loopback. `versions.lock.json` records actual backend/module versions, frontend imports and dictionary archive hashes.

For a fresh local installation, supply `OMRS_DB_PASSWORD` and `MYSQL_ROOT_PASSWORD` privately through your environment, then:

```sh
docker compose -f openmrs-config/compose.yml up -d
curl -f http://127.0.0.1:8090/openmrs/health/started
```

First initialization can take several minutes. Use the local distribution bootstrap administrator only for setup, supplying `DNH_ADMIN_USERNAME` and `DNH_ADMIN_PASSWORD` through your environment. This pack does not store or rotate the distribution bootstrap credential. For existing volumes, preserve their existing database passwords.

For the existing root compose stack, apply only the frontend overlay:

```sh
docker compose -f docker-compose.openmrs.yml -f openmrs-config/compose.overlay.yml up -d --no-deps frontend
```

The overlay assumes the repository root compose file is first. The standalone compose file works in a fresh checkout without that pre-existing root file. No command here drops tables, purges resources, removes volumes or resets a database. Use synthetic data only.

```sh
python3 openmrs-config/inventory.py
python3 openmrs-config/configure.py --synthetic-instance
python3 openmrs-config/publish-fixture.py
```

Random passwords for `dnh-doctor`, `dnh-simulation` and `dnh-review` are generated in ignored `runs/openmrs/credentials.json` with mode 0600. Existing passwords are retained on rerun. Retrieve them locally; never paste this file into GitHub. Log in as doctor or review and select Emergency Department.

Metadata uses exact names, an ownership description and server-issued UUIDs. Unowned collisions, retired metadata, incorrect parent/type mappings and changed form schemas fail instead of overwriting shared metadata. Owned DNH roles converge to explicit privileges; inherited Anonymous/Authenticated privileges are checked first.

The same output manifest reuses its patient and active visit. `--output runs/openmrs/another-run.json` starts a separate patient/visit and retains previous history. Persisted run identifiers recover partial patient/visit creation on sequential retries. Retain runtime manifests with their database. Run one configurator/publisher at a time; these scripts are not a concurrent event service. Default verification uses `runs/openmrs/manifest.json`. When selecting a new run,
pass the same manifest to the publisher and all verification commands:

```sh
python3 openmrs-config/configure.py --synthetic-instance --output runs/openmrs/another-run.json
python3 openmrs-config/publish-fixture.py --manifest runs/openmrs/another-run.json
DNH_MANIFEST=runs/openmrs/another-run.json python3 openmrs-config/test_live.py -v
DNH_MANIFEST=runs/openmrs/another-run.json node openmrs-config/browser-smoke.cjs
DNH_MANIFEST=runs/openmrs/another-run.json node openmrs-config/adapter-smoke.cjs /path/to/emr-webmcp
```

These paths are relative to the working directory. Credentials still come from
`runs/openmrs/credentials.json`; the publisher also accepts `--credentials` for an
explicit alternative. The browser list check identifies the selected patient by
UUID, so retained runs with the same synthetic display name do not collide.

## Workflow and tests

Open DNH SYNTHETIC from the active-visit list or patient search. Navigation exposes summary, vitals, results, visits, allergies, medications, orders and conditions. Use the Clinical forms button for the four DNH forms. Triage and ED Assessment save to the current visit.

The publisher adds one numeric encounter per run on sequential reruns: temperature 37 DEG C and haemoglobin 12 g/dL. These are software display fixtures, not clinical advice. It writes as the simulation user and retains server observation/creation timestamps. Haemoglobin is a directly entered result, not evidence of a placed or executed order. O3's installed reference ranges are not used for grading.

```sh
python3 -m unittest discover -s tests -v
python3 openmrs-config/test_live.py -v
python3 openmrs-config/inventory.py
node openmrs-config/browser-smoke.cjs
node openmrs-config/adapter-smoke.cjs /path/to/emr-webmcp
```

Install Playwright 1.62.1 and Chromium in local tooling for browser tests. `NODE_PATH` may point to that installation; `CHROMIUM_PATH` optionally selects an existing executable. Screenshots remain in ignored `runs/openmrs/`. Live/browser tests append synthetic encounters and exercise valid denied writes; they preserve history.

The adapter probe requires `CrimsonSithria/emr-webmcp` at `323cc14ac09678bbdefa4950d4c584fba9e50b46`, with its locked dependencies installed (`corepack yarn install --immutable`). It bundles the existing adapter unchanged and tests patient search, active-patient lookup and FHIR result mapping as review. This does not validate its old deployment, full chart-brief/task workflows or writes.

## DNH-06 handoff

`runs/openmrs/metadata.json` contains actual metadata UUIDs and role privileges. The runtime `manifest.json` additionally binds patient, visit, user and provider UUIDs. The checked-in `verified-metadata.json` is an installation snapshot, not portable IDs: use each target's generated manifest. It excludes patient, visit, user/provider identities and credentials.

| Operation | Supported mapping |
|---|---|
| Patient / visit | REST `patient/{uuid}`, `visit/{uuid}`; verify patient, visit type, location and null stopDatetime |
| Encounter write | REST `POST encounter` with patient, visit, encounterType, location, encounterDatetime, encounterProviders |
| Observation | Nested encounter `obs` or REST `POST obs`; standalone writes use person, concept, value, obsDatetime, encounter |
| Concept / units | REST `concept/{uuid}?v=full`; numeric mappings include installed datatype and units |
| Form | REST `form/{uuid}?v=full`; JSON schema resource points to `clobdata/{valueReference}` |
| Result review | REST observation read-back, FHIR2 `R4/Observation/{uuid}` and O3 Results |
| Authorship | `auditInfo.creator.uuid` and encounterProviders distinguish doctor and simulation |
| Review denial | REST encounter/observation create, visit update and patient delete are denied server-side |

Doctor can enter encounter/form observations; simulation can add encounters/observations; review has only explicit read privileges. Stock O3 can show mutation buttons to review users; backend denial is the boundary. Roles are instance-wide, not patient/run-scoped. DNH-06 must validate run bindings and enforce controlled access.

Unsupported here: automated triage, custom ED queue transitions, drug/orderable selection, order-result linkage, administration, physiology, delayed-event scheduling, pause enforcement, historical replay and clinical scoring. These await the approved case and other workstreams. Forms and fixture content require clinical review before a scored demonstration.

References: [O3 distribution](https://github.com/openmrs/openmrs-distro-referenceapplication), [O3 form builder](https://github.com/openmrs/openmrs-esm-form-builder), [pinned adapter](https://github.com/CrimsonSithria/emr-webmcp/tree/323cc14ac09678bbdefa4950d4c584fba9e50b46/packages/adapters/openmrs). Actual installed resources and browser behavior determined this configuration.
