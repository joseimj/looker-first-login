/**
 * @fileoverview Dual-mode provisioning flow (SMB 'direct' + Enterprise
 * 'passive_poll'). Free of DOM and Looker SDK imports for plain Node tests.
 */

export const State = Object.freeze({
  IDENTIFYING: 'identifying',
  PROVISIONING: 'provisioning',
  REDIRECTING: 'redirecting',
  READY: 'ready',
  NO_ACCESS: 'no_access',
  FAILED: 'failed',
});

const DONE_STATUSES = new Set(['provisioned', 'already_provisioned']);
const NO_ACCESS_STATUSES = new Set([
  'no_entitlements',
  'unknown_user',
  'disabled',
  'not_saml',
]);
const NON_RETRYABLE_HTTP_STATUSES = new Set([400, 401, 403, 404, 405, 422]);

export function isSafeRelativePath(path) {
  return typeof path === 'string' && /^\/(?![/\\])\S*$/.test(path);
}

export function isSelfTarget(path, extensionId) {
  if (!extensionId || typeof path !== 'string') return false;
  const pathname = path.split(/[?#]/, 1)[0].replace(/\/+$/, '');
  const prefixes = [`/extensions/${extensionId}`, `/spartan/${extensionId}`];
  return prefixes.some(
      (prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`));
}

/**
 * Extracts an optional `?next=/dashboards/42` parameter from an extension
 * route string (useful when an enterprise portal wraps Looker deep links).
 * @param {string|undefined} route
 * @return {string|null}
 */
export function extractNextParam(route) {
  if (typeof route !== 'string') return null;
  const qIndex = route.indexOf('?');
  if (qIndex === -1) return null;
  const params = new URLSearchParams(route.slice(qIndex + 1));
  const next = params.get('next');
  return isSafeRelativePath(next) ? next : null;
}

export function redirect(extensionSDK, path, mode) {
  if (mode === 'spa') {
    extensionSDK.updateLocation(path);
  } else if (mode === 'new_tab') {
    extensionSDK.openBrowserWindow(path, '_blank');
  } else if (mode === 'logout') {
    // With SAML Bypass Login Page enabled, /logout silently re-authenticates
    // via the active IdP session and lands on the newly ranked landing_page.
    extensionSDK.updateLocation('/logout', undefined, '_self');
  } else {
    extensionSDK.updateLocation(path, undefined, '_self');
  }
}

function defaultSleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function withTimeout(promise, ms) {
  let timer;
  const timeout = new Promise((resolve, reject) => {
    timer = setTimeout(
        () => reject(new Error(`No answer from the backend after ${ms} ms`)),
        ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

/**
 * Evaluates a user's Looker group_ids in 'passive_poll' mode.
 * @param {!Array<string|number>} groupIds
 * @param {!Object} config
 * @param {!Set<string>} baselineGroupIds Initial groups observed at t=0.
 * @return {'ready'|'no_access'|'pending'}
 */
export function classifyGroups(groupIds, config, baselineGroupIds) {
  const current = new Set((groupIds || []).map((g) => String(g)));
  const pendingId = config.pendingMarkerGroupId ?
      String(config.pendingMarkerGroupId) :
      '';
  if (pendingId && current.has(pendingId)) {
    return 'no_access';
  }
  const explicitTargets = (config.provisionedGroupIds || []).map(String);
  if (explicitTargets.length > 0) {
    return explicitTargets.some((id) => current.has(id)) ? 'ready' : 'pending';
  }
  const ignored = new Set((config.ignoredGroupIds || []).map(String));
  // Ready if user already holds a non-ignored group beyond baseline, OR if
  // any brand-new non-ignored group appeared since t=0.
  for (const id of current) {
    if (id === pendingId || ignored.has(id)) continue;
    if (!baselineGroupIds.has(id)) return 'ready';
  }
  return 'pending';
}

export async function callBackend(extensionSDK, config, userId) {
  const payload = {user_id: String(userId)};
  const viaServer = config.transport === 'server';
  if (viaServer && config.secretKeyName) {
    payload.secret = extensionSDK.createSecretKeyTag(config.secretKeyName);
  }
  const init = {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  };
  const call = viaServer ?
      extensionSDK.serverProxy(config.backendUrl, init, 'json') :
      extensionSDK.fetchProxy(config.backendUrl, init, 'json');
  const response = await withTimeout(call, config.requestTimeoutMs);
  if (!response || !response.ok) {
    const status = response ? response.status : 'nothing';
    const error = new Error(`Backend answered HTTP ${status}`);
    if (response && NON_RETRYABLE_HTTP_STATUSES.has(response.status)) {
      error.retryable = false;
    }
    throw error;
  }
  const body = typeof response.body === 'string' ?
      JSON.parse(response.body) :
      response.body;
  if (!body || typeof body.status !== 'string') {
    throw new Error('Backend answer has no status');
  }
  return body;
}

/**
 * Entry point supporting both Enterprise 'passive_poll' (zero external calls)
 * and SMB 'direct' (calls POST /provision).
 */
export async function runProvisioning({
  sdk,
  extensionSDK,
  ui,
  config,
  nextPath = null,
  sleep = defaultSleep,
  random = Math.random,
}) {
  if (config.mode === 'passive_poll') {
    return runPassivePoll(
        {sdk, extensionSDK, ui, config, nextPath, sleep, random});
  }
  return runDirect({sdk, extensionSDK, ui, config, nextPath, sleep});
}

async function runPassivePoll(
    {sdk, extensionSDK, ui, config, nextPath, sleep, random}) {
  ui.setState(State.IDENTIFYING);
  let me;
  try {
    me = await sdk.ok(sdk.me('id,group_ids'));
  } catch (error) {
    ui.log(`Could not read the current user: ${error.message}`);
    ui.setState(State.FAILED, {error});
    return {state: State.FAILED, error};
  }
  const initialIds = (me.group_ids || []).map(String);
  ui.log(`Signed in as Looker user ${me.id} (groups: [${initialIds.join(', ')}])`);

  // Check if user is ALREADY provisioned (explicit target group present) or
  // already marked as pending approval at t=0.
  const emptyBaseline = new Set();
  const explicitMode = (config.provisionedGroupIds || []).length > 0;
  const initialCheck = classifyGroups(
      initialIds, config, explicitMode ? emptyBaseline : new Set(initialIds));
  if (initialCheck === 'no_access') {
    ui.log('Pending-approval marker group already assigned');
    ui.setState(State.NO_ACCESS);
    return {state: State.NO_ACCESS};
  }
  if (initialCheck === 'ready') {
    ui.log('Target group already present on initial check');
    return finish(extensionSDK, ui, config, nextPath || config.hubPath);
  }
  // If no explicit provisionedGroupIds are set, check whether user already
  // holds a group outside ignoredGroupIds (other than coarse onboarding).
  const baseline = new Set(initialIds);
  const intervals = config.pollIntervalsMs || [2500, 4000, 7000, 12000, 20000];

  for (let i = 0; i < intervals.length; i++) {
    const attempt = i + 1;
    ui.setState(State.PROVISIONING, {attempt});
    const jitterFactor = 0.8 + random() * 0.4;  // ±20% jitter
    await sleep(Math.round(intervals[i] * jitterFactor));
    try {
      const latest = await sdk.ok(sdk.me('id,group_ids'));
      const latestIds = (latest.group_ids || []).map(String);
      ui.log(`Poll ${attempt}: groups=[${latestIds.join(', ')}]`);
      const verdict = classifyGroups(latestIds, config, baseline);
      if (verdict === 'ready') {
        return finish(extensionSDK, ui, config, nextPath || config.hubPath);
      }
      if (verdict === 'no_access') {
        ui.setState(State.NO_ACCESS);
        return {state: State.NO_ACCESS};
      }
    } catch (error) {
      ui.log(`Poll ${attempt} failed: ${error.message}`);
    }
  }
  const error = new Error(
      'Provisioning is taking longer than expected; please click Try again.');
  ui.setState(State.FAILED, {error});
  return {state: State.FAILED, error};
}

async function runDirect({sdk, extensionSDK, ui, config, nextPath, sleep}) {
  ui.setState(State.IDENTIFYING);
  let userId;
  try {
    userId = (await sdk.ok(sdk.me('id'))).id;
  } catch (error) {
    ui.log(`Could not read the current user: ${error.message}`);
    ui.setState(State.FAILED, {error});
    return {state: State.FAILED, error};
  }
  ui.log(`Signed in as Looker user ${userId}`);

  let lastError;
  for (let attempt = 1; attempt <= config.maxAttempts; attempt++) {
    ui.setState(State.PROVISIONING, {attempt});
    const startedAt = Date.now();
    try {
      const result = await callBackend(extensionSDK, config, userId);
      ui.log(`Backend answered "${result.status}" in ` +
          `${Date.now() - startedAt} ms (attempt ${attempt})`);
      if (DONE_STATUSES.has(result.status)) {
        return finish(
            extensionSDK, ui, config, nextPath || result.redirect);
      }
      if (NO_ACCESS_STATUSES.has(result.status)) {
        ui.setState(State.NO_ACCESS);
        return {state: State.NO_ACCESS};
      }
      lastError = new Error(`Backend status "${result.status}"`);
    } catch (error) {
      lastError = error;
      ui.log(`Attempt ${attempt} failed: ${error.message}`);
      if (error.retryable === false) break;
    }
    if (attempt < config.maxAttempts) {
      await sleep(config.retryDelayMs * attempt);
    }
  }
  ui.setState(State.FAILED, {error: lastError});
  return {state: State.FAILED, error: lastError};
}

function finish(extensionSDK, ui, config, requestedTarget) {
  const extensionId = extensionSDK && extensionSDK.lookerHostData &&
      extensionSDK.lookerHostData.extensionId;
  let target = null;
  if (isSafeRelativePath(requestedTarget) &&
      !isSelfTarget(requestedTarget, extensionId)) {
    target = requestedTarget;
  } else if (requestedTarget != null) {
    ui.log('Ignored destination that is unsafe or points back to itself');
  }
  if (target === null && isSafeRelativePath(config.fallbackPath) &&
      !isSelfTarget(config.fallbackPath, extensionId)) {
    target = config.fallbackPath;
  }
  if (target === null) {
    const error = new Error(
        'No safe redirect destination (target and fallback are invalid or ' +
        'point back to this extension)');
    ui.log(error.message);
    ui.setState(State.FAILED, {error});
    return {state: State.FAILED, error};
  }
  if (!config.autoRedirect) {
    ui.setState(State.READY, {target});
    return {state: State.READY, target};
  }
  ui.setState(State.REDIRECTING, {target});
  redirect(extensionSDK, target, config.redirectMode);
  return {state: State.REDIRECTING, target};
}
