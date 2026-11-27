-- Enterprise Plane 3: Warehouse-Native Row-Level Security Entitlements Table
--
-- Sync your Enterprise IGA / HR entitlement rows into this clustered BigQuery
-- table instead of calling Looker's set_user_attribute_user_value API per user.
-- Benefits:
--   * Zero Looker Admin API calls for RLS/CLS (preserves monthly edition quota)
--   * Zero first-login RLS race window (SAML email is available at t = 0ms)
--   * Supports hundreds/thousands of high-cardinality entitlements per user
--   * Instant revocation on the next query when is_active is set to FALSE

CREATE SCHEMA IF NOT EXISTS `YOUR_PROJECT.governance`
OPTIONS (
  location = "europe-west3",
  description = "Enterprise BI row-level security entitlements synced from IGA"
);

CREATE TABLE IF NOT EXISTS `YOUR_PROJECT.governance.user_rls_entitlements` (
  user_email  STRING    NOT NULL,
  region      STRING    NOT NULL,  -- e.g. 'EMEA', 'NA', or '*'
  cost_center STRING    NOT NULL,  -- e.g. 'CC-4100', or '*'
  is_active   BOOL      NOT NULL,
  updated_at  TIMESTAMP NOT NULL
)
CLUSTER BY user_email, region;

-- Example seed rows (replace with automated IGA / HR pipeline sync):
-- INSERT INTO `YOUR_PROJECT.governance.user_rls_entitlements`
-- VALUES
--   ('alice@example.com', 'EMEA', 'CC-4100', TRUE, CURRENT_TIMESTAMP()),
--   ('alice@example.com', 'EMEA', 'CC-4200', TRUE, CURRENT_TIMESTAMP());
