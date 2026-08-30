# Data dictionary

Generated from `data/output/vngross_fund_period.csv` on 2026-08-16.
The documented file has SHA-256
`5a1b6fb125d3febaf0425b3d45351ecdee64e41f54e7b227ac7ceedc4cdc5608`:
**3,989 rows, 77 columns, 18 economic funds**, with period ends from
2021-01-04 to 2026-08-13. DCBC is retained only as DCDE's legacy
`source_fund_key`.

All monetary amounts are Vietnamese dong. All flow rates are scaled by
**beginning-of-period NAV**; using closing or average NAV would place the flow
inside its own denominator.

Line numbers in parentheses refer to Appendix XXIV of Circular
98/2020/TT-BTC. Every template line has "This period" and "Last period"
columns. Both are captured: `prior_` fields hold the latter and provide an
independent consistency check. A blank source cell is missing, never silently
converted to zero. Zero is substituted only inside reconciliation arithmetic.

## `vngross_fund_period.csv`

| Column | Type | Non-null | Meaning |
|---|---|---:|---|
| `fund_code` | string | 3,989 | Canonical economic-fund identifier. DCBC rows use `DCDE`. |
| `period_start` | date | 3,989 | First day of the filing's dealing period. |
| `period_end` | date | 3,989 | Last day of the filing's dealing period. |
| `report_date` | date | 3,599 | Date the filing was signed or published when parsed. |
| `period_days` | integer | 3,989 | `period_end - period_start`; inspect before pooling because frequency varies. |
| `source` | string | 3,989 | Primary filing URL; unique in the accepted panel. |
| `source_fund_key` | string | 3,989 | Filename/registry identity. Both `dcbc` and `dcde` map to canonical `fund_code=DCDE`. |
| `date_conflict` | string | 13 | Written audit detail when bilingual header dates disagree. |
| `template_variant` | string | 3,989 | `standard` or `alt`; the alternate layout uses different line-code semantics and is net-only. |
| `nav_begin` | float | 3,989 | Total NAV at period start, VND (line 1.1). |
| `nav_per_unit_begin` | float | 3,973 | NAV per certificate at period start (line 1.3, or alternate equivalent). |
| `nav_end` | float | 3,989 | Total NAV at period end, VND (line 2.1). |
| `nav_per_unit_end` | float | 3,973 | NAV per certificate at period end (line 2.3, or alternate equivalent). |
| `chg_investment` | float | 3,989 | Change in NAV from investment activity, VND (line 3.1). |
| `chg_flows_net` | float | 3,987 | Disclosed combined subscription/redemption change, VND (line 3.2). |
| `subscriptions` | float | 3,491 | Disclosed gross subscription inflow, positive VND (line 3.2.1); missing for net-only templates. |
| `redemptions` | float | 3,490 | Disclosed gross redemption outflow, negative VND (line 3.2.2); missing for net-only templates. |
| `chg_distribution` | float | 853 | Signed change from cash distribution, VND (line 3.3); blank means the line was absent. |
| `chg_nav_per_unit` | float | 951 | Disclosed change in NAV per certificate (line 4). |
| `nav_52w_high` | float | 3,736 | Disclosed 52-week NAV-per-certificate high (line 5.1). |
| `nav_52w_low` | float | 3,736 | Disclosed 52-week NAV-per-certificate low (line 5.2). |
| `foreign_units` | float | 3,970 | Period-end certificates classified as foreign-held (line 6.1); an ownership stock, not a gross flow. |
| `foreign_value` | float | 3,989 | Period-end value classified as foreign-held, VND (line 6.2). |
| `foreign_ownership_pct` | float | 3,989 | Foreign ownership percentage as printed (line 6.3). |
| `prior_nav_begin` | float | 3,989 | Prior-column value on the opening-NAV line. |
| `prior_nav_per_unit_begin` | float | 3,973 | Prior-column value on the opening NAV-per-certificate line. |
| `prior_nav_end` | float | 3,989 | Prior-column closing NAV; checked against current `nav_begin`. |
| `prior_nav_per_unit_end` | float | 3,973 | Prior-column closing NAV per certificate; checked against current opening value. |
| `prior_chg_investment` | float | 3,989 | Prior-column value for line 3.1. |
| `prior_chg_flows_net` | float | 3,986 | Prior-column value for line 3.2. |
| `prior_subscriptions` | float | 3,491 | Prior-column gross subscriptions where disclosed. |
| `prior_redemptions` | float | 3,489 | Prior-column gross redemptions where disclosed. |
| `prior_chg_distribution` | float | 852 | Prior-column distribution change where present. |
| `prior_chg_nav_per_unit` | float | 951 | Prior-column value for line 4. |
| `prior_nav_52w_high` | float | 3,736 | Prior-column value for line 5.1. |
| `prior_nav_52w_low` | float | 3,736 | Prior-column value for line 5.2. |
| `prior_foreign_units` | float | 3,970 | Prior-column foreign certificate stock. |
| `prior_foreign_value` | float | 3,989 | Prior-column foreign value. |
| `prior_foreign_ownership_pct` | float | 3,989 | Prior-column disclosed foreign ownership percentage. |
| `net_flow` | float | 3,987 | Line 3.2 where present, otherwise the sum of disclosed gross legs. |
| `units_begin` | float | 3,973 | `nav_begin / nav_per_unit_begin`; diagnostic certificate stock. |
| `units_end` | float | 3,973 | `nav_end / nav_per_unit_end`; diagnostic certificate stock. |
| `gross_return` | float | 3,973 | Legacy name for unadjusted NAV price return. |
| `price_return` | float | 3,973 | `nav_per_unit_end / nav_per_unit_begin - 1`. |
| `distribution_yield` | float | 853 | Positive cash distribution per beginning certificate divided by beginning NAV per certificate; missing when line 3.3 is absent. |
| `total_return` | float | 3,973 | Distribution-adjusted return used in lagged-performance analysis. |
| `foreign_share_of_nav` | float | 3,989 | `foreign_value / nav_end`, expressed as a fraction; independent check on the printed percentage. |
| `foreign_ownership_anomaly` | string | 97 | Written warning when disclosed ownership conflicts materially with value/unit-based shares. Values are retained, not silently corrected. |
| `reconcile_residual_vnd` | float | 3,989 | Residual from the NAV identity. 3,988 are exactly zero; one VND480.93 residual is within its VND784.63 scale-aware tolerance. |
| `net_flow_residual_vnd` | float | 3,989 | Line 3.2 minus lines 3.2.1 + 3.2.2 where cross-checkable. |
| `prior_column_warnings` | string | 2 | Same-filing prior-column mismatch warning; optional malformed priors do not automatically discard a valid current row. |
| `filename_date_conflict` | boolean | 3,989 | True when a date inferred from the filename materially conflicts with the accepted filing period. |
| `gross_subscription_rate` | float | 3,490 | `subscriptions / nav_begin`, available only with both genuinely disclosed gross legs. |
| `gross_redemption_rate` | float | 3,490 | `abs(redemptions) / nav_begin`, available only with both genuinely disclosed gross legs. |
| `net_flow_rate` | float | 3,987 | `net_flow / nav_begin`. |
| `churn_rate` | float | 3,490 | `(subscriptions + abs(redemptions)) / nav_begin`. |
| `gross_legs_disclosed` | boolean | 3,989 | True only when both gross lines are present in the source; 3,490 rows are true. |
| `gross_legs_inferred_zero` | boolean | 3,989 | Both gross lines absent while disclosed net flow is zero; 9 rows are true and remain excluded from gross-leg analysis. |
| `flow_asymmetry` | float | 3,482 | `(subscriptions - abs(redemptions)) / (subscriptions + abs(redemptions))`, in [-1, 1]. |
| `manager_id` | string | 3,989 | Canonical management-company identifier. |
| `fund_key` | string | 3,989 | Canonical registry fund key after alias resolution; legacy DCBC rows use `dcde`. |
| `fund_name` | string | 3,989 | Registered fund name. |
| `asset_class` | string | 3,989 | `equity`, `balanced`, or `bond`. |
| `canonical_code` | string | 3,989 | Canonical code from registry metadata; equals final `fund_code`. |
| `is_alias` | boolean | 3,989 | Canonical-panel metadata flag; false after aliases have been resolved. Use `source_fund_key` to identify legacy source files. |
| `index_end` | float | 3,989 | Last VN-Index close at or before the period-end boundary. |
| `index_begin` | float | 3,989 | Previous accepted row's `index_end` for contiguous periods; otherwise the last close at or before `period_start`. |
| `market_boundary_source` | string | 3,989 | `contiguous`, `first_period_asof`, or a gap/as-of fallback explaining `index_begin`. |
| `market_return` | float | 3,989 | VN-Index return under the documented boundary rule. |
| `excess_return` | float | 3,973 | `total_return - market_return`; primary excess-return field. |
| `excess_return_gross` | float | 3,973 | Legacy unadjusted `gross_return - market_return`. |
| `month` | string | 3,989 | Period-end month, `YYYY-MM`. |
| `deposit_rate_pct` | float | 3,031 | Curated 12-month deposit rate for the period-end month; mixes documented source constructs. |
| `deposit_rate_provenance` | string | 3,298 | Observation, bridge, or missing-value provenance. |
| `deposit_rate_tenor` | string | 3,298 | Rate tenor; 12 months. |
| `deposit_rate_source` | string | 3,031 | Source construct for each non-missing rate. |
| `deposit_rate_bank` | string | 3,298 | Bank or bank group associated with the rate/provenance row. |

