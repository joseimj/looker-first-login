/**
 * @fileoverview Unit tests covering BOTH SMB ('direct') and Enterprise
 * ('passive_poll') modes, marker groups, ?next= deep links, loop guards, and
 * redirect modes. Run with `npm test` (Node 20+, zero dependencies).
 */

import assert from 'node:assert/strict';
import {test} from 'node:test';

import {classifyGroups, extractNextParam, isSafeRelativePath, isSelfTarget, redirect, runProvisioning} from '../src/flow.js';

const DIRECT_CFG = Object.freeze({
  mode: 'direct',
  backendUrl: 'https://backend.example.com/provision',
  transport: 'fetch',
  secretKeyName: '',
  redirectMode: 'reload',
  autoRedirect: true,
  maxAttempts: 4,
  retryDelayMs: 1000,
  requestTimeoutMs: 1000,
  fallbackPath: '/browse',
});

const PASSIVE_CFG = Object.freeze({
  mode: 'passive_poll',
  hubPath: '/browse/boards/3',
  provisionedGroupIds: ['21'],
  ignoredGroupIds: ['1', '10'],
  pendingMarkerGroupId: '99',
  pollIntervalsMs: [100, 200, 400],
  redirectMode: 'reload',
  autoRedirect: true,
  fallbackPath: '/browse',
});

function makeFakes({
  meSequence = [{id: '101', group_ids: ['1', '10']}],
  responses = [],
  extensionId = 'provisioning_landing::splash',
} = {}) {
  const meQueue = [...meSequence];
  const respQueue = [...responses];
  const fakes = {calls: [], locations: [], windows: [], states: [], sleeps: [], logs: []};
  const proxy = (via) => async (url, init, bodyType) => {
    fakes.calls.push({via, url, init, bodyType});
    const next = respQueue.shift();
    if (next instanceof Error) throw next;
    return next;
  };
  fakes.extensionSDK = {
    lookerHostData: {extensionId},
    fetchProxy: proxy('fetch'),
    serverProxy: proxy('server'),
    createSecretKeyTag: (name) => `{{provisioning_landing_splash_${name}}}`,
    updateLocation: (...args) => fakes.locations.push(args),
    openBrowserWindow: (...args) => fakes.windows.push(args),
  };
  fakes.sdk = {
    me: async () => {
      const next = meQueue.length > 1 ? meQueue.shift() : meQueue[0];
      if (next instanceof Error) throw next;
      return next;
    },
    ok: (p) => p,
  };
  fakes.ui = {
    setState: (s, i) => fakes.states.push(i === undefined ? [s] : [s, i]),
    log: (m) => fakes.logs.push(m),
  };
  fakes.sleep = async (ms) => fakes.sleeps.push(ms);
  return fakes;
}

test('passive_poll (Enterprise): polls sdk.me() with zero external calls and redirects when Hub group lands', async () => {
  const fakes = makeFakes({
    meSequence: [
      {id: '101', group_ids: ['1', '10']},
      {id: '101', group_ids: ['1', '10']},
      {id: '101', group_ids: ['1', '10', '21']},
    ],
  });

  const outcome = await runProvisioning({
    sdk: fakes.sdk,
    extensionSDK: fakes.extensionSDK,
    ui: fakes.ui,
    config: PASSIVE_CFG,
    sleep: fakes.sleep,
    random: () => 0.5,
  });

  assert.deepEqual(outcome, {state: 'redirecting', target: '/browse/boards/3'});
  assert.equal(fakes.calls.length, 0);
  assert.deepEqual(fakes.sleeps, [100, 200]);
  assert.deepEqual(fakes.locations, [['/browse/boards/3', undefined, '_self']]);
});

test('passive_poll: stops immediately on pendingMarkerGroupId (BI_Access_Pending_Approval)', async () => {
  const fakes = makeFakes({
    meSequence: [
      {id: '103', group_ids: ['1', '10']},
      {id: '103', group_ids: ['1', '10', '99']},
    ],
  });

  const outcome = await runProvisioning({
    sdk: fakes.sdk,
    extensionSDK: fakes.extensionSDK,
    ui: fakes.ui,
    config: PASSIVE_CFG,
    sleep: fakes.sleep,
    random: () => 0.5,
  });

  assert.deepEqual(outcome, {state: 'no_access'});
  assert.deepEqual(fakes.sleeps, [100]);
  assert.deepEqual(fakes.locations, []);
});

