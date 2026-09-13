"""Additive ED workflow fixture for O3 3.7.1. No clinical protocol is implied."""

import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from uuid import uuid4

OWNER = "DO NO HARM ED workflow fixture v1; synthetic only; clinical review pending"
HERE = Path(__file__).resolve().parent


def timestamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000%z")


FIELDS = {
    "acuity": "Clinician-entered acuity (no automatic score)",
    "complaint": "Presenting complaint",
    "assessment": "ED assessment and examination",
    "reassessment": "Reassessment findings",
    "disposition": "Disposition plan",
}
FORM_FIELDS = {
    "Triage": ["acuity", "complaint"],
    "ED Assessment": ["assessment"],
    "Reassessment": ["reassessment"],
    "Disposition": ["disposition"],
}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self, base, username, password):
        parts = urlsplit(base)
        if (parts.scheme not in ("http", "https") or not parts.hostname
                or parts.username or parts.password or parts.query or parts.fragment):
            raise ValueError("Use an OpenMRS base URL without credentials or query")
        if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("Non-loopback OpenMRS requires HTTPS")
        self.base = base.rstrip("/")
        self.authorization = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        self.opener = build_opener(NoRedirect())

    def request(self, method, path, body=None, *, rest=True):
        if path.startswith("/") or ":" in path or ".." in path or "\\" in path:
            raise ValueError("Only relative API paths are allowed")
        data = None if body is None else json.dumps(body).encode()
        request = Request(self.base + ("/ws/rest/v1/" if rest else "/") + path,
                          data=data, method=method,
                          headers={"Authorization": self.authorization, "Content-Type": "application/json"})
        with self.opener.open(request, timeout=60) as response:
            raw = response.read()
            return json.loads(raw) if raw else None

    def all(self, resource):
        results = []
        for start in range(0, 10000, 100):
            join = "&" if "?" in resource else "?"
            page = self.request("GET", f"{resource}{join}v=full&limit=100&startIndex={start}")
            results.extend(page["results"])
            if not any(link["rel"] == "next" for link in page.get("links", [])):
                return results
        raise ValueError("Resource pagination exceeded safety limit")

    def upload_schema(self, schema):
        boundary = "dnh" + secrets.token_hex(16)
        data = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="form.json"\r\n'
                'Content-Type: application/json\r\n\r\n' + json.dumps(schema) + f'\r\n--{boundary}--\r\n').encode()
        request = Request(self.base + "/ws/rest/v1/clobdata", data=data, method="POST", headers={
            "Authorization": self.authorization, "Content-Type": "multipart/form-data; boundary=" + boundary})
        with self.opener.open(request, timeout=60) as response:
            return response.read().decode().strip().strip('"')


def name_of(item):
    name = item.get("name", item.get("display"))
    return name.get("name") if isinstance(name, dict) else name


def ensure(client, resource, name, fields):
    kind = resource.split("?")[0]
    matches = [x for x in client.all(resource) if name_of(x) == name]
    if len(matches) > 1:
        raise ValueError(f"Ambiguous {resource}: {name}")
    if matches:
        obj = matches[0]
        description = obj.get("description")
        if kind == "concept":
            description = next((x["description"] for x in obj.get("descriptions", []) if x["locale"] == "en"), None)
        if description != OWNER:
            raise ValueError(f"Refusing to adopt unowned {resource}: {name}")
        if obj.get("retired"):
            raise ValueError(f"Owned metadata is retired: {name}")
        for key, expected in fields.items():
            if key in ("published", "privileges", "inheritedRoles"):
                continue
            actual = obj.get(key)
            if isinstance(actual, dict):
                actual = actual.get("uuid")
            elif isinstance(actual, list):
                actual = sorted(x["uuid"] for x in actual)
                expected = sorted(expected)
            if actual != expected:
                raise ValueError(f"Metadata drift in {resource} {name}: {key}")
        return obj
    payload = {"name": name, "description": OWNER, **fields}
    if kind == "concept":
        payload.pop("name")
        payload.pop("description")
        payload.update(names=[{"name": name, "locale": "en", "localePreferred": True,
                               "conceptNameType": "FULLY_SPECIFIED"}],
                       descriptions=[{"description": OWNER, "locale": "en"}])
    return client.request("POST", resource.split("?")[0], payload)


