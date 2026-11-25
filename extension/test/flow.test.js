/**
 * @fileoverview Unit tests of the provisioning flow. Run with `npm test`
 * (plain Node 20+, no dependencies).
 */

import assert from 'node:assert/strict';
import {test} from 'node:test';

import {callBackend, isSafeRelativePath, isSelfTarget, redirect, runProvisioning, State} from '../src/flow.js';

const BASE_CONFIG = Object.freeze({
  backendUrl: 'https://backend.example.com/provision',
  transport: 'fetch',
  secretKeyName: '',
  redirectMode: 'reload',
  autoRedirect: true,
  maxAttempts: 4,
  retryDelayMs: 1500,
  requestTimeoutMs: 1000,
  fallbackPath: '/browse',
});

/** A successful backend answer as the extension SDK proxies return it. */
function ok(body) {
  return {ok: true, status: 200, body};
}

/**
 * Builds in-memory stand-ins for the Looker objects and the splash screen.
 * `responses` are returned (or thrown, for Error instances) by successive
 * backend calls.
 */
function makeFakes({
  responses = [],
  meFails = false,
  extensionId = 'provisioning_landing::splash',
} = {}) {
  const queue = [...responses];
  const fakes = {
    calls: [],
    locations: [],
    windows: [],
    states: [],
    logs: [],
    sleeps: [],
  };
  const proxy = (via) => async (url, init, bodyType) => {
    fakes.calls.push({via, url, init, bodyType});
    const next = queue.shift();
    if (next instanceof Error) throw next;
    return next;
  };
  fakes.extensionSDK = {
    lookerHostData: {extensionId},
    fetchProxy: proxy('fetch'),
    serverProxy: proxy('server'),
    createSecretKeyTag: (name) => `{{provisioning_landing_splash_${name}}}`,
    updateLocation: (...args) => {
      fakes.locations.push(args);
    },
    openBrowserWindow: (...args) => {
      fakes.windows.push(args);
    },
  };
  fakes.sdk = {
    me: async () => {
      if (meFails) throw new Error('session expired');
      return {id: '101'};
    },
    ok: (promise) => promise,
  };
  fakes.ui = {
    setState: (state, info) => {
      fakes.states.push(info === undefined ? [state] : [state, info]);
    },
    log: (message) => {
      fakes.logs.push(message);
    },
  };
  fakes.sleep = async (ms) => {
    fakes.sleeps.push(ms);
  };
  fakes.run = (config = {}) => runProvisioning({
    sdk: fakes.sdk,
    extensionSDK: fakes.extensionSDK,
    ui: fakes.ui,
    config: {...BASE_CONFIG, ...config},
    sleep: fakes.sleep,
  });
  return fakes;
}

test('redirects with a page load once the user is provisioned', async () => {
  const fakes = makeFakes(
      {responses: [ok({status: 'provisioned', redirect: '/dashboards/12'})]});

  const outcome = await fakes.run();

  assert.deepEqual(outcome, {state: 'redirecting', target: '/dashboards/12'});
  assert.deepEqual(fakes.locations, [['/dashboards/12', undefined, '_self']]);
  assert.deepEqual(fakes.states, [
    ['identifying'],
    ['provisioning', {attempt: 1}],
    ['redirecting', {target: '/dashboards/12'}],
  ]);
});

test('sends only the Looker user ID to the backend', async () => {
  const fakes = makeFakes(
      {responses: [ok({status: 'provisioned', redirect: '/dashboards/12'})]});

  await fakes.run();

  assert.equal(fakes.calls.length, 1);
  const [call] = fakes.calls;
  assert.equal(call.via, 'fetch');
  assert.equal(call.url, 'https://backend.example.com/provision');
  assert.equal(call.init.method, 'POST');
  assert.deepEqual(call.init.headers, {'Content-Type': 'application/json'});
  assert.deepEqual(JSON.parse(call.init.body), {user_id: '101'});
  assert.equal(call.bodyType, 'json');
});

test('navigates in-app when the redirect mode is spa', async () => {
  const fakes = makeFakes(
      {responses: [ok({status: 'provisioned', redirect: '/dashboards/12'})]});

  await fakes.run({redirectMode: 'spa'});

  assert.deepEqual(fakes.locations, [['/dashboards/12']]);
});

test('server transport adds the secret key tag, not a secret', async () => {
  const fakes = makeFakes(
      {responses: [ok({status: 'provisioned', redirect: '/dashboards/12'})]});

  await fakes.run({transport: 'server', secretKeyName: 'backend_secret'});

  assert.equal(fakes.calls[0].via, 'server');
  assert.deepEqual(JSON.parse(fakes.calls[0].init.body), {
    user_id: '101',
    secret: '{{provisioning_landing_splash_backend_secret}}',
  });
});

