/**
 * @fileoverview Settings of the landing extension. Edit, then rebuild.
 *
 * Every URL listed here must also appear in `external_api_urls` in the
 * LookML manifest, or Looker blocks the call.
 */

export const CONFIG = Object.freeze({
  /** Endpoint of the provisioning backend. */
  backendUrl: 'http://localhost:8787/provision',

  /**
   * How the backend is reached:
   *   'fetch'  - from the user's browser (extensionSDK.fetchProxy). The
   *              backend must be reachable from the browser and allow the
   *              Looker origin with CORS. Fits a backend on localhost or on
   *              the corporate network.
   *   'server' - from the Looker server (extensionSDK.serverProxy). The
   *              backend must be reachable from the Looker instance. Allows
   *              a shared secret that never reaches the browser.
   */
  transport: 'fetch',

  /**
   * 'server' transport only. Short name of the hidden Looker user attribute
   * that holds the shared secret; empty to send none. With extension
   * `provisioning_landing::splash` and the name `backend_secret`, the user
   * attribute must be called `provisioning_landing_splash_backend_secret`.
   */
  secretKeyName: '',

  /**
   * How the browser moves to the destination once access is granted:
   *   'reload' - asks the Looker host to load the destination as a new page,
   *              so that everything Looker cached in the page is rebuilt.
   *   'spa'    - in-app navigation, without a page load.
   * Only used when autoRedirect is true.
   */
  redirectMode: 'reload',

  /**
   * false: the splash screen stops once provisioning is done and offers one
   *        button per redirect mode. This is the setting to start with: it
   *        shows which mode gives the user working access.
   * true:  the user is redirected at once with redirectMode. This is the
   *        real first-login experience.
   */
  autoRedirect: false,

  /** Calls to the backend before giving up and offering "Try again". */
  maxAttempts: 4,

  /** Wait before the second attempt; later attempts wait proportionally longer. */
  retryDelayMs: 1500,

  /** Longest time to wait for one answer from the backend. */
  requestTimeoutMs: 15000,

  /** Destination when the backend names none. */
  fallbackPath: '/browse',
});
