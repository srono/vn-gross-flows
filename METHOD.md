# Method

Version 2.0.
Built 2026-08-16 against the VCBF, VinaCapital, SSIAM and DCVFM archives as they
stood that day.

This document records how the panel is constructed, which decisions are load
bearing, and what a reviewer should distrust.
It is written to be read before the data.

---

## 1. What the panel is

One row per fund per dealing period, built from Appendix XXIV of Circular
98/2020/TT-BTC, the Ministry of Finance template every licensed Vietnamese
open-ended fund must file on each dealing period.

Current coverage, from the full run of 2026-08-16:

| | |
|---|---|
| Managers | 4: VinaCapital, DCVFM, VCBF, SSIAM |
| Funds | 19 |
| Rows | 3,860 fund-period observations |
| Span | 2021-01-04 to 2026-08-13 |
| Frequency | weekly; 95.7% of rows span exactly 6 days |
| Quarantined | 23 |
| Reconciliation residual | 0.00 VND on every row |

| Manager | Funds | Rows |
|---|---|---|
| VinaCapital | VEOF, VESAF, VFF, VIBF, VLBF | 1,387 |
| DCVFM | DCDS, DCDE, DCBF, DCIP, DCBC | 1,070 |
| VCBF | BCF, MGF, TBF, FIF, AIF | 921 |
| SSIAM | SSI-SCA, SSIBF, SSI-EF, VLGF | 482 |

3,494 of the 3,860 rows carry the gross legs. The remainder come from managers
filing a reduced template that discloses only the net line; `gross_legs_disclosed`
marks them.

The novel content is the **gross decomposition**.
Subscriptions and redemptions are carried as separate disclosed cash flows, not
collapsed into a net number.

---

## 2. The measurement that makes this worth doing

### 2.1 Flows are read, not inferred

Lines 3.2.1 and 3.2.2 of the template are the fund's disclosed gross
subscription and redemption cash flows.
The panel takes them as reported.

It would be possible to estimate flows instead as `change in units outstanding x
NAV per unit`, since both are derivable from the same filing.
The panel never does this, and the reason is measured rather than asserted.

Flows transact at intra-period dealing NAVs, so the proxy's error moves with the
period return.
Across all 920 diagnosable rows:

| Statistic | Value |
|---|---|
| Median absolute error of the end-NAV proxy | 0.62% |
| 90th percentile | 3.03% |
| Maximum | 125% |

On the reference filing (VCBBCF, week ending 2022-11-21) the proxy overstates
the outflow by 3.19% in a week when NAV per unit rose 3.37%.
The sign of that error is not random: it tracks the return.

That is the problem.
An error in the dependent variable that correlates with the main regressor
biases every flow-performance estimate, in a direction that manufactures the
result such studies look for.
A midpoint-NAV proxy reduces the error but does not remove it, and only helps in
647 of 920 rows.

`reconcile.proxy_divergence` computes both proxies on every row and writes them
to `data/output/measurement_error_diagnostics.csv`.
They exist as evidence for this section and must never be used as panel values.

### 2.2 Flows are scaled by beginning-of-period NAV

`nav_begin`, never closing or average NAV.
Scaling by a denominator the flow has already entered puts part of the flow on
both sides of the regression and manufactures a relationship.

### 2.3 Returns align to the filing's own window

Each row's market return is measured between its own `period_start` and
`period_end`, using the last VN-Index close at or before each boundary.
Vietnamese dealing periods shift around Tet and public holidays, so a fixed
calendar week would misalign flow and return by a day or more precisely when
markets are most eventful.

The boundary rule also means a non-trading boundary date does not produce a
missing return.

---

## 3. The quality gate

Every filing must satisfy

```
nav_end = nav_begin + chg_investment + subscriptions + redemptions + chg_distribution
```

This is the whole quality control.
It is an internal consistency condition on six independently printed numbers,
so a parse that misreads a column, drops a sign, or slips a digit cannot satisfy
it by accident.

All 3,860 rows in the panel close this identity to **0.00 VND**, across four
managers, three container formats, two template variants and five years.
Not "within tolerance" - the residual is exactly zero at float precision on
every row.
The tolerance in `reconcile.ABS_TOL_VND` exists only to absorb float
representation of 1e14-magnitude values and has never been needed.

A blank cell is absent, not zero.
`chg_distribution` is blank whenever no distribution was paid, and the parser
records absence rather than fabricating a `0.0`.
Zero is substituted only inside the identity arithmetic, and the check's detail
string names every line it treated that way.

A failing row is quarantined with a written reason, never dropped. 23 rows are
currently quarantined: three DCVFM filings whose printed period dates are
impossible (36 days, 371 days, and one running backwards by 116 days), seven
VinaCapital rows, and thirteen SSIBF rows carrying small unreconciled residuals
between 0.003% and 0.17% of NAV that have not yet been diagnosed individually. The parser records what the
document says rather than guessing a plausible date, and the period-length gate
catches the result.

