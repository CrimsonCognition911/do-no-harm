// Run with Playwright installed; credentials and screenshots stay in ignored runs/.
const { chromium } = require('playwright');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const path = require('node:path');
const runtime = path.resolve(__dirname, '../runs/openmrs');
const manifest = JSON.parse(fs.readFileSync(path.join(runtime, 'manifest.json')));
const credentials = JSON.parse(fs.readFileSync(path.join(runtime, 'credentials.json')));

async function login(page, identity) {
  await page.goto(manifest.base_url + '/spa/login');
  await page.getByLabel('Username', {exact: true}).fill('dnh-' + identity);
  await page.getByRole('button', {name: 'Continue', exact: true}).click();
  await page.getByLabel('Password', {exact: true}).fill(credentials[identity]);
  await page.getByRole('button', {name: 'Log in', exact: true}).click();
  await page.getByRole('searchbox').fill('Emergency Department');
  await page.getByText('Emergency Department', {exact: true}).click();
  await page.getByRole('button', {name: 'Confirm', exact: true}).click();
  await page.goto(manifest.base_url + '/spa/patient/' + manifest.seed.patient_uuid + '/chart');
  await page.getByLabel('patient banner').getByText('DNH SYNTHETIC', {exact: true}).waitFor();
}

(async () => {
  const browser = await chromium.launch({headless: true,
    ...(process.env.CHROMIUM_PATH ? {executablePath: process.env.CHROMIUM_PATH} : {}),
    args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    const errors = [];
    const recordError = response => {if (response.status() >= 400) errors.push(response.status() + ' ' + new URL(response.url()).pathname);};
    page.on('response', recordError);
    await login(page, 'doctor');
    await page.getByText('37', {exact: true}).first().waitFor();
    console.log('PASS temperature renders in the installed vitals module');
    await page.getByRole('button', {name: 'Clinical forms', exact: true}).click();
    for (const [name, labels] of [
      ['Triage', ['Clinician-entered acuity (no automatic score)', 'Presenting complaint']],
      ['ED Assessment', ['ED assessment and examination']],
    ]) {
      await page.getByText('DNH ' + name, {exact: true}).click();
      for (const label of labels) await page.getByLabel(label, {exact: false}).fill('SYNTHETIC browser workflow fixture');
      const saving = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname.endsWith('/encounter'));
      await page.getByRole('button', {name: 'Save', exact: true}).click();
      const response = await saving;
      assert.equal(response.status(), 201);
      const saved = await response.json();
      const encounter = await page.evaluate(async uuid => (await fetch(window.openmrsBase + '/ws/rest/v1/encounter/' + uuid + '?v=full')).json(), saved.uuid);
      assert.equal(encounter.visit.uuid, manifest.seed.visit_uuid);
      assert.equal(encounter.patient.uuid, manifest.seed.patient_uuid);
      assert.equal(encounter.form.uuid, manifest.forms[name]);
      console.log('PASS browser saved ' + name + ' under the active visit');
      await page.getByRole('button', {name: 'Save', exact: true}).waitFor({state: 'hidden'});
    }
    await page.getByRole('button', {name: 'Clinical forms', exact: true}).click();
    await page.getByText('Results', {exact: true}).first().click();
    await page.getByText('12.0 g/dL', {exact: true}).waitFor();
    assert.equal(await page.getByText('12.0 g/dL', {exact: true}).count(), 1);
    await page.screenshot({path: path.join(runtime, 'doctor-results.png'), fullPage: true});
    console.log('PASS one haemoglobin result renders with g/dL units');
    const review = await browser.newPage({viewport: {width: 1440, height: 1000}});
    review.on('response', recordError);
    await login(review, 'review');
    await review.getByText('Results', {exact: true}).first().click();
    await review.getByText('12.0 g/dL', {exact: true}).waitFor();
    const session = await review.evaluate(async () => (await fetch(window.openmrsBase + '/ws/rest/v1/session')).json());
    assert.equal(session.user.uuid, manifest.identities.review.user_uuid);
    await review.screenshot({path: path.join(runtime, 'review-results.png'), fullPage: true});
    console.log('PASS separate review browser session opens the same result');
    await page.goto(manifest.base_url + '/spa/home');
    await page.getByText('Active Visits', {exact: true}).waitFor();
    const row = page.getByRole('row').filter({hasText: 'DNH SYNTHETIC'}).filter({hasText: 'Emergency Visit'});
    assert.equal(await row.count(), 1);
    await page.getByText('OpenMRS ID', {exact: true}).waitFor();
    await page.screenshot({path: path.join(runtime, 'active-visits.png'), fullPage: true});
    console.log('PASS synthetic Emergency Visit appears once in the active-visit list');
    assert.deepEqual(errors, [], 'Required browser requests must succeed');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
