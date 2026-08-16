# Original build specification (v1, historical)

Superseded by METHOD.md. Retained because METHOD.md section 4 records where this
specification turned out to be wrong, and those notes only make sense alongside
the original text.

The project was called `vnflows` at this point.

**Version:** 1.0
**Date:** 2026-08-14
**Owner:** 10thirtyLabs
**Status:** Ready to build. Core template and identity verified against a live filing. Manager coverage beyond VCBF requires a discovery phase.

---

## 1. What you are building

A Python package that constructs an original panel dataset of Vietnamese
open-ended fund flows from public regulatory filings.

Unit of observation: one fund, one dealing period (weekly for most funds).

Primary measures, per observation:

- gross subscriptions in VND
- gross redemptions in VND
- net flow in VND
- total NAV, opening and closing
- NAV per fund certificate, opening and closing
- change in NAV from investment activity
- foreign ownership share

Joined to VN-Index return over the matching period window and to a 12-month
deposit rate series.

The deliverable is a reproducible pipeline plus the panel it produces, not a
one-off extract. Every number in the panel must be traceable to a filing, and
every excluded row must carry a reason.

---

## 2. Why this is tractable

Every licensed Vietnamese open-ended fund files **Appendix XXIV of Circular
98/2020/TT-BTC**, "Bao cao ve thay doi gia tri tai san rong" / "Report on change
of Net Asset Value", on each dealing period. It is a fixed Ministry of Finance
template. The line codes are identical across VCBF, Dragon Capital, SSIAM and
VinaCapital, which is what makes a single parser viable across managers.

Filings are published as bilingual, signed PDFs on each manager's investor
relations pages, and mirrored by Vietstock.

---

## 3. Verified ground truth

Do not re-derive these. They were confirmed against a live filing: VCBF Blue Chip
Fund (VCBBCF), period 17 to 21 November 2022, published at
`vcbf.com/images/2022/vcbbcf_bc_ky_20221121_2.pdf`.

### 3.1 Line codes

| Code  | Field                | Meaning                                        |
|-------|----------------------|------------------------------------------------|
| 1.1   | `nav_begin`          | Total NAV, beginning of period                 |
| 1.3   | `nav_per_unit_begin` | NAV per certificate, beginning of period       |
| 2.1   | `nav_end`            | Total NAV, end of period                       |
| 2.3   | `nav_per_unit_end`   | NAV per certificate, end of period             |
| 3.1   | `chg_investment`     | Change in NAV from investment activities       |
| 3.2   | `chg_flows_net`      | Change from subscription and redemption, net   |
| 3.2.1 | `subscriptions`      | Change from subscription, gross, positive      |
| 3.2.2 | `redemptions`        | Change from redemption, gross, negative        |
| 3.3   | `chg_distribution`   | Change from profit distribution                |
| 4     | `chg_nav_per_unit`   | Change in NAV per certificate vs prior period  |
| 5.1   | `nav_52w_high`       | 52-week high NAV per certificate               |
| 5.2   | `nav_52w_low`        | 52-week low NAV per certificate                |
| 6.1   | `foreign_units`      | Certificates held by foreign investors         |
| 6.2   | `foreign_value`      | Value held by foreign investors                |
| 6.3   | `foreign_ownership_pct` | Foreign ownership ratio, as printed         |

Codes `1`, `2`, `3`, `5`, `6`, `I`, `II` are section headers and never carry
values. They must not be mapped to fields.

The template has two numeric columns per row: "This period" and "Last period".
Capture both. The prior column gives a free consistency check against the
preceding filing.

### 3.2 The reconciliation identity

```
nav_end = nav_begin + chg_investment + subscriptions + redemptions + chg_distribution
```

Verified on the reference filing:

```
302,234,351,773 + 10,171,039,485 + 80,627,726 - 96,819,279 = 312,389,199,705
reported nav_end                                            = 312,389,199,705
residual                                                    = 0
```

It closes to zero dong. This is the core quality control of the entire build.
Treat a non-zero residual as a defect, never as noise.

### 3.3 Formatting conventions

- Thousands separated by commas
- Negatives in accounting parentheses: `(96,819,279)` is `-96819279`
- Percentages printed with a trailing `%`
- Blank cells mean the line does not apply this period, most commonly `3.3` when
  no distribution was paid. A blank is absent, not zero. Do not fabricate zeros
  at parse time. Substitute zero only inside the reconciliation arithmetic.