The same rule applies to filings that cannot be parsed at all. 1,620 are listed
in `data/output/parse_failures.csv`. Discarding that list once hid an entire
fund, VinaCapital's VLBF and its 255 filings, behind a panel that looked
complete; the panel stage now writes it and logs a per-fund breakdown.

### 3.1 Independent validation

Internal consistency cannot detect an error that is consistent.
So every parsed `nav_per_unit_end` is compared against Fmarket's independent NAV
feed:

| | |
|---|---|
| Panel rows with an Fmarket observation | 888 of 921 |
| Agree within 0.1% | **886 (99.77%)** |
| Median absolute difference | 0.000% |
| Rows differing by more than 0.1% | 2 |

Two of five funds agree on 100% of rows.
The two exceptions differ by 0.56% and 0.17% on single dates and are more likely
Fmarket revisions than parse errors.

Fmarket stamps NAV with its publication date, one day after the filing's dealing
date.
Matching on `period_end` exactly agrees on about 1% of rows; matching on
`period_end + 1` agrees on 96% at a 0.1% tolerance.
An earlier nearest-date match with a 3-day tolerance produced apparent 9% errors
on 2025-04-09, a day the index fell about 9%: the date slack was converting
market moves into phantom parse errors.
The lag is now explicit in `macro.FMARKET_PUBLICATION_LAG_DAYS`.

Fmarket returns NAV per unit only, with no units outstanding, no total NAV and no
flow legs.
It can cross-check a parse; it can never substitute for a filing.

---

## 4. Where the build spec was wrong

The spec was verified against a single PDF filing.
Seven of its assumptions did not survive contact with the full archive, and the
last two were only found once coverage extended beyond one manager.
They are recorded because each one would have silently corrupted the panel.

### 4.1 The archive is mostly XLSX, not PDF

VCBF changed container in December 2022.
Of 935 listed filings, 88 are PDF (2022-07 to 2022-11) and 845 are XLSX.
A PDF-only parser would have covered 9% of the data.

The template, its line codes and the identity are unchanged across containers.
Three readers therefore feed one mapping layer, and only that layer knows
`FIELD_MAP`, so the mapping rules cannot drift between formats.

### 4.2 Labels are inline, and the spec's fixture is idealised

The spec's golden fixture puts each line code alone on its row.
The real text layer does not:

```
1.1 của quỹ/ per Fund 302,234,351,773 303,392,880,988
```

The spec's rule, that every token after the code is numeric, fails on this.
Worse, the header line `4 Kỳ báo cáo: ... năm 2022` opens with a real line code
and ends in digits, so a trailing-number rule reads line 4 as 2022.

`parse_pdf` therefore assigns values by word x-coordinate.
Both numeric columns are right-aligned, at x1 about 424.5 and 515.0 on the
reference filing, and the column edges are inferred per document rather than
hardcoded.
This also makes blank cells genuinely blank: a lone number under "Last period"
is recorded as a prior value, not promoted into the current column.

`parse_text` is retained as the spec's contract and now takes the trailing run of
numeric tokens, which handles inline labels.
It cannot distinguish a single trailing number from a blank current cell; that is
why the PDF path uses geometry instead.

The fixture is kept byte-exact as the spec defines it, and the live filing is
verified to parse to an identical `values` dict.

### 4.3 The English half of the header is unreliable

The spec directs parsing dates from the English half of the bilingual header.
Real filings contradict it.
`vcbbcf_bc_tuan_20240103_1.xlsx` reads:

```
D9  Từ ngày 28 tháng 12 năm 2023 đến ngày 03 tháng 01 năm 2024     (correct)
D10 From 28 Dec 2023 to 03 Dec 2024                                (mistyped)
```

Trusting English dates that row eleven months late, which misaligns its return
window and inverts its position in the chain.
Eight filings carry such disagreements.

Vietnamese is now authoritative, English is the fallback, and any disagreement is
recorded in the `date_conflict` column rather than resolved silently.

VCBF-MGF compounds this by stating the year once:
`From 02 Jan to 08 Jan 2025`.
Neither the spec's pattern nor a full-form pattern matches, and before this was
handled all 214 MGF filings parsed with no dates at all, which would have
quarantined 23% of the panel.
The compressed form infers the start year from the end year, decrementing it when
the start month is later, so a period straddling New Year resolves correctly.

### 4.4 Line 6.3 has different units per container

PDF prints `20.54%`, which parses to 20.54.
XLSX stores the same cell as the fraction `0.2054` under a percent number format.
Unnormalised, foreign ownership would be 100 times too small on 90% of the panel.

The XLSX reader scales percent-formatted cells so both containers land on percent
units, verified against `foreign_value / nav_end` computed independently.

The printed ratio does not always equal that quotient, so the panel carries both:
`foreign_ownership_pct` as disclosed and `foreign_share_of_nav` as derived.

