# Data dictionary

Generated from `data/output/vngross_fund_period.csv` on 2026-08-16.
3,860 rows, 65 columns, 19 funds,
2021-01-04 to 2026-08-13.

All monetary amounts are Vietnamese dong. All flow rates are scaled by
**beginning**-of-period NAV; using closing or average NAV would place the flow
inside its own denominator.

Line numbers in parentheses refer to Appendix XXIV of Circular 98/2020/TT-BTC.

Every template line has two numeric columns, "This period" and "Last period".
Both are captured. The `prior_` columns hold the second, which gives a free
consistency check: a filing's `prior_nav_end` should equal its own `nav_begin`,
and should equal the preceding filing's `nav_end`.

A blank cell in a filing means the line does not apply that period. It is
recorded as missing, never as zero. Zero is substituted only inside the
reconciliation arithmetic.

## vngross_fund_period.csv

| Column | Type | Non-null | Meaning |
|---|---|---|---|
| `fund_code` | object | 3,860 / 3,860 | Stable fund identifier. Keyed on the manager's own filename prefix, not the code printed inside the document, which changes on rename. |
| `period_start` | object | 3,860 / 3,860 | First day of the dealing period, from the filing's own header. |
| `period_end` | object | 3,860 / 3,860 | Last day of the dealing period. |
| `report_date` | object | 3,593 / 3,860 | Date the filing was signed. |
| `period_days` | int64 | 3,860 / 3,860 | period_end minus period_start. Check before pooling; frequency varies by fund. |
| `source` | object | 3,860 / 3,860 | URL of the filing this row was parsed from. |
| `date_conflict` | object | 13 / 3,860 | Set when the bilingual header's two halves disagree on the period window. |
| `template_variant` | object | 3,860 / 3,860 | standard or alt. The alt layout assigns different meanings to codes 1.2, 2.2, 3.2.1 and 3.2.2. |
| `nav_begin` | float64 | 3,860 / 3,860 | Total NAV at the start of the period, VND (line 1.1). |
| `nav_per_unit_begin` | float64 | 3,860 / 3,860 | NAV per fund certificate at period start (line 1.3). |
| `nav_end` | float64 | 3,860 / 3,860 | Total NAV at the end of the period, VND (line 2.1). |
| `nav_per_unit_end` | float64 | 3,860 / 3,860 | NAV per fund certificate at period end (line 2.3). |
| `chg_investment` | float64 | 3,860 / 3,860 | Change in NAV from investment activity, VND (line 3.1). |
| `chg_flows_net` | float64 | 3,860 / 3,860 | Change in NAV from subscription and redemption, net, VND (line 3.2). |
| `subscriptions` | float64 | 3,487 / 3,860 | Gross subscription inflow, VND, positive (line 3.2.1). Missing where the manager files a net-only template. |
| `redemptions` | float64 | 3,486 / 3,860 | Gross redemption outflow, VND, negative (line 3.2.2). Missing where the manager files a net-only template. |
| `chg_distribution` | float64 | 857 / 3,860 | Change in NAV from profit distribution, VND (line 3.3). Absent means no distribution, not zero. |
| `chg_nav_per_unit` | float64 | 815 / 3,860 | Change in NAV per certificate versus the prior period (line 4). |
| `nav_52w_high` | float64 | 3,611 / 3,860 | 52-week high (line 5.1). |
| `nav_52w_low` | float64 | 3,611 / 3,860 | 52-week low (line 5.2). |
| `foreign_units` | float64 | 3,860 / 3,860 | Certificates held by foreign investors (line 6.1). |
| `foreign_value` | float64 | 3,860 / 3,860 | Value held by foreign investors, VND (line 6.2). |
| `foreign_ownership_pct` | float64 | 3,860 / 3,860 | Foreign ownership ratio as printed, percent (line 6.3). |
| `prior_nav_begin` | float64 | 3,851 / 3,860 | Same line as `nav_begin`, read from the template's "Last period" column. |
| `prior_nav_per_unit_begin` | float64 | 3,851 / 3,860 | Same line as `nav_per_unit_begin`, read from the template's "Last period" column. |
| `prior_nav_end` | float64 | 3,851 / 3,860 | Same line as `nav_end`, read from the template's "Last period" column. |
| `prior_nav_per_unit_end` | float64 | 3,859 / 3,860 | Same line as `nav_per_unit_end`, read from the template's "Last period" column. |
| `prior_chg_investment` | float64 | 3,852 / 3,860 | Same line as `chg_investment`, read from the template's "Last period" column. |
| `prior_chg_flows_net` | float64 | 3,851 / 3,860 | Same line as `chg_flows_net`, read from the template's "Last period" column. |
| `prior_subscriptions` | float64 | 3,487 / 3,860 | Same line as `subscriptions`, read from the template's "Last period" column. |
| `prior_redemptions` | float64 | 3,485 / 3,860 | Same line as `redemptions`, read from the template's "Last period" column. |
| `prior_chg_distribution` | float64 | 856 / 3,860 | Same line as `chg_distribution`, read from the template's "Last period" column. |
| `prior_chg_nav_per_unit` | float64 | 815 / 3,860 | Same line as `chg_nav_per_unit`, read from the template's "Last period" column. |
| `prior_nav_52w_high` | float64 | 3,602 / 3,860 | Same line as `nav_52w_high`, read from the template's "Last period" column. |
| `prior_nav_52w_low` | float64 | 3,602 / 3,860 | Same line as `nav_52w_low`, read from the template's "Last period" column. |
| `prior_foreign_units` | float64 | 3,851 / 3,860 | Same line as `foreign_units`, read from the template's "Last period" column. |
| `prior_foreign_value` | float64 | 3,851 / 3,860 | Same line as `foreign_value`, read from the template's "Last period" column. |
| `prior_foreign_ownership_pct` | float64 | 3,851 / 3,860 | Same line as `foreign_ownership_pct`, read from the template's "Last period" column. |
| `net_flow` | float64 | 3,860 / 3,860 | Line 3.2 where disclosed, else subscriptions + redemptions. |
| `units_begin` | float64 | 3,860 / 3,860 | nav_begin / nav_per_unit_begin. Diagnostic only. |
| `units_end` | float64 | 3,860 / 3,860 | nav_end / nav_per_unit_end. Diagnostic only. |
| `gross_return` | float64 | 3,860 / 3,860 | nav_per_unit_end / nav_per_unit_begin - 1. NOT distribution adjusted. |
| `foreign_share_of_nav` | float64 | 3,860 / 3,860 | foreign_value / nav_end, percent. Derived cross-check on line 6.3, which does not always equal it. |
| `reconcile_residual_vnd` | float64 | 3,860 / 3,860 | NAV identity residual. 0.00 on every row in the panel. |
| `net_flow_residual_vnd` | float64 | 3,860 / 3,860 | Line 3.2 minus (3.2.1 + 3.2.2), where all three are disclosed. |
| `gross_subscription_rate` | float64 | 3,494 / 3,860 | subscriptions / nav_begin. |
| `gross_redemption_rate` | float64 | 3,494 / 3,860 | abs(redemptions) / nav_begin. |
| `net_flow_rate` | float64 | 3,860 / 3,860 | net_flow / nav_begin. |
| `churn_rate` | float64 | 3,494 / 3,860 | (subscriptions + abs(redemptions)) / nav_begin. |
| `gross_legs_disclosed` | bool | 3,860 / 3,860 | False where the manager's template carries only the net line. Then the two gross rate columns are unknown, not zero. |
| `flow_asymmetry` | float64 | 3,479 / 3,860 | (subs - abs(reds)) / (subs + abs(reds)), in [-1, 1]. Missing when there was no flow. |
| `manager_id` | object | 3,860 / 3,860 | Fund management company. |
| `fund_key` | object | 3,860 / 3,860 | Filename prefix used as the identity key. |
| `fund_name` | object | 3,860 / 3,860 | Fund name as registered in sources.yaml. |
| `asset_class` | object | 3,860 / 3,860 | equity, balanced or bond. |
| `index_begin` | float64 | 3,860 / 3,860 | Last VN-Index close at or before period_start. |
| `index_end` | float64 | 3,860 / 3,860 | Last VN-Index close at or before period_end. |
| `market_return` | float64 | 3,860 / 3,860 | VN-Index return over this row's own period window. |
| `excess_return` | float64 | 3,860 / 3,860 | gross_return - market_return. |
| `month` | object | 3,860 / 3,860 | period_end month, YYYY-MM. |
| `deposit_rate_pct` | float64 | 2,976 / 3,860 | Curated 12-month deposit rate for the period-end month. Mixes constructs; see deposit_rate_source. |
| `deposit_rate_provenance` | object | 3,224 / 3,860 | Per-row provenance for the rate, naming the snapshot or citation. |
| `deposit_rate_tenor` | object | 3,224 / 3,860 | Always 12 months. |
| `deposit_rate_source` | object | 2,976 / 3,860 | Which series the rate came from. |
| `deposit_rate_bank` | object | 3,224 / 3,860 | Bank or bank group behind the rate. |

## Companion files

| File | Contents |
|---|---|
| `vngross_fund_month.csv` | fund-month rollup. Flows sum, returns compound, NAV takes the month's first opening and last closing so the identity survives aggregation. |
| `quarantine.csv` | rows excluded from the panel, each with a written reason. |
| `continuity_breaks.csv` | an opening NAV that does not match the prior closing NAV, meaning a filing is missing between them. |
| `superseded_duplicates.csv` | filings collapsed as republications or cumulative overlaps. |
| `parse_failures.csv` | filings that could not be parsed at all, with the reason. |
| `measurement_error_diagnostics.csv` | units-outstanding proxy errors per row. Evidence for reading flows rather than inferring them; never panel values. |
| `fmarket_cross_check.csv` | every parsed NAV per unit against an independent source. |

## Reading the exclusions

The four exclusion files are part of the dataset, not an appendix to it. A row
absent from the panel appears in exactly one of them with a reason. Anyone
computing a multi-period quantity should read `continuity_breaks.csv` first: a
gap that goes unnoticed turns an unobserved period into a fabricated flow.