def lookup(client, resource, name):
    matches = [x for x in client.all(resource) if name_of(x) == name]
    if len(matches) != 1:
        raise ValueError(f"Expected one installed {resource}: {name}")
    return matches[0]["uuid"]


def role_privileges(identity):
    read = ["Get " + item for item in (
        "Patients", "People", "Patient Identifiers", "Visits", "Visit Types", "Encounters",
        "Encounter Types", "Encounter Roles", "Observations", "Concepts", "Forms", "Locations",
        "Providers", "Orders", "Order Types", "Order Frequencies", "Allergies", "Conditions",
        "Diagnoses", "Medication Dispense", "Global Properties", "Identifier Types", "Users",
        "Care Settings", "Queue Entries", "Queues", "Concept Sources", "Concept Reference Terms", "Concept Map Types")]
    if identity == "review":
        return read
    if identity == "simulation":
        return read + ["Add Encounters", "Add Observations"]
    if identity == "doctor":
        return read + ["Add Encounters", "Edit Encounters", "Add Observations", "Edit Observations", "Form Entry"]
    raise ValueError("Unknown identity")


def make_form(name, encounter, concepts):
    questions = [{"label": FIELDS[key], "type": "obs", "required": True, "id": key,
                  "questionOptions": {"rendering": "textarea", "concept": concepts[key]}}
                 for key in FORM_FIELDS[name] if key in concepts]
    return {"name": "DNH " + name, "description": OWNER, "version": "1", "published": True,
            "encounter": encounter, "processor": "EncounterFormProcessor", "referencedForms": [],
            "pages": [{"label": name, "sections": [{"label": "Synthetic workflow fixture",
                                                       "isExpanded": True, "questions": questions}]}]}


def configure(client, previous=None):
    if previous and previous.get("base_url", client.base) != client.base:
        raise ValueError("The runtime manifest belongs to another OpenMRS instance")
    check_inherited_privileges(client)
    manifest = {"schema_version": 1, "configuration_version": "1.0.0", "clinical_review": "pending",
                "fixture_only": True, "locations": {}, "encounter_types": {}, "forms": {}, "concepts": {}}
    tags = [lookup(client, "locationtag", name) for name in ("Login Location", "Visit Location")]
    parent = ensure(client, "location", "Emergency Department", {"tags": tags})["uuid"]
    manifest["locations"]["Emergency Department"] = parent
    for name in ("Triage", "Resuscitation", "Observation"):
        manifest["locations"][name] = ensure(client, "location", name, {"parentLocation": parent})["uuid"]
    manifest["visit_type"] = ensure(client, "visittype", "Emergency Visit", {})["uuid"]
    datatype = lookup(client, "conceptdatatype", "Text")
    concept_class = lookup(client, "conceptclass", "Question")
    for key, label in FIELDS.items():
        obj = ensure(client, "concept?q=" + urlencode({"q": "DNH " + label})[2:], "DNH " + label,
                     {"datatype": datatype, "conceptClass": concept_class})
        manifest["concepts"][key] = obj["uuid"]
    for name in FORM_FIELDS:
        encounter = ensure(client, "encountertype", "DNH " + name, {})["uuid"]
        manifest["encounter_types"][name] = encounter
        form = ensure(client, "form", "DNH " + name, {"version": "1", "published": False,
                                                      "encounterType": encounter})
        schema = make_form(name, encounter, manifest["concepts"])
        resources = client.all(f"form/{form['uuid']}/resource")
        existing = [r for r in resources if r["name"] == "JSON schema"]
        if not existing:
            reference = client.upload_schema(schema)
            client.request("POST", f"form/{form['uuid']}/resource", {
                "name": "JSON schema", "dataType": "AmpathJsonSchema", "valueReference": reference})
        elif len(existing) != 1 or client.request("GET", "clobdata/" + existing[0]["valueReference"]) != schema:
            raise ValueError(f"Published form schema drift: {name}")
        if not form["published"]:
            client.request("POST", f"form/{form['uuid']}", {"published": True})
        manifest["forms"][name] = form["uuid"]
    manifest["identities"] = configure_identities(client)
    manifest["encounter_role"] = lookup(client, "encounterrole", "Clinician")
    manifest["base_url"] = client.base
    manifest["run_key"] = (previous or {}).get("run_key", uuid4().hex)
    manifest["seed"] = seed(client, manifest, (previous or {}).get("seed"))
    manifest["numeric_concepts"] = {}
    for key, concept_name, units in (("temperature", "Temperature (c)", "DEG C"), ("haemoglobin", "Haemoglobin", "g/dL")):
        uuid = lookup(client, "concept?q=" + urlencode({"q": concept_name})[2:], concept_name)
        concept = client.request("GET", "concept/" + uuid + "?v=full")
        if concept["datatype"]["display"] != "Numeric" or concept["units"] != units:
            raise ValueError("Unexpected installed numeric concept mapping")
        manifest["numeric_concepts"][key] = {"uuid": uuid, "units": units, "datatype": "Numeric"}
    return manifest