### 4.5 The fund was renamed mid-archive

The in-document code is `VCBBCF` in 2022 and `VCBF-BCF` from 2024.
It is one fund.
Keying identity on the printed code would split it in two and fabricate a
continuity break at the rename.

Identity comes from the filename prefix, which is stable across the whole
archive, resolved through `sources.yaml`.

---

### 4.6 A second template with different code semantics

VinaCapital's pre-2022 layout is the same regulation and the same identity, but
four codes mean different things. It is the most dangerous document in the
corpus, because read with the standard map it produces a filing that looks
complete and is wrong.

| Code | Standard | Alternate layout |
|---|---|---|
| 1.2 / 2.2 | blank, per-lot, ETF only | NAV per certificate |
| 3.2.1 | gross subscriptions | change from distribution |
| 3.2.2 | gross redemptions | change from subscription and redemption, **net** |

A naive read files a distribution as a subscription and a net inflow as a
negatively signed redemption, **and the identity still closes**, because the
arithmetic is self-consistent either way. The reconciliation gate cannot catch
this one. Only the fact that the codes sit in column C rather than A made it fail
loudly enough to notice.

Detection is structural: the standard template always carries 1.3 and leaves 1.2
blank, so a valued 1.2 with no 1.3 identifies the alternate. The choice is
recorded per row in `template_variant`; 248 rows are `alt`. The alternate does not
disclose gross legs at all, since its 3.2.2 is already netted.

The code column is now located by scanning for the column that yields the most
recognisable line codes and contains 2.1, rather than assumed to be column A.

### 4.7 One "Appendix XXIV" is not the change report at all

Three managers publish a daily file that is also headed "Phu luc XXIV" and also
carries a line 2.1, but reports NAV *as at* a date rather than its change over a
period. Its 2.1 is the foreign certificate count, not closing NAV, and it has no
section-3 flow lines. Read as a change report it yields a plausible, wrong
nav_end.

`_filing_from_cells` therefore requires at least one section-3 line, verified
safe against all 931 VCBF filings before it was added. Discovery filters these
out by file name as well, so they are never fetched in bulk.

### 4.8 Word grouping is a heuristic, and it fails three ways

The PDF reader originally trusted pdfplumber's `extract_words`. That grouping is
itself a heuristic over the page's characters, and in this corpus it corrupts
values in three distinct ways, each of which produced a filing that looked
complete and was wrong by a specific amount.

**A wrapped label woven through the digits.** When a long label overflows onto
the line holding its value, the label glyphs and the number glyphs occupy
overlapping x-positions, and grouping interleaves them. SSIAM's line 3.1
extracted as `5ro1n,8g1 8k,y490`: the value 51,818,490 with the letters of
"rong ky" threaded through it. The affected row's identity residual was exactly
51,818,490.

**A leading digit split into its own token.** `828,087,665` grouped as `8` and
`28,087,665`. Taking the trailing numeric run silently truncated the figure and
left a residual of exactly 800,000,000.

**A detached value line.** Some filings render one row as a four-line block:
the code, the Vietnamese label, the values about 5pt below the code, then the
English label. The value line carries no code of its own, so vertical banding
never associates it with anything.

The fix reads glyphs rather than words. Numeric characters inside a column's
x-window, taken in x-order, reconstruct the figure exactly, because interleaving
preserves x-order. The window is anchored on the column's right edge and sized
to the widest figure that column actually holds, which is what keeps it from
reaching into the column to its left. A second, additive pass associates a
detached numeric line with the nearest code line above it, filling only slots
the first pass left empty.

Recovered 43 rows and removed 30 quarantines and 13 outright parse failures. The
VCBF reference filing and the golden fixture parse bit-identically before and
after, which is the regression test that matters: the change had to be invisible
where word grouping was already correct.

Three filings still resist. Their PDFs contain the table twice and the first
copy has a clipped text layer where digits are physically absent, so
`3.1 ... trong ky 5 92,029,5` is all that survives of 592,029,546. A clean
second copy exists later in the same document, and preferring whichever copy
satisfies the identity would recover them, but that is a large amount of
machinery to justify on three rows out of 4,543.

## 5. Archive gaps

Chain continuity verifies that each fund's opening NAV equals the prior filing's
closing NAV.
A break means a filing is missing, which matters because an unnoticed gap turns
an unobserved period into a fabricated flow: the next filing's opening NAV
silently absorbs it.

19 breaks remain, and every one is explained:

| Cause | Count |
|---|---|
| Weeks VCBF never published | 17 |
| Weeks published as truncated files | 2 |
| Unexplained | 0 |

Verified by checking, for each break, whether the missing week exists anywhere in
the enumerated listing.
No break is attributable to the crawler.

Two patterns are worth noting.
The week of 2025-05-29 to 2025-06-04 and the week of 2023-10-12 to 2023-10-18 are
missing for all five funds, which points to a manager-wide publishing lapse
rather than per-fund omissions.