- Text extraction preserves the line code as a row prefix, so parse on the code,
  not on the bilingual labels. Label text is reordered by PDF extraction and is
  unreliable as an anchor.
- Reference filing has a text layer. No OCR required for VCBF. Other managers
  unconfirmed.

### 3.4 Confirmed sources

| Source | Finding |
|--------|---------|
| VCBF investor relations | Weekly filings listed at `/quan-he-nha-dau-tu/bao-cao-cua-cac-quy-mo/bao-cao-thay-doi-gia-tri-tai-san-rong/`, files under `/images/{year}/`, archive depth confirmed to 2022 |
| Vietstock | Mirrors signed filings at `static2.vietstock.vn`. Use as fallback when a manager's own archive has rotated off |
| Fmarket public API | `POST api.fmarket.vn/res/product/get-nav-history` with `{isAllData: 1, productId, fromDate: null, toDate}` returns NAV per certificate back to inception. Fund IDs via `POST api.fmarket.vn/res/products/filter`. Returns NAV per unit only, no units outstanding, no total NAV |

---

## 4. Non-negotiable design rules

These are the decisions that determine whether the dataset survives review.
Violating any one of them invalidates the output.

**4.1 Never estimate flows from changes in units outstanding.**
Lines 3.2.1 and 3.2.2 are the disclosed cash flows. The proxy
`change in units x NAV per unit` was measured against the reference filing and
came out 3.2% wrong in a single week, in a direction that tracks the period
return, because flows transact at intra-period dealing NAVs. That makes the
measurement error in the dependent variable correlated with the main regressor,
which biases every flow-performance estimate. Compute the proxy only as a
diagnostic for the measurement-error appendix, never as a panel value.

**4.2 Keep gross legs separate all the way through.**
Subscriptions and redemptions stay as distinct columns. Net flow is derived, not
primary. The gross decomposition is the only genuinely novel content in the
dataset. Collapsing to net at ingest destroys it.

**4.3 Scale flows by beginning-of-period NAV.**
Using closing or average NAV puts the flow inside its own denominator and
manufactures part of the flow-performance relationship.

**4.4 Align returns to the filing's own period window.**
Not to a fixed calendar week. Vietnamese dealing periods shift around Tet and
public holidays. A fixed week misaligns flow and return by a day or more.

**4.5 Quarantine, never drop silently.**
Rows failing the identity go to a separate frame with a written reason. A
dataset whose exclusions are undocumented is not auditable.

**4.6 Cache raw PDFs. Never refetch to reparse.**
Parser iterations are frequent. Store the raw bytes with a content hash and
reparse from disk.

---

## 5. Repository layout

```
vngross/
  vngross/
    __init__.py
    appendix_xxiv.py   parse the template into typed observations
    reconcile.py       identity gate, chain continuity, proxy diagnostics
    discover.py        enumerate filing URLs per manager
    fetch.py           download and cache filings
    macro.py           VN-Index and deposit rate ingestion
    panel.py           panel assembly, flow measures, joins, monthly rollup
    sources.yaml       manager and fund registry
  fixtures/
    vcbbcf_20221121.txt
  tests/
    test_appendix_xxiv.py
    test_reconcile.py
    test_panel.py
  data/
    raw/               cached PDFs, gitignored
    interim/
    output/
  METHOD.md
  README.md
  pyproject.toml
```

Dependencies: `pandas`, `pyyaml`, `pdfplumber`, `httpx`, `pytest`. Add
`vnstock` for VN-Index. Avoid heavyweight scraping frameworks; these are static
PDF links behind listing pages.

---

## 6. Module contracts

### 6.1 `appendix_xxiv.py`

```python
FIELD_MAP: dict[str, str]           # line code to field name, per section 3.1
SECTION_CODES: set[str]             # headers to skip

class ParseError(ValueError): ...

@dataclass
class Filing:
    fund_code: str | None
    period_start: date | None
    period_end: date | None
    report_date: date | None
    values: dict[str, float]        # this-period column
    prior_values: dict[str, float]  # last-period column
    source: str | None

    @property
    def net_flow(self) -> float          # prefers 3.2, falls back to 3.2.1 + 3.2.2
    @property
    def units_begin(self) -> float | None  # nav_begin / nav_per_unit_begin
    @property
    def units_end(self) -> float | None
    @property
    def gross_return(self) -> float | None # not distribution adjusted
    def as_row(self) -> dict

def parse_number(tok: str) -> float
def parse_text(text: str, fund_code=None, source=None) -> Filing
def parse_pdf(path: str, fund_code=None) -> Filing
def parse_many(paths: Iterable[tuple[str, str]]) -> tuple[list[Filing], list[tuple[str, str]]]
```

