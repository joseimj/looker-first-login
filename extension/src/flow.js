/**
 * @fileoverview Provisioning flow of the landing extension.
 *
 * Free of DOM access and of Looker SDK imports, so that it runs under plain
 * Node in the unit tests. The Looker objects are passed in:
 *   - `sdk`: Looker API 4.0 client of the extension (only `me` is called),
 *   - `extensionSDK`: extension host API (`fetchProxy`, `serverProxy`,
 *     `createSecretKeyTag`, `updateLocation`, `openBrowserWindow`).
 */

/** States shown by the splash screen. */
export const State = Object.freeze({
  IDENTIFYING: 'identifying',
  PROVISIONING: 'provisioning',
  REDIRECTING: 'redirecting',
  READY: 'ready',
  NO_ACCESS: 'no_access',
  FAILED: 'failed',
});

/** Backend statuses meaning the user can be sent to their destination. */
const DONE_STATUSES = new Set(['provisioned', 'already_provisioned']);

/** Backend statuses that retrying cannot change. */
const NO_ACCESS_STATUSES = new Set([
  'no_entitlements',
  'unknown_user',
  'disabled',
  'not_saml',
]);

/** HTTP client/configuration errors that will not succeed on retry. */
const NON_RETRYABLE_HTTP_STATUSES = new Set([400, 401, 403, 404, 405, 422]);

/**
 * Tells whether a destination is a path on this Looker instance. The backend
 * answer crosses a network, so it is checked before the browser is sent
 * there.
 * @param {*} path
 * @return {boolean}
 */
export function isSafeRelativePath(path) {
  return typeof path === 'string' && /^\/(?![/\\])\S*$/.test(path);
}

/**
 * Tells whether `path` points back to this extension itself, which would
 * trigger an infinite redirect loop.
 * @param {string} path
 * @param {string|undefined} extensionId e.g. 'provisioning_landing::splash'.
 * @return {boolean}
 */
export function isSelfTarget(path, extensionId) {
  if (!extensionId || typeof path !== 'string') return false;
  const pathname = path.split(/[?#]/, 1)[0].replace(/\/+$/, '');
  const prefixes = [`/extensions/${extensionId}`, `/spartan/${extensionId}`];
  return prefixes.some(
      (prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`));
}

/**
 * Sends the browser to a page of the Looker instance.
 * @param {!Object} extensionSDK
 * @param {string} path
 * @param {string} mode 'reload', 'spa', or 'new_tab'; see config.js.
 */
export function redirect(extensionSDK, path, mode) {
  if (mode === 'spa') {
    extensionSDK.updateLocation(path);
  } else if (mode === 'new_tab') {
    extensionSDK.openBrowserWindow(path, '_blank');
  } else {
    // A target makes the host open the URL as a page load in that window.
    extensionSDK.updateLocation(path, undefined, '_self');
  }
}

/**
 * @param {number} ms
 * @return {!Promise<void>}
 */
function defaultSleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Rejects when a promise takes longer than the given time.
 * @param {!Promise<T>} promise
 * @param {number} ms
 * @return {!Promise<T>}
 * @template T
 */
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
 * Asks the backend to provision the user.
 * @param {!Object} extensionSDK
 * @param {!Object} config
 * @param {string|number} userId Looker ID of the current user.
 * @return {!Promise<{status: string, redirect: (string|null|undefined)}>}
 */
export async function callBackend(extensionSDK, config, userId) {
  const payload = {user_id: String(userId)};
  const viaServer = config.transport === 'server';
  if (viaServer && config.secretKeyName) {
    // Looker replaces the tag with the user attribute value on the server:
    // the secret itself never reaches the browser.
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
 * Runs one provisioning round: identifies the user, calls the backend (with
 * automatic retries on transient errors) and, on success, redirects or reports
 * the destination.
 *
 * @param {{
 *   sdk: !Object,
 *   extensionSDK: !Object,
 *   ui: {setState: function(string, !Object=), log: function(string)},
 *   config: !Object,
 *   sleep: (function(number): !Promise<void>|undefined),
 * }} deps
 * @return {!Promise<{state: string, target: (string|undefined),
 *     error: (!Error|undefined)}>} The state the splash screen ends in.
 *     READY and REDIRECTING carry `target`, FAILED carries `error`.
 */
export async function runProvisioning(
    {sdk, extensionSDK, ui, config, sleep = defaultSleep}) {
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
        return finish(extensionSDK, ui, config, result.redirect);
      }
      if (NO_ACCESS_STATUSES.has(result.status)) {
        ui.setState(State.NO_ACCESS);
        return {state: State.NO_ACCESS};
      }
      // Anything else, such as "pending", is worth another attempt.
      lastError = new Error(`Backend status "${result.status}"`);
    } catch (error) {
      lastError = error;
      ui.log(`Attempt ${attempt} failed: ${error.message}`);
      if (error.retryable === false) {
        break;
      }
    }
    if (attempt < config.maxAttempts) {
      await sleep(config.retryDelayMs * attempt);
    }
  }
  ui.setState(State.FAILED, {error: lastError});
  return {state: State.FAILED, error: lastError};
}

/**
 * @param {!Object} extensionSDK
 * @param {!Object} ui
 * @param {!Object} config
 * @param {*} requestedTarget Destination named by the backend.
 * @return {{state: string, target: (string|undefined),
 *     error: (!Error|undefined)}}
 */
function finish(extensionSDK, ui, config, requestedTarget) {
  const extensionId = extensionSDK && extensionSDK.lookerHostData &&
      extensionSDK.lookerHostData.extensionId;
  let target = null;
  if (isSafeRelativePath(requestedTarget) &&
      !isSelfTarget(requestedTarget, extensionId)) {
    target = requestedTarget;
  } else if (requestedTarget != null) {
    ui.log(
        'Ignored a destination that is unsafe or points back to this ' +
        'extension');
  }
  if (target === null && isSafeRelativePath(config.fallbackPath) &&
      !isSelfTarget(config.fallbackPath, extensionId)) {
    target = config.fallbackPath;
  }
  if (target === null) {
    const error = new Error(
        'No safe redirect destination (target and fallback point back to ' +
        'this extension or are invalid)');
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