The two truncated files are VCBF-BCF and VCBF-AIF for the week ending
2025-11-26, served at 4,096 and 12,288 bytes with matching `Content-Length` while
sibling funds' files for the same week are 40 to 47 KB.
The zip central directory is absent, so the workbooks cannot be opened.
This is not a transfer error and refetching does not fix it.

Two further listing entries advertise a filing but link to
`javascript:void(0);`, so no file was ever published.
These are recorded in `data/interim/dead_vcbf.csv` and in `sources.yaml` rather
than dropped, because an undocumented hole is indistinguishable from data.

Breaks are reported in `data/output/continuity_breaks.csv`.
They are **not** quarantined: the rows either side of a gap are individually
valid and reconcile exactly.
Anyone computing a multi-period quantity must consult that file.

### 5.1 Duplicate republications

VCBF republishes a filing under a new sequence suffix without withdrawing the
old one, so `vcbaif_bc_tuan_20250528_1.xlsx` and `_3.xlsx` are byte-distinct
files carrying identical figures.
Ten such pairs exist.
Left alone they would double-count a week's flow and break continuity.

`panel.deduplicate` collapses filings sharing a fund and period window, keeping
the later suffix as the operative version and writing the superseded row to
`data/output/superseded_duplicates.csv` with a reason distinguishing an identical
republication from a restatement with changed figures.

---

## 6. The deposit rate is the weakest series in this build

Read this section before using `deposit_rate_pct` for anything.

No clean high-frequency 12-month Vietnamese deposit rate series is published.
World Bank and IMF figures are annual and 3-month tenor, wrong on both counts.

The chosen approach was the most defensible option in the spec: hand-assemble
monthly big-four board rates from published rate tables plus Wayback Machine
snapshots.
It only partly succeeded, for a reason worth stating plainly.

**Of the big four, only Agribank is recoverable.**

| Bank | Outcome |
|---|---|
| Agribank | rate table rendered server-side; archived HTML carries the figures |
| Vietcombank | table renders client-side; archived HTML has no figures. Older `portal.vietcombank.com.vn` snapshots are exchange rates, not deposit rates |
| BIDV | archived HTML has a table element but no rate figures |
| VietinBank | no qualifying snapshots of the rate page |

Aggregators were checked.
webgia.com was rejected: it has 37 months of coverage but substitutes the literal
string `webgia.com` for every rate digit as an anti-scraping measure.

**CafeF was partly usable and anchors the tail.**
Its rate page renders client-side, and its archived HTML contains no bank names
at all, so the page itself is useless.
But the page reads a static JSON on the CafeF CDN
(`all_banks_interest_rates.json`) carrying all 29 banks and a clean `12T` tenor.
The Wayback Machine holds exactly one capture of that file, 2026-03-31, and it is
also readable live.

Those two observations matter out of proportion to their number.
They are genuine **four-bank averages** rather than a single bank, and they land
precisely where the Agribank series stops being recoverable.
Both read 5.9%, with all four banks identical.
They also confirm the judgement in the carry-forward rule below: Agribank's last
recoverable value is 4.7% in 2025-07, and rates had risen to 5.9% by 2026-03, so
carrying 4.7% to the end of the panel would have been materially wrong.

The two sources do not overlap, so they cannot be reconciled directly.
What can be checked is that CafeF's Agribank tenor grid has the same shape and
level structure as Agribank's own published table, which it does.

The resulting series covers 2022-07 to 2026-08 as:

| | Months | Construct |
|---|---|---|
| Observed, Agribank board rate | 21 | one bank |
| Observed, VNDIRECT market average | 6 | all commercial banks |
| Observed, Shinhan SCB average | 4 | state-owned commercial banks |
| Observed, press reports | 3 | one bank or state-owned average |
| Observed, CafeF big-four mean | 2 | four banks |
| Bridged between agreeing observations | 10 | - |
| Left missing | 4 | 2025-11 to 2026-02 |

838 of 921 panel rows (91%) receive a rate.
`deposit_rate_source` and `deposit_rate_bank` are on every panel row, so a
single-bank month is never mistaken for a four-bank one.

Three caveats follow, and none of them are minor.

**Most of it is one bank, not four.**
21 of the 23 observed months are Agribank alone.
Agribank is a state-owned commercial bank and one of the big four, so its board
rate is a reasonable reference rather than an outlier, but a single-bank board
rate is not a market average.
Only the two CafeF months are true four-bank means.
The `deposit_rate_bank` and `deposit_rate_source` columns say which is which on
every row.

**There is no carry-forward at all. A month is filled only if it is bracketed.**
An earlier version of this series carried the last observation forward for up to
three months, on the reasoning that board rates are administered prices that hold
between changes.
That reasoning is right about the price and wrong about the inference, and the
data refuted it: of 18 months filled by one-sided carry, **10 sat inside a stretch
where the next observation proved the rate had already moved**.
Holding 5.3% across 2024-01 to 2024-03 overstated a falling market by up to
0.55pp; holding 4.7% into late 2025 missed the turn to 5.9% entirely.

