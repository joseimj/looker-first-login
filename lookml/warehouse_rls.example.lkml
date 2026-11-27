# Enterprise Plane 3 Example: Warehouse-Native RLS via Built-In SAML Email.
#
# Eliminates Looker User Attribute API writes for high-cardinality RLS/CLS
# attributes (hundreds/thousands per user), enforces row-level isolation at
# t = 0ms even on deep links, and takes effect immediately when Enterprise IGA
# updates the BigQuery security table (see sql/bigquery_rls_entitlements.sql).

explore: sales_orders {
  # Built-in `_user_attributes['email']` is populated automatically by SAML on
  # Login #1 with ZERO Looker Admin API calls.
  # Note: Enterprise SAML emails are IdP-controlled, and BigQuery Row-Level
  # Access Policies (`CREATE ROW ACCESS POLICY ... FILTER USING (SESSION_USER()...)`)
  # can also be used if OAuth/per-user warehouse credentials are enabled.
  sql_always_where:
    EXISTS (
      SELECT 1
      FROM `YOUR_PROJECT.governance.user_rls_entitlements` AS rls
      WHERE LOWER(rls.user_email) = LOWER('{{ _user_attributes["email"] }}')
        AND rls.is_active = TRUE
        AND (rls.region = '*' OR rls.region = ${sales_orders.region})
        AND (rls.cost_center = '*' OR rls.cost_center = ${sales_orders.cost_center})
    ) ;;
}