`parse_text` must be separable from PDF handling so the mapping layer is
testable against fixtures without network or pdfplumber.

`parse_many` collects failures rather than raising. A multi-year, multi-manager
run will always hit malformed or scanned filings, and the run should surface
them for review instead of aborting.

Raise `ParseError` when line 2.1 is absent. That means the layout is not
Appendix XXIV.

Dates parse from the English half of the bilingual header:
`From 17 Nov 2022 to 21 Nov 2022` and `Reporting Date: 22 Nov 2022`.

### 6.2 `reconcile.py`

```python
ABS_TOL_VND = 5.0
REL_TOL = 1e-9

@dataclass
class Check:
    fund_code, period_end, passed: bool, residual_vnd: float, detail: str

def reconcile(filing: Filing) -> Check
def check_net_flow_consistency(filing: Filing) -> Check   # 3.2.1 + 3.2.2 vs 3.2
def proxy_divergence(filing: Filing) -> dict              # diagnostic only
def chain_continuity(filings: list[Filing]) -> list[Check]
def summarise(checks: list[Check]) -> str
```

`proxy_divergence` returns `proxy_end`, `proxy_mid`, and their errors in VND and
percent, alongside `gross_return`. This feeds the measurement-error appendix and
justifies rule 4.1 with evidence rather than assertion.

`chain_continuity` verifies each fund's opening NAV equals the prior filing's
closing NAV. Breaks indicate a missed filing, which matters because an unnoticed
gap turns a missing week into a fabricated flow.

### 6.3 `discover.py`

```python
def discover_filings(manager_id: str, cfg: dict) -> list[FilingRef]
```

Crawl the manager's disclosure listing page, follow pagination, and enumerate
PDF links with fund code and publication date. Do not construct URLs by pattern.
The VCBF filename carries an unpredictable sequence suffix
(`vcbbcf_bc_ky_20221121_2.pdf`), so enumeration is required.

Fall back to the Vietstock mirror when a manager's archive does not reach the
target start date.

### 6.4 `fetch.py`

```python
def fetch(url: str, cache_dir: Path) -> Path
```

Content-addressed cache. Skip if present. Rate limit to no more than one request
per second per host. Set a descriptive user agent identifying the requester.
Retry with backoff on 5xx, never on 4xx.

### 6.5 `macro.py`

```python
def vnindex_daily(start: date, end: date) -> pd.DataFrame   # columns: time, close
def load_deposit_rate(path: Path) -> pd.DataFrame           # month, rate_pct, provenance
def fmarket_nav_history(fund_code: str) -> pd.DataFrame     # cross-check only
```

VN-Index via `vnstock`. The deposit rate is loaded from a curated CSV, not
scraped. See section 9.

### 6.6 `panel.py`

```python
@dataclass
class PanelResult:
    panel: pd.DataFrame
    quarantine: pd.DataFrame
    continuity_breaks: pd.DataFrame
    diagnostics: pd.DataFrame

def build_fund_period_panel(filings, fund_meta=None) -> PanelResult
def attach_market_return(panel, vnindex_daily) -> pd.DataFrame
def attach_deposit_rate(panel, deposit_monthly) -> pd.DataFrame
def to_monthly(panel) -> pd.DataFrame
```

Derived columns on the fund-period panel:

| Column | Definition |
|--------|------------|
| `gross_subscription_rate` | `subscriptions / nav_begin` |
| `gross_redemption_rate` | `abs(redemptions) / nav_begin` |
| `net_flow_rate` | `net_flow / nav_begin` |
| `churn_rate` | `(subscriptions + abs(redemptions)) / nav_begin` |
| `flow_asymmetry` | `(subscriptions - abs(redemptions)) / (subscriptions + abs(redemptions))`, bounded in [-1, 1] |
| `period_days` | `period_end - period_start` |

`attach_market_return` uses the last available close at or before each period
boundary, so non-trading boundary dates do not create gaps. Emits
`market_return` and `excess_return`.