A month is now filled only when the nearest observation before it and the nearest
after it both exist and **agree on the same level**, within a six-month span.
That is interpolation between two measured points at one value.
A one-sided carry is an extrapolation into a period the series has no information
about, and it is exactly where the errors were.

The cost is real: coverage falls from 82% to 62% of panel rows.
That is the right trade. A missing value is a fact about the series; a wrong value
is a defect that propagates into every regression that uses it.

Concretely, this leaves every transitional period missing:
2023-05, 2023-07 to 2023-11, 2024-01 to 2024-04, and 2025-08 to 2026-02 are all
stretches whose bracketing observations disagree, so the rate moved somewhere
inside them and the series declines to guess where.
Each such row's `provenance` names both bracketing observations and their levels.

The eight bridged months are 2023-01, 2024-07, 2024-08, 2025-01 and 2026-04 to
2026-07, every one of them sitting between two observations at an identical
level.

### 6.4 External corroboration

An independently compiled monthly table was checked against this series in
August 2026. Where both had a value, 22 of 28 months agreed within 0.1pp,
including 2023-12 exactly, every month of 2025 exactly, and 2026-08 exactly.
Every month that disagreed by more than 0.1pp was a carried-forward value of
ours, never an observation, which is what prompted the rule change above.

That table is **not** merged into the series. It arrived without a citable
publisher, its values are bucketed into multi-month bands, and its 2026 path is
internally inconsistent with a dated capture: it places 2026-04 to 2026-05 at
5.5% while the CafeF snapshot of 2026-03-31 reads 5.9% for all four big-four
banks. The snapshot was verified as a genuine distinct capture rather than a
stale copy: it carries 28 banks against the live file's 29, and 19 banks differ
on the 12-month tenor.

The SBV monthly release supports the level structure. Its January 2026 figure of
5.1% to 6.5% for 12-to-24-month terms brackets that table's 5.0%, and its June
2026 figure of 5.9% to 7.3% has a lower bound exactly equal to the observed
big-four 5.9%. The big four sit at the bottom of the SBV's cross-bank range,
which is what makes the SBV release usable as a level check but not as a
substitute series.

**Every row carries its own provenance.**
`deposit_rate_provenance` states whether the value was observed, naming the
Wayback snapshot, or carried forward, naming the month it came from and by how
many months.
`attach_deposit_rate` pushes that column into the panel on purpose.
The series is curated and the panel presents it as curated.

`scripts/build_deposit_rate.py` regenerates `data/deposit_rate_12m.csv` and
documents the source selection.
The CSV, not the script, is what the pipeline consumes; `macro.load_deposit_rate`
never touches the network and refuses to load a row without provenance.

### 6.1 The VNDIRECT market average, and why each figure was checked

VNDIRECT Research publishes an average 12-month term deposit rate across
Vietnamese commercial banks in its money-market chartbooks and economic updates.
Six of its month-end observations fill months no board-rate snapshot reaches,
covering most of the 2023 easing cycle and the 2024 trough.

Every figure was verified by fetching the cited PDF and reading the sentence,
not by trusting a summary. The quoted sentence travels into each row's
`provenance` so the claim can be rechecked without refetching. Three candidate
figures failed that check and were excluded:

| Month | Claim | Why excluded |
|---|---|---|
| 2023-11 | 5.30 | the source forecasts rates will "remain at 5.4%/year for the remainder of 2023". A forecast is not an observation |
| 2024-03 | 4.63 | cited only through a click-tracking redirect, and the April update's own "+0.05% pts MoM" implies March near 4.56 |
| 2024-02 | 4.70 | derived from that unverified March figure |
| 2023-05 | 7.00 | the cited PDF says "We **expect** the 12-month deposit interest rate to drop to 7.0% in 2023F". A forecast for year end, not a May level. Shinhan gives the actual month-end figure, 6.80 |
| 2025-10 | 4.80 | the cited URL returns "404 Page Not Found" |
| 2025-12 | 5.20 | the cited article fetches but contains no 12-month rate figure anywhere in its text |
| 2022-07 | 5.60 | right number, wrong month. The article is dated 02/08/2022 and reports the rate "increases from 5.5%/year to 5.6%/year", so 5.60 is the August level and 5.50 the July one. Both are now carried, correctly dated |

2023-08 is included despite being a derivation rather than a printed level,
because two separate reports state it independently: one puts 5.4% on 10 October
"down 0.5% points compared to the end of August", the other 5.6% on 25 September
"down 0.3% points compared to the end of Aug". Both imply 5.9%.

