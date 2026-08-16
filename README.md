# vn-gross-flows

A reproducible panel of Vietnamese open-ended fund flows, built from Appendix
XXIV of Circular 98/2020/TT-BTC: the Ministry of Finance template every licensed
fund files on each dealing period.

Every number traces to a filing.
Every excluded row carries a reason.

## What makes it useful

Commercial fund databases carry **net** flows, because that is all most
regulators require. Vietnamese funds disclose the gross subscription and
redemption legs as separate lines, and this panel keeps them apart all the way
through rather than collapsing them at ingest. That is the novel content, and
the name says so.

The alternative approach, inferring flows from changes in units outstanding, is
implemented only as a diagnostic and never as a panel value.
Measured on this sample its error has a median of 0.62% and a 90th percentile of
3.03%, and the error tracks the period return, which would bias exactly the
flow-performance estimates such a panel is built to support.
See [METHOD.md](METHOD.md) section 2.1.

## Current coverage

| | |
|---|---|
| Observations | 3,860 fund-periods |
| Funds | 19, across 4 managers |
| Span | 2021-01-04 to 2026-08-13 |
| Frequency | weekly; 95.7% of rows span exactly 6 days |
| Reconciliation residual | **0.00 VND on every row** |
| Quarantined rows | 23, each with a written reason |
| Fmarket cross-check | 886 of 888 matched rows agree within 0.1% |

| Manager | Funds | Rows | From |
|---|---|---|---|
| VinaCapital | VEOF, VESAF, VFF, VIBF, VLBF | 1,387 | 2021-01 |
| DCVFM (Dragon Capital) | DCDS, DCDE, DCBF, DCIP, DCBC | 1,070 | 2021-06 |
| VCBF | BCF, MGF, TBF, FIF, AIF | 921 | 2022-07 |
| SSIAM | SSI-SCA, SSIBF, SSI-EF, VLGF | 482 | 2021-07 |

All four managers are `verified` in `sources.yaml`, meaning a filing has been
fetched, parsed and added as a fixture. An unverified manager is excluded from
runs by construction: `discover_filings` raises rather than guessing.

## Install

```bash
python -m venv .venv && .venv/bin/pip install -e ".[market,dev]"
```

`vnstock`, used only for VN-Index, is an optional extra so the parser and quality
gates install and test without it.

## Run

```bash
python -m vngross.run all
```

With no manager named, every verified manager runs. Name them to narrow it, but
note that `panel` writes one combined output: naming a subset **overwrites the
panel with only that subset**.

```bash
python -m vngross.run discover dcvfm
```

```bash
python -m vngross.run fetch dcvfm
```

```bash
python -m vngross.run parse dcvfm
```

```bash
python -m vngross.run panel vcbf vinacapital ssiam dcvfm
```

```bash
python -m vngross.run analysis
```

Stages are independent and safe to re-run.
`fetch` is content-addressed and skips anything already cached, so iterating on
the parser never refetches.
Requests are limited to one per second per host with an identifying user agent.

```bash
python -m pytest
```

## Outputs

Written to `data/output/`:

| File | Contents |
|---|---|
| `vngross_fund_period.csv` | the panel: one row per fund per dealing period |
| `vngross_fund_month.csv` | monthly rollup; flows sum, returns compound, NAV takes the month's first opening and last closing |
| `quarantine.csv` | rows failing a gate, each with a written reason |
| `continuity_breaks.csv` | missing filings, detected as opening NAV not matching the prior close |
| `superseded_duplicates.csv` | republished filings collapsed to one observation |
| `measurement_error_diagnostics.csv` | units-outstanding proxy errors, the evidence for reading flows rather than inferring them |
| `fmarket_cross_check.csv` | every parsed NAV per unit against an independent source |
| `parse_failures.csv` | every filing that could not be parsed, with the reason |
| `analysis_paired_weekly.csv`, `analysis_paired_monthly.csv` | the paired net-versus-gross estimates |

`data/deposit_rate_12m.csv` is a curated series, tracked in git, with
per-observation provenance.
It is not scraped at run time.

## Key columns

| Column | Definition |
|---|---|
| `subscriptions` | gross subscription inflow, VND, positive, line 3.2.1 |
| `redemptions` | gross redemption outflow, VND, negative, line 3.2.2 |
| `net_flow` | line 3.2 where disclosed, else the sum of the gross legs |
| `nav_begin`, `nav_end` | total NAV at each end of the period |
| `gross_subscription_rate` | `subscriptions / nav_begin` |
| `gross_redemption_rate` | `abs(redemptions) / nav_begin` |
| `net_flow_rate` | `net_flow / nav_begin` |
| `churn_rate` | `(subscriptions + abs(redemptions)) / nav_begin` |
| `flow_asymmetry` | gross legs netted over gross legs summed, in [-1, 1] |
| `market_return` | VN-Index return over this row's own period window |
| `deposit_rate_pct` | curated 12-month deposit rate; see the caveats below |
| `deposit_rate_source` | `agribank-board-rate` or `cafef-big-four` |
| `deposit_rate_provenance` | how that rate was obtained, per row |
| `period_days` | period length; check before pooling, as frequency varies |
| `date_conflict` | set when the filing's bilingual header disagrees with itself |
| `template_variant` | `standard` or `alt`; see [METHOD.md](METHOD.md) section 4.6 |
| `gross_legs_disclosed` | false where the manager files a net-only template |
| `reconcile_residual_vnd` | identity residual; 0.00 throughout |