`attach_deposit_rate` carries the `provenance` column into the panel
deliberately. The series is hand-assembled and the panel should make that
visible.

`to_monthly` sums flows, compounds returns, and takes NAV from the month's first
opening and last closing so the identity survives aggregation.

---

## 7. Golden fixture

Create `fixtures/vcbbcf_20221121.txt` with exactly this content. It is the text
layer of the reference filing and is the anchor for the whole test suite. A
regression against it means the parser has drifted from the actual Ministry of
Finance template rather than from an invented one.

```
PUBLIC#
1
Tên Công ty quản lý quỹ: 
Fund Management Company:
2
Tên Ngân hàng giám sát:
Supervisory bank: 
3
Tên Quỹ:
Fund name: 
4 Kỳ báo cáo: Từ ngày 17 tháng 11 năm 2022 đến ngày 21 tháng 11 năm 2022 
Reporting period: From 17 Nov 2022 to 21 Nov 2022
5 Ngày lập báo cáo: Ngày 22 tháng 11 năm 2022
Reporting Date: 22 Nov 2022
 Đơn vị tính/Currency: VND 
STT
No.
Mã số
Code
Kỳ báo cáo
This period
Kỳ trước
Last period
I
1
1.1 302,234,351,773 303,392,880,988 
1.2
1.3 21,439.31 21,517.75 
2
2.1 312,389,199,705 302,234,351,773 
2.2
2.3 22,160.84 21,439.31 
3
3.1 10,171,039,485 (1,105,852,259)
3.2 (16,191,553) (52,676,956)
3.2.1 80,627,726 146,183,850 
3.2.2 (96,819,279) (198,860,806)
3.3
4 721.53 (78.44)
5
5.1 31,723.28 31,723.28 
5.2 21,439.31 21,439.31 
6
6.1 2,885,796.11 2,885,796.11 
6.2 63,951,665,866 61,869,477,399 
6.3 20.54% 20.47%
II
Quỹ Đầu tư Cổ phiếu hàng đầu VCBF
VCBF Blue Chip Fund (VCBBCF)
Phụ lục XXIV: Mẫu báo cáo về thay đổi giá trị tài sản ròng
Appendix XXIV: Report on change of Net Asset Value
Công ty Liên doanh Quản lý Quỹ Đầu tư Chứng khoán Vietcombank
Vietcombank Fund Management
Ngân Hàng TNHH Một Thành Viên Standard Chartered (Việt Nam)
Standard Chartered Bank (Vietnam) Limited
```

---

## 8. Acceptance criteria

The build is not complete until all of the following pass.

**Number parsing**

- `302,234,351,773` to `302234351773.0`
- `(96,819,279)` to `-96819279.0`
- `21,439.31` to `21439.31`
- `(78.44)` to `-78.44`
- `20.54%` to `20.54`
- `n/a` raises `ParseError`

**Field mapping against the fixture**

- `period_start` 2022-11-17, `period_end` 2022-11-21, `report_date` 2022-11-22
- `nav_begin` 302,234,351,773 and `nav_end` 312,389,199,705
- `nav_per_unit_begin` 21,439.31 and `nav_per_unit_end` 22,160.84
- `chg_investment` 10,171,039,485
- `subscriptions` 80,627,726, strictly positive
- `redemptions` -96,819,279, strictly negative
- `chg_flows_net` -16,191,553
- `prior_values["nav_end"]` 302,234,351,773 and `prior_values["subscriptions"]` 146,183,850
- section codes `1` and `3` absent from `values`
- `chg_distribution` absent from `values`, not present as 0.0
- text lacking line 2.1 raises `ParseError`

**Reconciliation**

- identity residual on the fixture is 0 within 1 VND
- `check_net_flow_consistency` passes on the fixture
- mutating `subscriptions` to 8,062,772,600, a single digit slip, fails the gate
- `units_end` approximately 14,096,451.2 and `units_begin` approximately 14,097,205.17

**Measurement error, the argument that justifies rule 4.1**

- `gross_return` on the fixture is positive
- `proxy_end` is more negative than `reported_net_flow`, that is the proxy
  overstates the outflow in a rising week
- absolute `proxy_end_error_pct` exceeds 3.0
- absolute `proxy_mid_error_vnd` is smaller than absolute `proxy_end_error_vnd`,
  confirming midpoint NAV reduces but does not remove the bias

**Chain continuity**