2023-11 then filled itself anyway, through the bracketing rule rather than the
forecast: VNDIRECT's October observation and Agribank's December observation are
both 5.30%, from different sources measuring different constructs. 2024-02 and
2024-03 stay missing, correctly, because their bracketing observations of 5.14%
and 4.61% disagree and the rate was moving through them.

**This is a third construct in one column.** A market-wide average across
commercial banks is not a single bank's board rate and not a big-four mean.
Splicing them is defensible here only because they sit close where they can be
compared: VNDIRECT's 4.61% for April 2024 against Agribank's 4.70% for May 2024,
and VNDIRECT's 5.30% for October 2023 against Agribank's 5.30% for December.
`deposit_rate_source` and `deposit_rate_bank` mark every row, and anyone who
needs a single construct should filter on them rather than take the column
whole.

### 6.2 The Shinhan indicator series, and what the sources agree on

Shinhan Securities Vietnam's Vietnam Economic Update of 25 March 2024 prints a
monthly indicator table whose row "Avg. Deposit rate 12M term of SCBs" gives the
average 12-month rate at state-owned commercial banks at each month end, on one
methodology, for thirteen consecutive months from 2023-02 to 2024-02. Its body
text adds 2024-03: "reaching 4.7% on March 22, 2024". This is the single best
source found for the 2023 easing cycle, and it is the right construct: SCBs are
the big four.

It also makes the whole series checkable, because it overlaps every other source:

| Where | Agreement |
|---|---|
| 2023-02, 2023-03, 2023-04 vs Agribank | exact: 7.40, 7.20, 7.20 |
| 2023-07 to 2024-01 vs VNDIRECT | VNDIRECT sits 0.05 to 0.21pp higher throughout |
| 2023-06 and 2023-12 vs Agribank | Agribank higher by 0.50 and 0.35pp |

The first line is independent confirmation that the Agribank extraction is
correct. The second has the right sign and magnitude: VNDIRECT averages all
commercial banks, Shinhan only state-owned ones, and state-owned banks pay less.
The third looks like disagreement and is not. Both Agribank observations come
from Wayback snapshots taken on the 6th and the 4th of their month, against
Shinhan's month end, in months when rates were falling fast. It is a timing
difference, and it is the clearest evidence in this build that a board-rate
snapshot dated mid-month is not the same measurement as a month-end average.

### 6.3 The rejected alternative, for the record

The SBV publishes a genuinely monthly official series, "Developments of interest
rates applied by credit institutions", as a PDF per month.
It was not used as the primary series for two reasons.
It reports a range across banks bucketed as "6-month to 12-month", so the
12-month tenor sits on a bucket boundary rather than being a point observation.
And the SBV portal migration left older article slugs returning soft 404s, with
the WAF rejecting requests after roughly thirty, so a 50-month backfill is not
reliably retrievable.

It remains the best available cross-check on the level, which is how it is used
in the carry-forward decision above.

---

## 7. Limitations

These are stated first because a reviewer will find them anyway.

**`gross_return` is not distribution adjusted.**
It is `nav_per_unit_end / nav_per_unit_begin - 1`.
Any fund paying distributions on line 3.3 needs a total-return series built
before a performance-chasing test.
Most Vietnamese equity open-ended funds accumulate rather than distribute, and
line 3.3 is blank throughout the current sample, but this must be verified per
fund rather than assumed as coverage extends.

**Unit series break on corporate actions.**
Splits, consolidations and distributions paid in units break any series derived
from units outstanding.
Flows from 3.2.1 and 3.2.2 are immune, which is a further argument for reading
flows rather than inferring them, but NAV-per-unit return series need adjustment.

**Survivorship is unaddressed.**
The sample is VCBF's five currently operating funds.
Closure correlates with poor performance and outflows, so an as-is sample biases
any flow-performance estimate.
Recovering closed and merged funds needs the Vietstock mirror, which is not yet
usable: its document lists load from a tokenised AJAX endpoint rather than static
links, and recovering it needs a browser-driven session.
The DCVFM funds are the main exposure, given the VFM to Dragon Capital
transition.

**Four managers, nineteen funds, none of them closed.**
All four are `verified`; see section 8. Coverage is no longer the constraint.
Survivorship is: every fund in the panel is still operating, and closure
correlates with the outflows this dataset exists to measure.

**Filing frequency varies.**
Most funds file weekly; some file daily.
`period_days` is on every row and must be checked before pooling.
95.7% of the panel is six-day periods; the remainder run from one to thirteen
days. DCBF in particular files on a genuinely variable dealing calendar.

**No pre-2021 template detector.**
Filings before 2021 may use Circular 183/2011, which would need a second parser.
The VCBF archive does not reach that far, so the question is open rather than
answered.

**VCBF's own directory paths are unreliable.**
`vcbbcf_bc_tuan_20240102.xlsx` is served from `/images/2025/`.
Dates come from the document, never from the path, and URLs are enumerated from
the listing rather than constructed.

---

## 8. Extending to further managers