def seed(client, manifest, previous):
    if previous:
        patient = client.request("GET", "patient/" + previous["patient_uuid"])
        visit = client.request("GET", "visit/" + previous["visit_uuid"])
        if (patient["voided"] or visit["voided"] or visit["stopDatetime"]
                or visit["patient"]["uuid"] != patient["uuid"]
                or patient["person"]["display"] != "DNH SYNTHETIC"
                or visit["visitType"]["uuid"] != manifest["visit_type"]
                or visit["location"]["uuid"] != manifest["locations"]["Emergency Department"]):
            raise ValueError("Previous fixture is no longer active; use a fresh output for a new run")
        return previous
    identifier_type = lookup(client, "patientidentifiertype", "OpenMRS ID")
    run_identifier = "DNH-" + manifest["run_key"]
    matches = [p for p in client.all("patient?q=" + run_identifier)
               if any(i["identifier"] == run_identifier for i in p["identifiers"])]
    if len(matches) > 1:
        raise ValueError("Multiple patients have this fixture run identifier")
    if matches:
        patient = matches[0]
    else:
        generated = client.request("POST", "idgen/identifiersource/" +
                               lookup(client, "idgen/identifiersource", "Generator for OpenMRS ID") +
                               "/identifier", {"source": "DNH synthetic workflow fixture"})
        patient = client.request("POST", "patient", {
          "person": {"names": [{"givenName": "DNH", "familyName": "SYNTHETIC"}], "gender": "U",
                   "birthdate": "1990-01-01"},
          "identifiers": [{"identifier": generated["identifier"], "identifierType": identifier_type,
                           "location": manifest["locations"]["Emergency Department"], "preferred": True},
                          {"identifier": run_identifier, "identifierType": lookup(client, "patientidentifiertype", "Legacy ID"),
                           "location": manifest["locations"]["Emergency Department"]}]})
    visits = [v for v in client.all("visit?patient=" + patient["uuid"]) if not v["stopDatetime"]
              and v["visitType"]["uuid"] == manifest["visit_type"]]
    if len(visits) > 1:
        raise ValueError("Multiple active fixture visits")
    visit = visits[0] if visits else client.request("POST", "visit", {"patient": patient["uuid"], "visitType": manifest["visit_type"],
                                             "location": manifest["locations"]["Emergency Department"],
                                             "startDatetime": timestamp()})
    return {"patient_uuid": patient["uuid"], "visit_uuid": visit["uuid"], "fixture_only": True}


