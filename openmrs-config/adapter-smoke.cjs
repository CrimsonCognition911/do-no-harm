// Usage: node adapter-smoke.cjs /path/to/pinned/emr-webmcp (after its locked install).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const checkout = path.resolve(process.argv[2]);
assert.equal(execFileSync('git', ['rev-parse', 'HEAD'], {cwd: checkout, encoding: 'utf8'}).trim(),
  '323cc14ac09678bbdefa4950d4c584fba9e50b46');
const esbuild = require(path.join(checkout, 'node_modules/esbuild'));
const built = esbuild.buildSync({entryPoints: [path.join(checkout, 'packages/adapters/openmrs/src/openmrs-adapter.ts')],
  bundle: true, platform: 'node', format: 'cjs', write: false});
const compiled = {exports: {}};
new Function('module', 'exports', built.outputFiles[0].text)(compiled, compiled.exports);
const {createOpenmrsAdapter} = compiled.exports;
const runtime = path.resolve(__dirname, '../runs/openmrs');
const manifest = JSON.parse(fs.readFileSync(path.join(runtime, 'manifest.json')));
const credentials = JSON.parse(fs.readFileSync(path.join(runtime, 'credentials.json')));
const authorization = 'Basic ' + Buffer.from('dnh-review:' + credentials.review).toString('base64');
const base = new URL(manifest.base_url);
assert(['127.0.0.1', 'localhost'].includes(base.hostname), 'This live probe is local only');
const transport = async (resource, init = {}) => {
  assert.equal(init.method ?? 'GET', 'GET', 'Compatibility probe is read-only');
  assert(resource.startsWith('/ws/'));
  const response = await fetch(manifest.base_url + resource, {headers: {Authorization: authorization}, redirect: 'error'});
  return {status: response.status, data: await response.json()};
};
(async () => {
  const adapter = createOpenmrsAdapter({fetch: transport, getActivePatientId: () => manifest.seed.patient_uuid,
    canCreateFollowup: () => false});
  const active = await adapter.getActivePatient();
  assert.equal(active.id, manifest.seed.patient_uuid);
  const matches = await adapter.searchPatients('DNH', 20);
  assert(matches.some(p => p.id === active.id));
  const obs = await transport('/ws/rest/v1/obs?patient=' + active.id + '&concept=' + manifest.numeric_concepts.haemoglobin.uuid);
  assert.equal(obs.status, 200);
  assert.equal(obs.data.results.length, 1);
  const result = await adapter.getResult(obs.data.results[0].uuid);
  assert.equal(result.patient.id, active.id);
  assert.equal(result.value, '12');
  assert.equal(result.unit, 'g/dL');
  console.log('PASS pinned adapter: patient search, active patient, FHIR result value/unit/patient mapping as review identity');
})().catch(error => {console.error(error); process.exitCode = 1;});