## `vngross_fund_month.csv`

The monthly file has **950 rows and 33 columns**. Flows sum; opening/closing NAV
use the first/last filing boundary; returns compound; and gross status is true
only when all required constituent gross data are available. A filing is
assigned to its ending month. Because 804 current period rows cross a calendar
month boundary, monthly timing and seasonality inherit that approximation.

## Companion and research files

| File | Contents |
|---|---|
| `quarantine.csv` | 20 rows excluded by hard gates, each with source and written reason. |
| `continuity_breaks.csv` | 79 cross-filing NAV-chain records; inspect before multi-period work. |
| `superseded_duplicates.csv` | 114 republications or overlapping alternatives not used as operative rows. |
| `parse_failures.csv` | 1,497 cached references that could not be parsed, with reason. |
| `period_corrections.csv` | Explicit accepted period/date corrections and their evidence. |
| `measurement_error_diagnostics.csv` | Unit-change flow proxies and errors; diagnostics only, never panel flow values. |
| `fmarket_cross_check.csv` | VCBF NAV per certificate against an independent feed. |
| `build_manifest.json` | Panel hashes, counts, managers, source references, versions, and analysis sample sizes. |
| `growth_research/investor_net_demand_period.csv` | Validated foreign/domestic **net certificate-demand** inference; not gross segment flows. |
| `growth_research/investor_net_demand_exclusions.csv` | Every failed segment split with its written reason. |
| `growth_research/investor_net_demand_exclusion_summary.csv` | Stable category counts for failed splits. |
| `growth_research/manifest.json` | Input panel hashes and hashes of every published growth-research CSV. |

## Reading exclusions and investor segments

Exclusion files are part of the dataset, not an appendix. An accepted row may
still border a reported continuity gap; exact-consecutive analyses must not
bridge it.

The public filing identifies gross subscriptions/redemptions for the **whole
fund** and foreign ownership as an end-of-period **stock**. It cannot split gross
subscriptions or gross redemptions into foreign and domestic legs. For validated
contiguous stock chains, the growth-research layer infers only foreign net unit
demand and domestic residual net unit demand, valued at midpoint NAV per
certificate. Segment-level gross flows, customer identity, investor counts, and
internal transfers require transfer-agent or transaction-level CRM data.
