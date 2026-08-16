# DCVFM crawler: implementation handoff

**Repo:** `vn-gross-flows`  
**Status:** implemented and validated  
**Last live run:** 2026-08-16  
**Manager status:** `verified`

This document is now a planner/orchestrator handoff, not an unimplemented task
brief. Do not rebuild the crawler or repeat the archive investigation. The live
weekly DCVFM pipeline has been run through discovery, fetch, parse, panel and
monthly aggregation. The remaining decisions are listed in section 9.

## 1. Production decision: weekly only

The production DCVFM panel includes only files whose **enumerated filename**
contains `_BC_TUAN_`.

| Filename marker | Meaning observed | Production action |
|---|---|---|
| `_BC_TUAN_` | Weekly Appendix XXIV change report | Include |
| `_BC_Ky_` | Valid DCBF subweekly change window | Exclude |
| `_BC_Ngay_` | Daily point-in-time NAV, no section 3 | Exclude |
| `_BC_THANG_` | Monthly manager report, not verified as the required flow filing | Exclude |

This supersedes the original instruction to include `_BC_Ky_`. All 178 live
`_BC_Ky_` references belong to DCBF and overlap its `_BC_TUAN_` windows. For
example, in August 2024 DCBF publishes subperiods 02-06 and 07-08 alongside the
weekly 02-08 report. Keeping both frequencies would double-count flows and
produce false chain-continuity failures.

Monthly data must be built by aggregating retained weekly filings with
`panel.to_monthly`; do not ingest `_BC_THANG_` merely because it is monthly.

No bulk `_BC_Ngay_` archive was downloaded. One real daily workbook remains in
`fixtures/dcds_daily_20260813.xlsx` solely to prove the parser rejects that
look-alike document.

## 2. What was implemented

### Registry

`vngross/sources.yaml` now registers DCVFM as `verified` with crawler `dcvfm`
and five stable filename-prefix identities:

| `fund_key` | Code | Asset class |
|---|---|---|
| `dcds` | DCDS | equity |
| `dcde` | DCDE | equity |
| `dcbf` | DCBF | bond |
| `dcip` | DCIP | bond |
| `dcbc` | DCBC | equity |

Fund names and classes were checked against the names printed inside live
filings. Panel identity comes from the filename prefix, never the document's
printed code or the report-page slug.

### Discovery

`vngross/discover.py` contains and registers `_discover_dcvfm`.

Source archive:

```text
https://maintenance.dragoncapital.com.vn/sitemap.xml
```

The current Salesforce site remains a dead end; its shadow-root document list
has no replayable endpoint. The maintenance WordPress site is the production
source.

Discovery behavior:

1. Enumerate the 50 `report-sitemap*.xml` files.
2. Cache their 27,712 unique report-page URLs in
   `data/interim/dcvfm_pages.json`.
3. Scope the cache to the sitemap URL and `max_pages`, validate its inventory,
   and refresh it after 24 hours.
4. Visit only registered-fund report-page slugs containing `tuan` or the one
   legacy English spelling `week`. Daily and `ky` pages are not requested.
5. Extract, decode and deduplicate file links from each page.
6. Use the file prefix as authoritative `fund_key` and emit only `_BC_TUAN_`.
7. Emit `DeadRef` records for page failures, missing files, identity mismatches
   or unknown `_BC_` naming variants.

The crawler never constructs a filing URL.

### Fixtures and tests

Real fixtures:

- `fixtures/dcds_20250116.xlsx`: weekly filing; positive subscriptions,
  negative redemptions, residual 0.00 VND.
- `fixtures/dcds_daily_20260813.xlsx`: daily point-in-time report; required to
  raise `ParseError` for missing section 3.

Discovery tests cover caching, stale-cache refresh, empty child sitemaps,
`max_pages`, daily/period-page avoidance, filename-prefix identity, duplicates,
missing links and unknown `_BC_` variants.

### Overlap deduplication and continuity

`panel.deduplicate` now also handles cumulative reports sharing one opening
boundary. DCBF published both 2025-01-17/21 and 2025-01-17/23 as weekly reports.
The longer Jan 23 window is retained and the shorter row is written to
`superseded_duplicates.csv`; retaining both would count Jan 17-21 twice.