test('passive_poll: supports ?next= deep link from enterprise portal', async () => {
  const fakes = makeFakes({
    meSequence: [{id: '101', group_ids: ['1', '10', '21']}],
  });

  const outcome = await runProvisioning({
    sdk: fakes.sdk,
    extensionSDK: fakes.extensionSDK,
    ui: fakes.ui,
    config: PASSIVE_CFG,
    nextPath: extractNextParam('/?next=%2Fdashboards%2F42'),
    sleep: fakes.sleep,
  });

  assert.deepEqual(outcome, {state: 'redirecting', target: '/dashboards/42'});
  assert.deepEqual(fakes.sleeps, []);
});

test('passive_poll: detects any new non-ignored group when provisionedGroupIds is empty', () => {
  const cfg = {...PASSIVE_CFG, provisionedGroupIds: []};
  const baseline = new Set(['1', '10']);

  assert.equal(classifyGroups(['1', '10'], cfg, baseline), 'pending');
  assert.equal(classifyGroups(['1', '10', '99'], cfg, baseline), 'no_access');
  assert.equal(classifyGroups(['1', '10', '20'], cfg, baseline), 'ready');
});

test('passive_poll: fails gracefully with Retry when polls exhaust', async () => {
  const fakes = makeFakes({meSequence: [{id: '101', group_ids: ['1', '10']}]});

  const outcome = await runProvisioning({
    sdk: fakes.sdk,
    extensionSDK: fakes.extensionSDK,
    ui: fakes.ui,
    config: PASSIVE_CFG,
    sleep: fakes.sleep,
    random: () => 0.5,
  });

  assert.equal(outcome.state, 'failed');
  assert.deepEqual(fakes.sleeps, [100, 200, 400]);
});

test('direct (SMB): calls backend via serverProxy with secret key tag and redirects', async () => {
  const fakes = makeFakes({
    responses: [{ok: true, status: 200, body: {status: 'provisioned', redirect: '/dashboards/12'}}],
  });

  const outcome = await runProvisioning({
    sdk: fakes.sdk,
    extensionSDK: fakes.extensionSDK,
    ui: fakes.ui,
    config: {...DIRECT_CFG, transport: 'server', secretKeyName: 'backend_secret'},
    sleep: fakes.sleep,
  });

  assert.deepEqual(outcome, {state: 'redirecting', target: '/dashboards/12'});
  assert.equal(fakes.calls[0].via, 'server');
  assert.deepEqual(JSON.parse(fakes.calls[0].init.body), {
    user_id: '101',
    secret: '{{provisioning_landing_splash_backend_secret}}',
  });
});

test('direct: fails fast on 401 without retrying', async () => {
  const fakes = makeFakes({responses: [{ok: false, status: 401, body: {}}]});

  const outcome = await runProvisioning({
    sdk: fakes.sdk,
    extensionSDK: fakes.extensionSDK,
    ui: fakes.ui,
    config: DIRECT_CFG,
    sleep: fakes.sleep,
  });

  assert.equal(outcome.state, 'failed');
  assert.equal(fakes.calls.length, 1);
  assert.deepEqual(fakes.sleeps, []);
});

test('redirect supports reload, spa, logout, and new_tab modes', () => {
  const fakes = makeFakes();
  redirect(fakes.extensionSDK, '/dashboards/12', 'reload');
  redirect(fakes.extensionSDK, '/dashboards/12', 'spa');
  redirect(fakes.extensionSDK, '/dashboards/12', 'logout');
  redirect(fakes.extensionSDK, '/dashboards/12', 'new_tab');

  assert.deepEqual(fakes.locations, [
    ['/dashboards/12', undefined, '_self'],
    ['/dashboards/12'],
    ['/logout', undefined, '_self'],
  ]);
  assert.deepEqual(fakes.windows, [['/dashboards/12', '_blank']]);
});

test('blocks self-redirect loop and unsafe URLs', async () => {
  assert.equal(isSelfTarget('/spartan/provisioning_landing::splash', 'provisioning_landing::splash'), true);
  assert.equal(isSafeRelativePath('//evil.example.com'), false);
  assert.equal(extractNextParam('/?next=https://evil.example.com'), null);

  const fakes = makeFakes({
    responses: [{ok: true, status: 200, body: {status: 'provisioned', redirect: '/extensions/provisioning_landing::splash'}}],
  });
  const outcome = await runProvisioning({
    sdk: fakes.sdk,
    extensionSDK: fakes.extensionSDK,
    ui: fakes.ui,
    config: {...DIRECT_CFG, fallbackPath: '/extensions/provisioning_landing::splash'},
    sleep: fakes.sleep,
  });
  assert.equal(outcome.state, 'failed');
  assert.deepEqual(fakes.locations, []);
});