Spec section 10 step 7, worked 2026-08-15 and 2026-08-16, one manager at a time,
each with its own fixture.

| Manager | Status | Funds | In panel | Span |
|---|---|---|---|---|
| VinaCapital | verified | 5 | 1,387 | 2021-01 to 2026-08 |
| DCVFM | verified | 5 | 1,070 | 2021-06 to 2026-08 |
| VCBF | verified | 5 | 921 | 2022-07 to 2026-08 |
| SSIAM | verified | 4 | 439 | 2021-07 to 2026-07 |

The central claim of the build survived: **the Ministry of Finance template is
genuinely identical across managers.** A VinaCapital weekly filing parsed to a
zero-dong residual with no code changes at all. What differed was everything
around the template, and each difference is now pinned by a fixture.

### 8.1 What each manager does differently

**VinaCapital** publishes two files per fund per day, and only one of them is
the change report. The other is titled "Phu luc XXIV" as well, and carries a
line 2.1 as well, but it is a point-in-time NAV snapshot in which 2.1 is the
foreign certificate count rather than closing NAV. Parsed as a change report it
yields a plausible, wrong `nav_end` of 6.6 million dong against a true NAV of
2.3 trillion. The identity gate caught it, but a filter that relies on the gate
is a filter that failed. The parser now requires a section-3 line before it will
accept a document as a change report, which is what distinguishes a change
report from a snapshot. Verified safe against all 931 VCBF filings first.

Weekly and daily are also named three different ways across VinaCapital's own
funds: `BC_Tuan`/`BC_Ngay` for VEOF, `BC_Weekly`/`BC_Daily` for VESAF, and
`NAV-TUAN`/`NAV-NGAY` for VLBF. Matching the whole filename stem missed VLBF
entirely and dumped 4,389 files into the unclassified pile; matching on the
period word alone recovers all five funds.

**DCVFM hides a working archive behind a broken one.** Its current site is a
Salesforce Experience Cloud application whose document list lives inside nested
shadow roots with no replayable endpoint, and it defeated three separate attempts.
The pre-Salesforce WordPress site is still live at
`maintenance.dragoncapital.com.vn`, publishes a sitemap, and enumerates 27,712
report pages across 50 child sitemaps, each linking one file on an Azure CDN.
The lesson generalises: when a manager's site is a single-page application, look
for the estate it replaced before concluding the archive is unreachable.

DCVFM also publishes three documents whose names differ by one word.
`_BC_TUAN_` is the weekly change report and is the only one ingested. `_BC_Ngay_`
is the daily point-in-time snapshot described above. `_BC_Ky_` is a genuine
change report over a subweekly window, but every one of them belongs to DCBF and
falls inside a `_BC_TUAN_` window that is also published, so ingesting both would
count the shared days twice. That the weeklies tile the calendar was checked
rather than assumed: 262 of DCBF's 266 consecutive window pairs are exactly
contiguous, and the only two holes trace to quarantined filings.

The page slug is not a safe filter. A page slugged `...-ky-01-06-2026` links a
`_BC_Ngay_` file, and slugs containing the word `ky` mostly turn out to be
`cbtt-ky-hop-dong-kiem-toan`, the disclosure of signing an audit contract, where
"ky" means "sign" and not "period". Filter on the file name.

**SSIAM files a reduced Appendix XXIV.** Its template carries lines 3.1, 3.2 and
3.3 but has no 3.2.1 or 3.2.2 at all, so the gross subscription and redemption
legs are simply not disclosed. This is the one manager difference that reaches
the research design rather than the plumbing: the gross decomposition is the
novel content of this dataset, and for SSIAM it does not exist.

Two consequences follow. The identity had to learn to close off the disclosed
net line 3.2 when the gross legs are absent, or every SSIAM filing would be
quarantined over a disclosure choice its manager made. And `_derive_flow_measures`
had to stop treating a blank leg as zero. A blank leg means two different
things: no flow at all, when the disclosed net is zero, or not disclosed, when
the net is non-zero. Conflating them would have reported SSIAM as having zero
subscriptions every week. The panel now carries `gross_legs_disclosed` on every
row, and the gross rates are missing rather than zero where the split is not
published.

SSIAM also writes its period numerically in Vietnamese, "tuan tu 21/11/2023 den
27/11/2023", while its English half reads "from Nov 21th 2023" - month-first and
malformed. Another vote for the Vietnamese-first rule from section 4.3.

### 8.2 SSIAM parse quality is materially below the other two

A 32-filing random sample across SSIAM's four funds parsed 23 and reconciled 20
of those to under one dong. The causes are known rather than mysterious:

- six filings from 2018 to 2021 have no line 2.1 in the expected place. These
  predate Circular 98 and are almost certainly the Circular 183/2011 template,
  which answers spec section 9 item 3 in the affirmative: the older template
  does exist in the wild and needs its own parser or a version detector.