- two contiguous filings where the second's opening equals the first's closing:
  all checks pass
- a second filing with a mismatched opening: one failing check whose detail
  mentions a missed filing

**Panel assembly**

- a batch of three filings, one deliberately corrupted, yields two panel rows and
  one quarantine row carrying a reason
- `flow_asymmetry` is bounded in [-1, 1]
- `to_monthly` sums flows and preserves first opening and last closing NAV

---

## 9. Discovery phase, run before scraping

Only VCBF is verified. Resolve the following before any full run.

1. **Confirm which firm "VCFM" means.** VCFM is VinaCapital Fund Management,
   which is the renamed VinaWealth Fund Management. One firm across time, not two
   peers. Treating them as separate managers double-counts. Separately, "VCFM" is
   sometimes used informally for Viet Capital, Ban Viet, Fund Management. Confirm
   the intended entity before fixing the sample.

2. **Enumerate listing pages** for DCVFM, SSIAM and VinaCapital. Record the
   listing URL, archive depth, and whether PDFs carry a text layer. Promote an
   entry in `sources.yaml` from `unverified` to `verified` only once a filing has
   been fetched and parsed, and add its text layer as a fixture.

3. **Check for the older template.** Filings before 2021 may use Circular
   183/2011, which would need a second parser or a template-version detector.
   Determine the earliest date the Appendix XXIV codes appear per manager.

4. **Resolve the deposit rate.** No clean high-frequency 12-month deposit series
   exists. World Bank and IMF publish an annual, 3-month tenor figure, wrong on
   both counts. Options in order of defensibility:
   - hand-assemble a monthly big-four board rate series from Vietcombank, BIDV,
     Agribank and VietinBank published rate tables plus Wayback Machine
     snapshots, with per-observation provenance
   - SBV monthly interest rate statistics where published
   - substitute a genuinely high-frequency market rate such as the interbank rate
     or 1-year government bond yield, stating the substitution plainly

   Whichever is chosen, ship as a curated CSV with a provenance column. Never
   present it as scraped.

5. **Recover closed and merged funds** from the Vietstock mirror. Closure
   correlates with poor performance and outflows, so an as-is sample biases any
   flow-performance estimate. The DCVFM funds are the main exposure given the VFM
   to Dragon Capital transition.

---

## 10. Build order

Ship VCBF end to end before touching the other managers. Five funds, roughly four
years, weekly, is on the order of 1,000 filings. That alone is a publishable
panel and it derisks everything downstream.

1. `appendix_xxiv.py` and `reconcile.py` against the fixture, tests green
2. `fetch.py` with caching
3. `discover.py` for VCBF only
4. Full VCBF run. Inspect quarantine and continuity breaks before proceeding
5. Cross-check every parsed `nav_per_unit_end` against Fmarket. Two independent
   sources agreeing is strong evidence the parse is clean
6. `panel.py` and macro joins
7. Extend discovery to remaining managers, one at a time, each with its own fixture

---

## 11. Limitations to carry into METHOD.md

Document these rather than resolving them. A reviewer will find them regardless,
and stating them first is the stronger position.

- `gross_return` is not distribution adjusted. Funds paying distributions, line
  3.3, need a total-return series built before any performance-chasing test.
  Most Vietnamese equity open-ended funds accumulate rather than distribute, but
  verify per fund rather than assuming
- unit splits, consolidations and distributions paid in units break any units
  series. Flows from 3.2.1 and 3.2.2 are immune, which is a further argument for
  rule 4.1, but NAV-per-unit return series need adjustment
- survivorship, per section 9 item 5
- the deposit rate series is curated, not scraped
- filing frequency varies. Most funds file weekly, some daily. `period_days` on
  each row makes this explicit and must be checked before pooling

---

## 12. Scraping conduct

These are small managers' web servers hosting mandatory regulatory disclosures.
Rate limit to one request per second per host. Cache aggressively so a reparse
never refetches. Set an identifying user agent. Check each site's terms. The
filings being public disclosures is an argument about the data, not a licence to
hammer the host.

---

## 13. Version history

| Version | Date | Change |
|---------|------|--------|
| 1.0 | 2026-08-14 | Initial spec. Appendix XXIV template, line codes, and reconciliation identity verified against VCBBCF filing of 2022-11-21. Units-outstanding proxy rejected on measurement-error grounds with quantified evidence. |