`build_fund_period_panel` now runs continuity on the exact Filing objects that
passed every panel gate. A quarantined filing can no longer hide a break by
bridging two retained rows.

Monthly output now carries `reconcile_residual_vnd`. It exposes a missing or
excluded filing inside a month. A break crossing a month boundary can still
have a zero monthly residual, so `continuity_breaks.csv` remains authoritative.

No changes were made to `appendix_xxiv.py`.

## 3. Final live-run accounting

### Enumerated weekly references

`data/interim/refs_dcvfm.json` contains exactly 1,077 unique references:

| Fund | `_BC_TUAN_` refs | Retained panel rows | Fund-months |
|---|---:|---:|---:|
| DCBC | 121 | 120 | 29 |
| DCBF | 270 | 267 | 63 |
| DCDE | 148 | 148 | 35 |
| DCDS | 269 | 268 | 63 |
| DCIP | 269 | 267 | 63 |
| **Total** | **1,077** | **1,070** | **253** |

Reference inventory checks:

- `_BC_TUAN_`: 1,077
- `_BC_Ky_`: 0
- `_BC_Ngay_`: 0
- References with a fetched path: 1,074
- Filename-date span: 2021-06-17 to 2026-08-13

Exact row accounting:

```text
1,077 references
-   3 unavailable source files
-   3 quarantined date-header defects
-   1 superseded cumulative overlap
= 1,070 retained fund-weeks
```

All 1,073 unique parsed filings reconcile to **0.00 VND**. The retained panel's
maximum `abs(reconcile_residual_vnd)` is **0.00 VND**.

## 4. Unavailable source files

Repeated fetches confirmed three genuine archive failures. They remain in
`fetch_failures_dcvfm.csv`, `parse_failures_dcvfm.csv`, the final
`parse_failures.csv`, and `sources.yaml`.

| Fund | Week ending | Failure | Source-page finding |
|---|---|---|---|
| DCBC | 2022-12-08 | HTTP 404 | Page still advertises the exact broken Azure URL |
| DCIP | 2022-07-14 | zero-byte body | Page still advertises the exact Azure URL |
| DCIP | 2023-02-09 | zero-byte body | Page still advertises the exact Azure URL |

Do not construct replacement URLs. A future recovery attempt must enumerate an
independent mirror or archived copy and preserve provenance.

## 5. Quarantine

Three internally reconciling filings are quarantined because their printed
period dates are impossible. The parser correctly records the document instead
of guessing a date.

| Fund | File | Parsed period | Reason |
|---|---|---|---|
| DCBF | `DCBF_BC_TUAN_20210708.xlsx` | 2021-06-02 to 2021-07-08 | 36 days; outside the 0-35 gate |
| DCBF | `DCBF_BC_TUAN_20230112.xlsx` | 2022-01-06 to 2023-01-12 | 371 days; header year defect |
| DCDS | `DCDS_BC_TUAN_20210624.xlsx` | 2021-10-18 to 2021-06-24 | reversed dates, -116 days |

Do not loosen the parser or period-length gate. Recovery requires an
independent authoritative copy or a separately approved correction layer that
keeps the printed and corrected dates side by side.

## 6. Supersession

One row is intentionally removed as a cumulative overlap:

| Fund | Superseded | Retained | Reason |
|---|---|---|---|
| DCBF | 2025-01-17 to 2025-01-21 | 2025-01-17 to 2025-01-23 | Same opening boundary; later close chains to the next week |

The exclusion is written to `data/output/superseded_duplicates.csv`.

## 7. Continuity and monthly residuals

The final retained weekly panel has six continuity breaks. All are explained by
an unavailable or quarantined filing; there are no unexplained crawler holes.

| Fund | Break reported at | Cause |
|---|---|---|
| DCBC | 2022-12-15 | Unavailable week ending 2022-12-08 |
| DCBF | 2021-07-15 | Quarantined week ending 2021-07-08 |
| DCBF | 2023-01-26 | Quarantined week ending 2023-01-12 |
| DCDS | 2021-07-01 | Quarantined week ending 2021-06-24 |
| DCIP | 2022-07-21 | Unavailable week ending 2022-07-14 |
| DCIP | 2023-02-16 | Unavailable week ending 2023-02-09 |