- one filing named as a change report is actually a point-in-time snapshot; the
  new guard rejected it correctly.
- one PDF split an accounting parenthesis across tokens. `parse_number` now
  reads a lone leading or trailing bracket as the negative it is.
- three reconcile with residuals in the hundreds of millions, which is a real
  layout difference not yet diagnosed.

SSIAM should be treated as enumerated but not yet production-clean. Its filings
will land in `quarantine.csv` with residuals rather than silently entering the
panel, which is the gate working as intended.

### 8.3 DCVFM was not blocked, only hidden

An earlier version of this document recorded DCVFM as unreachable. Dragon
Capital's current disclosure page is a Salesforce Experience Cloud application:
the sitemap holds four static pages, the HTML carries no document list, the
accessibility tree is empty because the markup lives in nested shadow roots, and
changing a filter fires no interceptable request. All of that remains true.

It was the wrong place to look. The pre-Salesforce WordPress estate is still
served at `maintenance.dragoncapital.com.vn`, with a sitemap, 50 child report
sitemaps and 27,712 report pages, each linking one file on an Azure CDN. 1,077
weekly change reports across five funds were enumerated from it, and 1,070
reached the panel at a zero-dong residual.

The generalisable lesson: a single-page application is a rendering choice, not
a statement about what the manager publishes. Before concluding an archive is
unreachable, look for the estate the current site replaced.

What is still out of reach is the part that matters for survivorship. The legacy
VFM funds, VFMVF1, VFMVF4, VFMVFB, VFMVFA and VFMVSF, are present in the same
archive with thousands of pages going back to 2006, but they file under Circular
183/2011 Appendix 26 rather than Circular 98/2020 Appendix XXIV, and the
pre-2013 filings are scanned images. VF1 to DCDS, VF4 to DCBC and VFB to DCBF
are renames rather than closures, so they would extend history rather than fix
the bias; VFMVFA, live 2015 to 2016, and VFMVSF, 2019 to 2022, are the ones that
look genuinely discontinued.

### 8.4 VCFM is resolved

Spec section 9 item 1 asked whether "VCFM" means VinaCapital Fund Management,
the renamed VinaWealth, or Viet Capital, because conflating them would
double-count a firm. VinaCapital's own disclosure filter answers it: it offers a
`vcfm` entity labelled "VinaCapital (VCFM)", and the filings name the manager
"VinaCapital Fund Management Joint Stock Company". VCFM is that firm. The slug
carries corporate disclosures rather than fund NAV reports, so it is registered
as the manager, not as a fund.

---

## 9. Source terms and redistribution

Checked 2026-08-16, machine-readable signals only. The human-readable terms of
use on each site were **not** individually reviewed, and anyone redistributing
this work should do that before relying on the summary below.

| Host | robots.txt | Bearing on this build |
|---|---|---|
| `www.vcbf.com` | `User-agent: *` → `Allow: /`, plus `Content-Signal: search=yes,ai-train=no,use=reference` | crawling permitted; see below |
| `vinacapital.com` | `Disallow: /landing-page` only | nothing used is disallowed |
| `ssiam.com.vn` | `Allow: /`, sitemap published | nothing disallowed |
| `maintenance.dragoncapital.com.vn` | none served | no stated restriction |
| `vfmcomvnaz.azureedge.net` | none served | no stated restriction |

VCBF is the one that needs care. Its `User-agent: *` group allows crawling, and
this pipeline is not any of the named disallowed agents: it identifies itself as
`vngross/0.1` with a contact address and fetches at one request per second. But
the site sets `ai-train=no` and `use=reference`, so the operator has explicitly
signalled that its content may be used for reference and not for model training.

Two consequences are already baked into how this repository is built, and they
were the right calls independent of the signal:

- **The raw filings are never redistributed.** `data/raw` is gitignored. What
  ships is the derived numeric panel plus the reference lists needed to refetch
  from the primary source. Anyone can rebuild byte-identically; nobody gets a
  mirror of a manager's own documents from here.
- **What ships is facts, not content.** NAV levels and disclosed cash flows from
  mandatory regulatory filings are figures, not expression.

That leaves one judgement that is the publisher's to make rather than this
document's: a factual dataset placed on a public host can foreseeably be
ingested for model training, whatever its own licence says, and one upstream
source has asked not to be. Weigh that before publishing, and say plainly in the
release notes which sources carry which signals.

## 10. Reproducing this

```bash
python -m vngross.run all
```

Stages are independent and re-runnable.
`fetch` skips any URL already cached, so a reparse never refetches; `parse` and
`panel` read only from disk.
Rate limiting is one request per second per host with an identifying user agent,
because these are small managers' servers hosting mandatory disclosures.

The full VCBF fetch is 933 requests, about 16 minutes, and completed with zero
failures on 2026-08-15.

Deposit rate regeneration is separate and deliberately manual:

```bash
python scripts/build_deposit_rate.py
```