All flow rates are scaled by **beginning**-of-period NAV.
Using closing or average NAV would put the flow inside its own denominator.

## Read before using

Two things need stating up front.

**Not every fund discloses the gross legs.**
3,494 of 3,860 rows do. SSIAM's SSIBF and SSI-EF, and VinaCapital's VLBF under
its older template, file a reduced Appendix XXIV carrying only the combined net
line. Check `gross_legs_disclosed` before using `gross_subscription_rate` or
`gross_redemption_rate`; where it is false those columns are missing rather than
zero, deliberately.

**The deposit rate is the weakest series in this build.**
36 of 50 months are observed, 10 are bridged between observations that agree on
the same level, and 4 are left missing (2025-11 to 2026-02).
There is no carry-forward: a month is filled only when bracketed on both sides by
the same measured level, because an audit found that one-sided carry produced a
wrong value in 10 of 18 cases.
It covers 77% of panel rows.
The column mixes several constructs - a single bank's board rate, a state-owned
average, a big-four mean, and a market-wide average - so check
`deposit_rate_source` before treating it as one series.
21 of the 23 observations are Agribank alone, because it is the only big-four
bank whose historical table is recoverable; the others render client-side and
were never captured.
The remaining 2 are true four-bank means recovered from CafeF's underlying JSON,
and they anchor the 2026 tail.
Check `deposit_rate_source` before treating the column as a market average.
[METHOD.md](METHOD.md) section 6 has the full account.

**Continuity breaks are reported, not quarantined.**
There are 101 across 19 funds, each recording an opening NAV that does not match
the prior closing NAV.
Every one traces to a filing the manager never published, published broken, or
that the panel quarantined; none is an unexplained crawler hole.
Rows either side of a gap are individually valid and reconcile exactly, so they
stay in the panel.
Anyone computing a multi-period quantity should consult
`continuity_breaks.csv` first, because a gap that goes unnoticed turns an
unobserved period into a fabricated flow.

The survivorship, distribution-adjustment and single-manager limitations are in
[METHOD.md](METHOD.md) section 7.

## Layout

```
vngross/
  appendix_xxiv.py   three container readers, one mapping layer
  reconcile.py       identity gate, chain continuity, proxy diagnostics
  discover.py        enumerate filing URLs per manager
  fetch.py           content-addressed cache, rate limited
  macro.py           VN-Index, deposit rate, Fmarket cross-check
  panel.py           dedupe, quarantine, flow measures, joins, monthly rollup
  analysis.py        paired net-versus-gross specifications, cluster-robust OLS
  run.py             stage driver
  sources.yaml       manager and fund registry
fixtures/            real filings, one per format variant that broke an assumption
scripts/
  build_deposit_rate.py
```

The parser reads three containers, PDF, XLSX and raw text, because VCBF changed
format in December 2022.
All three produce the same intermediate and hand it to a single mapping layer, so
line-code rules cannot drift between formats.

## Data sources

| Source | Role |
|---|---|
| VCBF investor relations | primary filings, enumerated from a paginated listing |
| VinaCapital information disclosure | primary filings, via a WordPress admin-ajax action |
| SSIAM fund pages | primary filings, all links embedded in one page per fund |
| DCVFM `maintenance.dragoncapital.com.vn` | primary filings, via 50 report sitemaps |
| Fmarket public API | independent NAV per unit, cross-check only |
| vnstock (VCI) | VN-Index daily closes |
| Agribank via Wayback Machine | 12-month deposit board rate, 21 months |
| VNDIRECT Research PDFs | 12-month commercial-bank average, 6 months |
| Shinhan Securities Vietnam PDF | 12-month state-owned-bank average, 4 months |
| Press reports, verified per figure | 12-month state-owned rate, 3 months |
| CafeF CDN JSON, live and via Wayback | big-four 12-month mean, 2 months |
| State Bank of Vietnam | monthly rate release, used as a level cross-check |

Source terms were checked on 2026-08-16 and are summarised in
[METHOD.md](METHOD.md) section 9. Raw filings are never redistributed from this
repository; only derived figures and the reference lists needed to refetch them.

Filings are enumerated, never constructed by URL pattern: VCBF filenames carry an
unpredictable sequence suffix and the year directory in the path does not reliably
match the filing's year.