Five fund-months have a nonzero monthly reconciliation residual:

| Fund-month | Residual (VND) |
|---|---:|
| DCBC 2022-12 | +12,739,019,235 |
| DCBF 2021-07 | +15,289,558,148 |
| DCBF 2023-01 | -1,806,106,182 |
| DCIP 2022-07 | +4,620,203,273 |
| DCIP 2023-02 | -657,576,139 |

The DCDS break crosses a month boundary, so neither individual month must carry
a nonzero residual. This is why downstream users must consult continuity as
well as the monthly residual column.

## 8. Validation evidence

Final validation completed 2026-08-16:

```text
python -m pytest -q
129 passed
```

Additional verified facts:

- Five DCVFM funds in weekly and monthly outputs.
- 1,070 retained weekly rows.
- 253 monthly rows.
- Zero daily or `_BC_Ky_` references in the production refs file.
- Zero period-level reconciliation residual on every retained row.
- Exact and disjoint source accounting across panel, failures, quarantine and
  supersession.
- All six retained-panel continuity mismatches reproduce
  `continuity_breaks.csv` exactly.
- Independent final audit: PASS.

Fmarket cross-check warnings are expected because no `fmarket_id` is registered
for these five DCVFM funds. This is a missing optional independent check, not a
panel gate.

## 9. Remaining planner/orchestrator decisions

### A. Build the combined manager panel

The current files under `data/output/` were last generated with:

```bash
.venv/bin/python -m vngross.run panel dcvfm
```

They are therefore **DCVFM-only**, not the combined project panel. If the next
milestone is the full dataset, run:

```bash
.venv/bin/python -m vngross.run panel vcbf vinacapital ssiam dcvfm
```

Then verify manager/fund counts, parse failures, quarantine, supersession and
continuity again. Do not copy the DCVFM-only row counts in this handoff into the
combined README without rerunning that command.

### B. Decide whether to recover the three unavailable weeks

Possible future work: enumerate Wayback snapshots or an independent disclosure
mirror. This is optional because all three holes are explicit and continuity
reports them. Never manufacture an Azure URL or interpolate flow values.

### C. Decide whether to correct the three bad date headers

Default recommendation: keep them quarantined. Any correction requires a
written provenance rule and tests; do not edit the parser to infer dates from
filenames.

### D. Optional independent NAV cross-check

The task brief identified
`https://api.dragoncapital.com.vn/nav/getAllLatestNav.php`, but it is a stale
point-in-time snapshot with no history or flows. Use it only after checking
freshness, and only as a NAV-per-unit cross-check.

### E. Documentation refresh after the combined run

`README.md` and `METHOD.md` still describe older coverage and should be updated
only after the combined panel has been regenerated and audited.

### F. Optional raw-cache cleanup

The earlier exploratory run fetched 178 `_BC_Ky_` files before the weekly-only
decision. They are excluded from refs and outputs but may remain in the
content-addressed raw cache. Deleting cached source files is not required for
correctness; do it only as an explicit storage-cleanup task.

## 10. Safe rerun commands

```bash
.venv/bin/python -m vngross.run discover dcvfm
.venv/bin/python -m vngross.run fetch dcvfm
.venv/bin/python -m vngross.run parse dcvfm
.venv/bin/python -m vngross.run panel dcvfm
.venv/bin/python -m pytest -q
```

Discovery is safe but not free: the 27,712-page sitemap inventory is cached,
then roughly 1,080 weekly candidate report pages are visited at the mandatory
one-request-per-second-per-host limit. Fetch is content-addressed and skips the
1,074 files already cached.

## 11. Non-negotiable constraints

1. Enumerate file URLs; never construct them.
2. Keep the one-request-per-second-per-host rate limit and identifying user
   agent.
3. Never include `_BC_Ngay_`, `_BC_Ky_` or `_BC_THANG_` in the production panel
   without a new explicit methodological decision.
4. Never infer authoritative dates from filenames or paths.
5. Never drop failures, quarantine, supersession or continuity breaks silently.
6. Do not loosen `appendix_xxiv.py` to rescue malformed or incompatible files.
7. Legacy Circular 183 funds, ETFs, OCR and binary `.xls` support remain out of
   scope.
