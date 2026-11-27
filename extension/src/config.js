/**
 * @fileoverview Dual-mode configuration for SMB and Enterprise profiles.
 * Edit, then rebuild (`npm run build`).
 */

export const CONFIG = Object.freeze({
  /**
   * Provisioning architecture mode:
   *   'passive_poll' (Enterprise / Private IP / VPC-SC):
   *     Uses manifest.enterprise.lkml with external_api_urls: [].
   *     Cloud Logging -> Pub/Sub -> Cloud Tasks -> Worker provisions the user
   *     privately inside GCP; the extension polls Looker's own sdk.me() with
   *     backoff until the user's new group lands, then auto-redirects.
   *   'direct' (SMB / Fast PoC):
   *     Uses manifest.smb.lkml. The extension posts {user_id} to backendUrl
   *     (via fetchProxy or serverProxy), then redirects immediately.
   */
  mode: 'passive_poll',

  /* ---- Enterprise ('passive_poll') settings ---- */

  /** Destination once provisioned (overridden if portal passes ?next=/...). */
  hubPath: '/browse',

  /**
   * Optional explicit list of Looker group IDs (strings) that indicate the
   * user is provisioned (e.g. your Dashboard Hub group ID). When empty, ANY
   * group outside `ignoredGroupIds` and `pendingMarkerGroupId` triggers ready.
   */
  provisionedGroupIds: [],

  /**
   * Looker group IDs present on unprovisioned users (typically '1' for All
   * Users plus your coarse SAML onboarding group ID).
   */
  ignoredGroupIds: ['1'],

  /**
   * Optional Looker group ID of `BI_Access_Pending_Approval`. When the Worker
   * assigns this zero-privilege marker group, polling stops immediately and
   * shows the "no dashboards assigned" screen (with optional request link).
   */
  pendingMarkerGroupId: '',

  /** Optional self-service IGA / IT portal URL shown on the no_access screen. */
  accessRequestUrl: '',

  /**
   * Jittered backoff schedule (ms) for passive_poll checks of sdk.me().
   * Initial 2.5s delay aligns with typical Cloud Logging -> Worker latency so
   * most first-time users resolve in 1–2 calls.
   */
  pollIntervalsMs: [2500, 4000, 7000, 12000, 20000],

  /* ---- SMB ('direct') settings ---- */

  /** Provisioning endpoint URL (must match manifest.smb.lkml). */
  backendUrl: 'http://localhost:8787/provision',

  /** 'fetch' (browser fetchProxy + CORS) or 'server' (Looker serverProxy). */
  transport: 'fetch',

  /** 'server' transport only: scoped user attribute suffix for shared secret. */
  secretKeyName: '',

  /** Retry settings for 'direct' mode. */
  maxAttempts: 4,
  retryDelayMs: 1500,
  requestTimeoutMs: 15000,

  /* ---- Shared redirect settings ---- */

  /**
   * How the browser transitions once access is ready:
   *   'reload'  - page load via updateLocation(path, undefined, '_self')
   *   'spa'     - in-app navigation via updateLocation(path)
   *   'logout'  - redirects to /logout (use if Looker session caches groups
   *               until re-login and SAML Bypass Login Page is enabled)
   *   'new_tab' - opens target in a new tab via openBrowserWindow
   */
  redirectMode: 'reload',

  /**
   * false: pauses on ready with buttons for reload / SPA / logout / new tab
   *        so you can verify which refreshes Looker permissions on your instance.
   * true:  redirects automatically using redirectMode (production setting).
   */
  autoRedirect: false,

  /** Safe fallback when no other destination is configured or valid. */
  fallbackPath: '/browse',
});