test('server transport without a secret key name sends no secret', async () => {
  const fakes = makeFakes(
      {responses: [ok({status: 'provisioned', redirect: '/dashboards/12'})]});

  await fakes.run({transport: 'server'});

  assert.deepEqual(JSON.parse(fakes.calls[0].init.body), {user_id: '101'});
});

test('fetch transport never sends a secret', async () => {
  const fakes = makeFakes(
      {responses: [ok({status: 'provisioned', redirect: '/dashboards/12'})]});

  await fakes.run({transport: 'fetch', secretKeyName: 'backend_secret'});

  assert.deepEqual(JSON.parse(fakes.calls[0].init.body), {user_id: '101'});
});

test('redirects a user who was provisioned earlier', async () => {
  const fakes = makeFakes({
    responses: [ok({status: 'already_provisioned', redirect: '/boards/3'})],
  });

  const outcome = await fakes.run();

  assert.deepEqual(outcome, {state: 'redirecting', target: '/boards/3'});
  assert.deepEqual(fakes.locations, [['/boards/3', undefined, '_self']]);
});

test('retries with growing waits until the backend succeeds', async () => {
  const fakes = makeFakes({
    responses: [
      new Error('network down'),
      ok({status: 'pending'}),
      ok({status: 'provisioned', redirect: '/dashboards/12'}),
    ],
  });

  const outcome = await fakes.run();

  assert.equal(outcome.state, 'redirecting');
  assert.equal(fakes.calls.length, 3);
  assert.deepEqual(fakes.sleeps, [1500, 3000]);
  assert.deepEqual(fakes.locations, [['/dashboards/12', undefined, '_self']]);
});

test('gives up after the configured number of attempts', async () => {
  const fakes = makeFakes({
    responses: [
      new Error('down 1'),
      new Error('down 2'),
      new Error('down 3'),
      new Error('down 4'),
    ],
  });

  const outcome = await fakes.run();

  assert.equal(outcome.state, 'failed');
  assert.equal(outcome.error.message, 'down 4');
  assert.equal(fakes.calls.length, 4);
  assert.deepEqual(fakes.sleeps, [1500, 3000, 4500]);
  assert.deepEqual(fakes.locations, []);
  assert.deepEqual(fakes.states.at(-1), ['failed', {error: outcome.error}]);
});

test('treats an HTTP error answer as a failed attempt', async () => {
  const fakes = makeFakes({responses: [{ok: false, status: 502, body: {}}]});

  const outcome = await fakes.run({maxAttempts: 1});

  assert.equal(outcome.state, 'failed');
  assert.equal(outcome.error.message, 'Backend answered HTTP 502');
  assert.deepEqual(fakes.locations, []);
});

test('treats an answer without status as a failed attempt', async () => {
  const fakes = makeFakes({responses: [ok({redirect: '/dashboards/12'})]});

  const outcome = await fakes.run({maxAttempts: 1});

  assert.equal(outcome.state, 'failed');
  assert.equal(outcome.error.message, 'Backend answer has no status');
  assert.deepEqual(fakes.locations, []);
});

test('fails an attempt when the backend does not answer in time', async () => {
  const fakes = makeFakes();
  fakes.extensionSDK.fetchProxy = () => new Promise(() => {});

  const outcome = await fakes.run({maxAttempts: 1, requestTimeoutMs: 10});

  assert.equal(outcome.state, 'failed');
  assert.equal(
      outcome.error.message, 'No answer from the backend after 10 ms');
});

for (const status of [400, 401, 403, 404, 422]) {
  test(`fails fast without retrying on HTTP ${status}`, async () => {
    const fakes = makeFakes({responses: [{ok: false, status, body: {}}]});

    const outcome = await fakes.run({maxAttempts: 4});

    assert.equal(outcome.state, 'failed');
    assert.equal(outcome.error.message, `Backend answered HTTP ${status}`);
    assert.equal(fakes.calls.length, 1);
    assert.deepEqual(fakes.sleeps, []);
  });
}

for (const status of ['no_entitlements', 'unknown_user', 'disabled', 'not_saml']) {
  test(`stops without redirect or retry on status ${status}`, async () => {
    const fakes = makeFakes({responses: [ok({status, redirect: null})]});

    const outcome = await fakes.run();

    assert.deepEqual(outcome, {state: 'no_access'});
    assert.equal(fakes.calls.length, 1);
    assert.deepEqual(fakes.locations, []);
    assert.deepEqual(fakes.states.at(-1), ['no_access']);
  });
}