def check_inherited_privileges(client):
    roles = {r["uuid"]: r for r in client.all("role")}
    pending = [r for r in roles.values() if r["name"] in ("Anonymous", "Authenticated")]
    visited = set()
    while pending:
        role = pending.pop()
        if role["uuid"] in visited:
            continue
        visited.add(role["uuid"])
        for privilege in role["privileges"]:
            name = privilege["name"]
            if not name.startswith(("Get ", "View ", "Patient Overview - View ")):
                raise ValueError("Inherited base role has a non-read privilege: " + name)
        pending.extend(roles[r["uuid"]] for r in role["inheritedRoles"])


def configure_identities(client):
    privileges = {p["name"]: p["uuid"] for p in client.all("privilege")}
    identities = {}
    for identity in ("doctor", "simulation", "review"):
        names = role_privileges(identity)
        role = ensure(client, "role", "DNH " + identity, {"privileges": [privileges[p] for p in names], "inheritedRoles": []})
        if {p["name"] for p in role.get("privileges", [])} != set(names) or role.get("inheritedRoles"):
            role = client.request("POST", "role/" + role["uuid"], {
                "privileges": [privileges[p] for p in names], "inheritedRoles": []})
        username = "dnh-" + identity
        users = [u for u in client.all("user?q=" + username) if u["username"] == username]
        if users:
            user = users[0]
            if {r["uuid"] for r in user["roles"]} != {role["uuid"]}:
                raise ValueError(f"Unexpected roles for {username}")
        else:
            password = os.environ["DNH_" + identity.upper() + "_PASSWORD"]
            person = client.request("POST", "person", {"names": [{"givenName": "Synthetic", "familyName": "DNH " + identity}], "gender": "U"})
            user = client.request("POST", "user", {"username": username, "password": password,
                                                   "person": person["uuid"], "roles": [role["uuid"]]})
        entry = {"user_uuid": user["uuid"], "role_uuid": role["uuid"], "privileges": names}
        if identity != "review":
            providers = [p for p in client.all("provider?q=" + username) if p.get("identifier") == username]
            if len(providers) > 1 or (providers and (
                    providers[0].get("retired") or
                    (providers[0].get("person") or {}).get("uuid") != user["person"]["uuid"])):
                raise ValueError(f"Provider identity drift for {username}")
            provider = providers[0] if providers else client.request("POST", "provider", {
                "person": user["person"]["uuid"], "identifier": username})
            entry["provider_uuid"] = provider["uuid"]
        identities[identity] = entry
    return identities


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8090/openmrs")
    parser.add_argument("--synthetic-instance", action="store_true", required=True,
                        help="Assert this target contains synthetic data only")
    parser.add_argument("--output", type=Path, default=HERE.parent / "runs" / "openmrs" / "manifest.json")
    args = parser.parse_args()
    credentials_path = HERE.parent / "runs" / "openmrs" / "credentials.json"
    credentials_path.parent.mkdir(parents=True, exist_ok=True)
    if not credentials_path.exists():
        with os.fdopen(os.open(credentials_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
            json.dump({identity: os.environ.get("DNH_" + identity.upper() + "_PASSWORD") or secrets.token_urlsafe(24) + "aA1!"
                       for identity in ("doctor", "simulation", "review")}, stream)
    credentials = json.loads(credentials_path.read_text())
    for identity, password in credentials.items():
        os.environ["DNH_" + identity.upper() + "_PASSWORD"] = password
    client = Client(args.base_url, os.environ["DNH_ADMIN_USERNAME"], os.environ["DNH_ADMIN_PASSWORD"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    previous = json.loads(args.output.read_text()) if args.output.exists() else {"run_key": uuid4().hex, "base_url": client.base}
    if not args.output.exists():
        args.output.write_text(json.dumps(previous, indent=2) + "\n")
    manifest = configure(client, previous)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2) + "\n")
    metadata = {k: v for k, v in manifest.items() if k not in ("seed", "identities", "base_url", "run_key")}
    metadata["roles"] = {name: {"uuid": identity["role_uuid"], "privileges": identity["privileges"]}
                         for name, identity in manifest["identities"].items()}
    (args.output.parent / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(f"Configured ED fixture; runtime mappings: {args.output}")


if __name__ == "__main__":
    main()