for (const unsafe of [
  'https://evil.example.com/',
  '//evil.example.com/',
  '/\\evil.example.com/',
  'javascript:alert(1)',
  'dashboards/12',
  '/dashboards/12 /x',
  '',
  null,
  42,
]) {
  test(`falls back when the destination is ${JSON.stringify(unsafe)}`,
      async () => {
        const fakes = makeFakes(
            {responses: [ok({status: 'provisioned', redirect: unsafe})]});

        const outcome = await fakes.run();

        assert.deepEqual(outcome, {state: 'redirecting', target: '/browse'});
        assert.deepEqual(fakes.locations, [['/browse', undefined, '_self']]);
      });
}

test('waits for the user when automatic redirect is off', async () => {
  const fakes = makeFakes(
      {responses: [ok({status: 'provisioned', redirect: '/dashboards/12'})]});

  const outcome = await fakes.run({autoRedirect: false});

  assert.deepEqual(outcome, {state: 'ready', target: '/dashboards/12'});
  assert.deepEqual(fakes.locations, []);
  assert.deepEqual(fakes.states.at(-1), ['ready', {target: '/dashboards/12'}]);
});

test('accepts a backend answer delivered as JSON text', async () => {
  const fakes = makeFakes({
    responses:
        [ok('{"status": "provisioned", "redirect": "/dashboards/12"}')],
  });

  const outcome = await fakes.run();

  assert.deepEqual(outcome, {state: 'redirecting', target: '/dashboards/12'});
});

test('fails without calling the backend when the user is unknown', async () => {
  const fakes = makeFakes({meFails: true});

  const outcome = await fakes.run();

  assert.equal(outcome.state, 'failed');
  assert.equal(outcome.error.message, 'session expired');
  assert.equal(fakes.calls.length, 0);
});

test('callBackend returns the decoded backend answer', async () => {
  const fakes = makeFakes(
      {responses: [ok({status: 'provisioned', redirect: '/dashboards/12'})]});

  const answer = await callBackend(fakes.extensionSDK, BASE_CONFIG, 101);

  assert.deepEqual(answer, {status: 'provisioned', redirect: '/dashboards/12'});
  assert.deepEqual(JSON.parse(fakes.calls[0].init.body), {user_id: '101'});
});

test('redirect asks for a page load in reload mode', () => {
  const fakes = makeFakes();

  redirect(fakes.extensionSDK, '/dashboards/12', 'reload');

  assert.deepEqual(fakes.locations, [['/dashboards/12', undefined, '_self']]);
});

test('redirect navigates in-app in spa mode', () => {
  const fakes = makeFakes();

  redirect(fakes.extensionSDK, '/dashboards/12', 'spa');

  assert.deepEqual(fakes.locations, [['/dashboards/12']]);
});

test('redirect opens a new tab in new_tab mode', () => {
  const fakes = makeFakes();

  redirect(fakes.extensionSDK, '/dashboards/12', 'new_tab');

  assert.deepEqual(fakes.windows, [['/dashboards/12', '_blank']]);
});

for (const selfPath of [
  '/extensions/provisioning_landing::splash',
  '/extensions/provisioning_landing::splash/',
  '/extensions/provisioning_landing::splash?retry=1',
  '/spartan/provisioning_landing::splash',
]) {
  test(`refuses to redirect back to itself (${selfPath})`, async () => {
    const fakes = makeFakes(
        {responses: [ok({status: 'provisioned', redirect: selfPath})]});

    const outcome = await fakes.run();

    assert.deepEqual(outcome, {state: 'redirecting', target: '/browse'});
    assert.deepEqual(fakes.locations, [['/browse', undefined, '_self']]);
  });
}

test('fails instead of looping when fallback also points to itself',
    async () => {
      const fakes = makeFakes({
        responses: [ok({
          status: 'provisioned',
          redirect: '/extensions/provisioning_landing::splash',
        })],
      });

      const outcome = await fakes.run(
          {fallbackPath: '/extensions/provisioning_landing::splash'});

      assert.equal(outcome.state, 'failed');
      assert.deepEqual(fakes.locations, []);
    });

test('isSelfTarget does not reject other extensions', () => {
  assert.equal(
      isSelfTarget(
          '/extensions/other_project::app', 'provisioning_landing::splash'),
      false);
  assert.equal(
      isSelfTarget(
          '/extensions/provisioning_landing::splash_v2',
          'provisioning_landing::splash'),
      false);
});

test('isSafeRelativePath accepts paths on the instance', () => {
  assert.equal(isSafeRelativePath('/dashboards/12'), true);
  assert.equal(isSafeRelativePath('/browse/boards/3?tab=1#top'), true);
  assert.equal(isSafeRelativePath('/extensions/a::b'), true);
  assert.equal(isSafeRelativePath('/'), true);
});

test('State lists the states of the splash screen', () => {
  assert.deepEqual(Object.values(State).sort(), [
    'failed',
    'identifying',
    'no_access',
    'provisioning',
    'ready',
    'redirecting',
  ]);
});
