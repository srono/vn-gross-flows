"""Assemble the curated 12-month deposit rate series.

Run this to regenerate `data/deposit_rate_12m.csv`. The CSV, not this script, is
the artifact the pipeline consumes: `macro.load_deposit_rate` reads the file and
never reaches the network. The series is curated, and the panel presents it as
curated.

Why the source is what it is
----------------------------
The spec's preferred option is a monthly big-four board rate series assembled
from published rate tables plus Wayback Machine snapshots. Of the big four, only
Agribank turns out to be recoverable:

  Agribank     www.agribank.com.vn/vn/lai-suat renders its rate table
               server-side, so archived HTML carries the actual figures.
               26 distinct months captured between 2022-09 and 2026-03.
  Vietcombank  the rate table renders client-side; archived HTML contains no
               figures. The older portal.vietcombank.com.vn snapshots are
               exchange rates (ty-gia.aspx), not deposit rates.
  BIDV         archived HTML contains a table element but no rate figures.
  VietinBank   no qualifying snapshots of the rate page at all.

Aggregators were checked. webgia.com was rejected: it has 37 months of coverage
but substitutes the literal string "webgia.com" for every rate digit as an
anti-scraping measure, so its tables carry no data.

CafeF was partly usable. Its rate page renders client-side and its archived HTML
contains no bank names at all, but the page reads a static JSON on the CafeF CDN
that carries all 29 banks and a clean 12-month tenor. The Wayback Machine holds
exactly one capture of that JSON, 2026-03-31, and the file is also readable live.
Those two observations are genuine big-four averages rather than a single bank,
and they land precisely where Agribank stops being recoverable, so they anchor
the tail of the series instead of leaving it entirely unobserved. They do not
overlap the Agribank window, so the two sources cannot be reconciled directly;
what can be checked is that CafeF's Agribank tenor grid has the same shape and
level structure as Agribank's own published table, which it does.

Agribank is a state-owned commercial bank and one of the big four, so its board
rate is a defensible reference rather than an outlier. But this is one bank, not
a four-bank average, and that limitation is recorded in METHOD.md and visible in
the `bank` column of every row.

Gap handling
------------
Board rates are administered prices that change only when they change, not
continuously, so carrying the last observed rate forward across a month with no
snapshot is a far weaker assumption than it would be for a market rate. Every
carried-forward month says so in its `provenance`, naming the month it came
from, so no consumer of the panel can mistake it for an observation.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import httpx
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = ROOT / "data" / "deposit_rate_12m.csv"
CACHE_DIR = ROOT / "data" / "raw" / "deposit_rate"

USER_AGENT = (
    "vngross/0.1 (academic research on Vietnamese fund flows; "
    "10thirtyLabs; mai@10thirtylabs.com)"
)
CDX_URL = "http://web.archive.org/cdx/search/cdx"
BANK = "Agribank"
TENOR = "12 months"

SOURCE_AGRIBANK = "agribank-board-rate"
SOURCE_CAFEF = "cafef-big-four"

# The static file the CafeF rate page reads. All banks, all tenors, no markup.
CAFEF_JSON_URL = (
    "https://cafefnew.mediacdn.vn/Images/Uploaded/DuLieuDownload/Liveboard/"
    "all_banks_interest_rates.json"
)
BIG_FOUR = ("Vietcombank", "BIDV", "Agribank", "VietinBank")
CAFEF_TWELVE_MONTH_KEY = "12T"
# Both host spellings are distinct CDX keys and hold different snapshots. During
# 2025 Agribank moved deposit rates off /vn/lai-suat, which became a scripted
# landing page carrying no table, onto /vn/lai-suat-tien-gui; both paths are
# needed to span the panel window.
SOURCE_URLS = [
    "www.agribank.com.vn/vn/lai-suat",
    "agribank.com.vn/vn/lai-suat",
    "www.agribank.com.vn/vn/lai-suat-tien-gui",
    "agribank.com.vn/vn/lai-suat-tien-gui",
]

PANEL_START = "2022-07"
PANEL_END = "2026-08"

# The widest gap that may be bridged between two agreeing observations. A gap
# this is applied to is bounded on both sides by the same measured level, so the
# risk is only that the rate moved and came back within the window.
MAX_BRIDGE_MONTHS = 6

_ROW_RE = re.compile(r"<tr.*?</tr>", re.S)
_CELL_RE = re.compile(r"<t[dh].*?</t[dh]>", re.S)
_TABLE_RE = re.compile(r"<table.*?</table>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
# "12 Tháng" / "12 thang". Must not match "12 tháng plus" style variants.
_TWELVE_MONTH_RE = re.compile(r"^12\s*th[áa]ng$", re.IGNORECASE)
_PERCENT_RE = re.compile(r"^([0-9]+(?:[.,][0-9]+)?)\s*%$")
# The retail schedule is headed "Ca nhan"; the corporate one "Doanh nghiep".
# Retail is the relevant outside option for a fund investor.
_RETAIL_MARKER_RE = re.compile(r"Cá\s*nhân", re.IGNORECASE)


def _clean(fragment: str) -> str:
    return " ".join(_TAG_RE.sub(" ", fragment).split())


def cdx_snapshots(client: httpx.Client, url: str) -> list[tuple[str, str]]:
    """Every 200-status snapshot of `url` inside the panel window."""
    params = {
        "url": url,
        "from": "20220601",
        "to": "20260930",
        "output": "json",
        "fl": "timestamp,original",
        "filter": "statuscode:200",
        "limit": "2000",
    }
    # The CDX endpoint sheds load with both 503s and outright connection resets,
    # so transport errors are retried on the same backoff as bad statuses.
    for attempt in range(6):
        try:
            response = client.get(CDX_URL, params=params, timeout=150)
        except httpx.HTTPError as exc:
            print(f"  CDX transport error ({type(exc).__name__}), retrying")
            time.sleep(5 * (attempt + 1))
            continue
        if response.status_code == 200:
            try:
                return [(row[0], row[1]) for row in response.json()[1:]]
            except (ValueError, IndexError):
                return []
        time.sleep(5 * (attempt + 1))
    print(f"  CDX unavailable for {url}", file=sys.stderr)
    return []


def fetch_snapshot(client: httpx.Client, timestamp: str, original: str) -> str:
    """Fetch a snapshot's original bytes, caching so a reparse never refetches."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    slug = "tien-gui" if "lai-suat-tien-gui" in original else "lai-suat"
    cached = CACHE_DIR / f"agribank-{slug}-{timestamp}.html"
    if cached.exists():
        return cached.read_text(encoding="utf-8", errors="replace")

    # `id_` asks the Wayback Machine for the archived bytes without its banner.
    url = f"https://web.archive.org/web/{timestamp}id_/{original}"
    response = client.get(url, timeout=120)
    response.raise_for_status()
    text = response.text
    cached.write_text(text, encoding="utf-8", errors="replace")
    time.sleep(1.0)
    return text


def extract_twelve_month_vnd(html: str) -> float | None:
    """Pull the 12-month VND rate out of the retail schedule.

    Picks the table preceded by the retail heading rather than the first table on
    the page, because the page also carries a corporate schedule with different
    rates and their order is not guaranteed.
    """
    candidates: list[tuple[bool, float]] = []
    for match in _TABLE_RE.finditer(html):
        preceding = _clean(html[max(0, match.start() - 1600) : match.start()])
        is_retail = bool(_RETAIL_MARKER_RE.search(preceding[-400:]))

        for row in _ROW_RE.findall(match.group(0)):
            cells = [_clean(c) for c in _CELL_RE.findall(row)]
            if len(cells) < 2 or not _TWELVE_MONTH_RE.match(cells[0]):
                continue
            percent = _PERCENT_RE.match(cells[1])
            if percent:
                candidates.append((is_retail, float(percent.group(1).replace(",", "."))))
            break

    if not candidates:
        return None
    for is_retail, rate in candidates:
        if is_retail:
            return rate
    return candidates[0][1]


# VNDIRECT publishes an average 12-month term deposit rate across Vietnamese
# commercial banks in its money-market chartbooks and economic updates. Each
# entry below was read out of the cited PDF and the quoted sentence checked
# against it on 2026-08-15; the quote is carried into the provenance so the
# claim can be re-checked without refetching.
#
# This is a THIRD construct: a market-wide average across commercial banks, not
# a single bank's board rate and not a big-four mean. It is spliced in only for
# months no board-rate observation exists. Where the constructs come close
# enough to compare they agree to within about 0.1pp, which is what makes the
# splice defensible rather than merely convenient.
#
# Deliberately excluded, having failed verification:
#   2023-11  the source forecasts rates will "remain at 5.4%/year for the
#            remainder of 2023". A forecast is not an observation.
#   2024-03  cited only via a click-tracking redirect, and the April update's
#            own "+0.05% pts MoM" implies March near 4.56, not the 4.63 claimed.
#   2024-02  derived from that unverified March figure.
SOURCE_VNDIRECT = "vndirect-market-average"
VNDIRECT_OBSERVATIONS: list[dict] = [
    {
        "month": "2023-07", "rate_pct": 6.40,
        "url": "https://www.vndirect.com.vn/cmsupload/beta/Vietnam-Money-market-chartbook_August_20230809.pdf",
        "quote": "the average 12-month deposit interest rate of commercial banks fell to 6.40% p.a at the end of July 2023",
    },
    {
        # Two independent statements in different reports both put end-August at
        # 5.9: "5.4% on October 10, down 0.5 points from the end of August" and
        # "5.6% on 25 Sep 2023, down 0.3 points compared to the end of Aug".
        "month": "2023-08", "rate_pct": 5.90,
        "url": "https://www.vndirect.com.vn/cmsupload/beta/Econ_Update_20231017.pdf",
        "quote": "decreased to 5.4%/year on October 10, down 0.5% points compared to the end of August (corroborated by the 25 Sep report's 0.3pp decline from end-August)",
    },
    {
        "month": "2023-09", "rate_pct": 5.60,
        "url": "https://www.vndirect.com.vn/Market-Strategy_October_20231005.pdf",
        "quote": "decreased to 5.6%/year on 25 Sep 2023, down 0.3% points compared to the end of Aug 2023",
    },
    {
        "month": "2023-10", "rate_pct": 5.30,
        "url": "https://www.vndirect.com.vn/Market-Strategy_November_20231106.pdf",
        "quote": "As of October 26, 2023, the average 12-month deposit interest rate of commercial banks has decreased to 5.3%/year",
    },
    {
        "month": "2024-01", "rate_pct": 5.14,
        "url": "https://www.vndirect.com.vn/cmsupload/beta/Market-Strategy_Feb_20240206.pdf",
        "quote": "As of January 31, 2024, the average 12-month term deposit interest rate of commercial banks has decreased to 5.14%/year",
    },
    {
        "month": "2024-04", "rate_pct": 4.61,
        "url": "https://www.vndirect.com.vn/cmsupload/beta/Econ_Update_20240520.pdf",
        "quote": "As of April 27, 2024, the average 12-month term deposit interest rate of commercial banks plateaued at around 4.61%",
    },
]


# Shinhan Securities Vietnam prints a monthly indicator table whose row
# "Avg. Deposit rate 12M term of SCBs" is the average 12-month rate at
# state-owned commercial banks - the big four, the same construct this series
# wants - at month end, on one consistent methodology across thirteen months.
#
# It corroborates the Agribank extraction exactly where they overlap and rates
# were flat: 7.40, 7.20 and 7.20 for 2023-02, 2023-03 and 2023-04. Where they
# diverge, 2023-06 and 2023-12, the Agribank snapshots are from the 6th and the
# 4th of a month in which rates were falling fast, against Shinhan's month end,
# so the difference is timing rather than disagreement. VNDIRECT's all-commercial
# banks average sits 0.05 to 0.21pp above Shinhan's state-owned figure
# throughout, which is the expected sign: state-owned banks pay less.
SOURCE_SHINHAN = "shinhan-scb-average"
_SHINHAN_URL = "https://shinhansec.com.vn/uploads/report/Vietnam_economic_update_2402_E.pdf"
_SHINHAN_TABLE = {
    "2023-02": 7.40, "2023-03": 7.20, "2023-04": 7.20, "2023-05": 6.80,
    "2023-06": 6.30, "2023-07": 6.30, "2023-08": 5.80, "2023-09": 5.50,
    "2023-10": 5.25, "2023-11": 5.18, "2023-12": 4.95, "2024-01": 4.93,
    "2024-02": 4.78,
}

# Press reports of a specific big-four or state-owned 12-month rate, each read
# from the article and quoted. Weaker than a board-rate table or a broker's
# indicator series, so these fill only months nothing better reaches.
SOURCE_PRESS = "press-state-owned"


def collect_shinhan() -> dict[str, dict]:
    records = {
        month: {
            "month": month, "rate_pct": rate,
            "bank": "state-owned commercial banks (average)", "tenor": TENOR,
            "source": SOURCE_SHINHAN, "n_banks": 4, "observed": True,
            "snapshot": None,
            "provenance": (
                f"Shinhan Securities Vietnam, Vietnam Economic Update "
                f"(25 Mar 2024), indicator table row \"Avg. Deposit rate 12M "
                f"term of SCBs\", column {month[5:]}/{month[2:4]} = {rate}% "
                f"({_SHINHAN_URL})"
            ),
        }
        for month, rate in _SHINHAN_TABLE.items()
    }
    records["2024-03"] = {
        "month": "2024-03", "rate_pct": 4.70,
        "bank": "state-owned commercial banks (average)", "tenor": TENOR,
        "source": SOURCE_SHINHAN, "n_banks": 4, "observed": True,
        "snapshot": None,
        "provenance": (
            "Shinhan Securities Vietnam, Vietnam Economic Update (25 Mar 2024): "
            "\"Deposit interest rates 12M term at State-owned commercial banks "
            f"(SCBs) continue to decrease, reaching 4.7% on March 22, 2024\" "
            f"({_SHINHAN_URL})"
        ),
    }
    return records


def collect_press() -> dict[str, dict]:
    entries = [
        {
            "month": "2022-07", "rate_pct": 5.5,
            "bank": "Vietcombank (over-the-counter)",
            "url": "https://en.baoquocte.vn/the-trend-of-race-to-increase-interest-rates-continues-to-be-hot-192920.html",
            "quote": (
                "article dated 02/08/2022 reporting the increase: \"for a "
                "12-month term, the interest rate increases from 5.5%/year to "
                "5.6%/year\". 5.5% is the pre-increase level, so it is the July "
                "level; the month is inferred from the increase date, the level "
                "is stated"
            ),
        },
        {
            "month": "2022-08", "rate_pct": 5.6,
            "bank": "Vietcombank (over-the-counter)",
            "url": "https://en.baoquocte.vn/the-trend-of-race-to-increase-interest-rates-continues-to-be-hot-192920.html",
            "quote": (
                "article dated 02/08/2022: \"for a 12-month term, the interest "
                "rate increases from 5.5%/year to 5.6%/year\". Sits just above "
                "the SBV's 5.5-6.6% band for 12-to-24-month terms that month"
            ),
        },
        {
            "month": "2025-10", "rate_pct": 4.7,
            "bank": "state-owned banks (average)",
            "url": "https://vcdn.vietnam.vn/en/lai-suat-ngan-hang-dong-loat-tang-gui-100-trieu-dong-nhan-duoc-bao-nhieu-tien-lai",
            "quote": (
                "\"Bank interest rates started to rise from the end of October. "
                "The average for a 12-month term was 5.34% at private banks and "
                "4.7% at state-owned banks\""
            ),
        },
    ]
    return {
        e["month"]: {
            "month": e["month"], "rate_pct": e["rate_pct"], "bank": e["bank"],
            "tenor": TENOR, "source": SOURCE_PRESS, "n_banks": 0,
            "observed": True, "snapshot": None,
            "provenance": f"Press report, {e['quote']} ({e['url']})",
        }
        for e in entries
    }


def collect_vndirect() -> dict[str, dict]:
    """The verified VNDIRECT market-average observations, as records."""
    return {
        entry["month"]: {
            "month": entry["month"],
            "rate_pct": entry["rate_pct"],
            "bank": "commercial banks (average)",
            "tenor": TENOR,
            "source": SOURCE_VNDIRECT,
            "n_banks": 0,
            "observed": True,
            "snapshot": None,
            "provenance": (
                f"VNDIRECT Research, average 12-month term deposit rate across "
                f"commercial banks: \"{entry['quote']}\" ({entry['url']})"
            ),
        }
        for entry in VNDIRECT_OBSERVATIONS
    }


def _big_four_twelve_month(payload: dict) -> dict[str, float]:
    """Pull each big-four bank's 12-month VND rate out of a CafeF payload."""
    rates: dict[str, float] = {}
    for bank in payload.get("Data") or []:
        name = bank.get("name")
        if name not in BIG_FOUR:
            continue
        for entry in bank.get("interestRates") or []:
            if entry.get("time") == CAFEF_TWELVE_MONTH_KEY:
                value = entry.get("value")
                if isinstance(value, (int, float)) and value > 0:
                    rates[name] = float(value)
                break
    return rates


def collect_cafef(client: httpx.Client, include_live: bool = True) -> dict[str, dict]:
    """Big-four 12-month averages from CafeF, archived and live.

    Returns at most a couple of months. The value is not coverage, it is that
    these are four-bank averages at the end of the panel window, where the
    single-bank Agribank series has already run out.
    """
    found: dict[str, dict] = {}
    sources: list[tuple[str, str]] = []

    for timestamp, original in cdx_snapshots(client, CAFEF_JSON_URL.split("//", 1)[-1]):
        sources.append((timestamp, f"https://web.archive.org/web/{timestamp}id_/{original}"))
    if include_live:
        sources.append((time.strftime("%Y%m%d%H%M%S"), CAFEF_JSON_URL))

    for timestamp, url in sources:
        try:
            response = client.get(url, timeout=90)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 - one dead capture is not fatal
            print(f"  cafef {timestamp}: unavailable ({type(exc).__name__})")
            continue
        time.sleep(1.0)

        rates = _big_four_twelve_month(payload)
        if len(rates) < len(BIG_FOUR):
            print(f"  cafef {timestamp}: only {len(rates)} of 4 big-four banks; skipped")
            continue

        month = f"{timestamp[:4]}-{timestamp[4:6]}"
        mean = sum(rates.values()) / len(rates)
        detail = ", ".join(f"{k} {v}" for k, v in sorted(rates.items()))
        archived = url.startswith("https://web.archive.org/")
        found[month] = {
            "month": month,
            "rate_pct": round(mean, 4),
            "bank": "big four (mean)",
            "tenor": TENOR,
            "source": SOURCE_CAFEF,
            "n_banks": len(rates),
            "observed": True,
            "snapshot": timestamp,
            "provenance": (
                f"mean of big-four published 12-month VND board rates ({detail}), "
                + (
                    f"read from Wayback Machine snapshot {timestamp} of "
                    f"{CAFEF_JSON_URL}"
                    if archived
                    else f"read live from {CAFEF_JSON_URL} on "
                    f"{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}"
                )
            ),
        }
        print(f"  cafef {month}: {mean:.2f}%  ({detail})")

    return found


def build() -> pd.DataFrame:
    observed: dict[str, dict] = {}

    with httpx.Client(
        headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as client:
        snapshots: list[tuple[str, str]] = []
        for url in SOURCE_URLS:
            found = cdx_snapshots(client, url)
            print(f"  {url}: {len(found)} snapshots")
            snapshots.extend(found)

        # One observation per month, from the latest snapshot in that month, so
        # the rate is the board rate as it stood at month end.
        by_month: dict[str, list[tuple[str, str]]] = {}
        for timestamp, original in sorted(snapshots):
            by_month.setdefault(f"{timestamp[:4]}-{timestamp[4:6]}", []).append(
                (timestamp, original)
            )

        for month, entries in sorted(by_month.items()):
            # Latest first: the board rate as it stood at month end. Fall back to
            # earlier snapshots in the month, and across both URL paths, because
            # a snapshot can land on the scripted landing page that carries no
            # table while another the same month carries the real one.
            rate = None
            timestamp = original = None
            for candidate_ts, candidate_url in sorted(entries, reverse=True):
                try:
                    html = fetch_snapshot(client, candidate_ts, candidate_url)
                except Exception as exc:  # noqa: BLE001 - a dead snapshot is not fatal
                    print(f"  {month}: fetch failed ({type(exc).__name__})")
                    continue
                rate = extract_twelve_month_vnd(html)
                if rate is not None:
                    timestamp, original = candidate_ts, candidate_url
                    break
            if rate is None:
                print(f"  {month}: no 12-month VND rate in any of {len(entries)} snapshots")
                continue
            observed[month] = {
                "month": month,
                "rate_pct": rate,
                "bank": BANK,
                "tenor": TENOR,
                "source": SOURCE_AGRIBANK,
                "n_banks": 1,
                "observed": True,
                "snapshot": timestamp,
                "provenance": (
                    f"{BANK} published board rate, {TENOR} VND retail term deposit, "
                    f"read from Wayback Machine snapshot {timestamp} of "
                    f"https://{original.split('//', 1)[-1]}"
                ),
            }
            print(f"  {month}: {rate}%  (snapshot {timestamp})")

        # A four-bank average beats a single bank for any month both cover.
        # In practice they do not overlap: Agribank stops being recoverable in
        # 2025-07 and CafeF's only captures are 2026.
        for month, record in collect_cafef(client).items():
            observed[month] = record

    # Board-rate observations win where they exist; the market average fills
    # only months neither Agribank nor CafeF covers.
    for month, record in collect_vndirect().items():
        observed.setdefault(month, record)
    for month, record in collect_shinhan().items():
        observed.setdefault(month, record)
    for month, record in collect_press().items():
        observed.setdefault(month, record)

    if not observed:
        raise SystemExit("no observations recovered; refusing to write a series")

    months = pd.period_range(PANEL_START, PANEL_END, freq="M")
    ordered = sorted(observed)

    def nearest(month: str, backwards: bool) -> dict | None:
        candidates = [m for m in ordered if (m < month) == backwards and m != month]
        if not candidates:
            return None
        return observed[candidates[-1] if backwards else candidates[0]]

    rows: list[dict] = []
    for period in months:
        month = str(period)
        if month in observed:
            rows.append(observed[month])
            continue

        # Fill only a month bracketed by observations on BOTH sides that agree
        # on the same level. That is interpolation between two measured points
        # at one value; a one-sided carry is an extrapolation into a period the
        # series has no information about.
        #
        # This replaces a plain carry-forward, which the data itself refuted:
        # of 18 carried months under that rule, 10 sat inside a stretch where
        # the next observation proved the rate had moved. Holding 5.3% across
        # 2024-01 to 2024-03 overstated a falling market by up to 0.55pp, and
        # holding 4.7% into late 2025 missed the turn to 5.9%.
        before = nearest(month, backwards=True)
        after = nearest(month, backwards=False)
        reason = None
        if before is None:
            reason = (
                "no observation: earliest recoverable snapshot postdates this "
                "month; not back-cast"
            )
        elif after is None:
            reason = (
                f"no observation: last is {before['month']}, and a one-sided "
                f"carry past the end of the series cannot be validated"
            )
        elif before["rate_pct"] != after["rate_pct"]:
            reason = (
                f"no observation: bracketing observations {before['month']} at "
                f"{before['rate_pct']}% and {after['month']} at "
                f"{after['rate_pct']}% disagree, so the rate moved somewhere in "
                f"this gap; left missing rather than picking a side"
            )
        elif (period - pd.Period(before["month"], freq="M")).n + (
            pd.Period(after["month"], freq="M") - period
        ).n > MAX_BRIDGE_MONTHS:
            reason = (
                f"no observation: {before['month']} and {after['month']} agree "
                f"at {before['rate_pct']}% but are more than "
                f"{MAX_BRIDGE_MONTHS} months apart"
            )

        if reason is not None:
            rows.append({
                "month": month, "rate_pct": None,
                "bank": before["bank"] if before else BANK, "tenor": TENOR,
                "source": None, "n_banks": 0, "observed": False,
                "snapshot": None, "provenance": reason,
            })
            continue

        rows.append({
            "month": month, "rate_pct": before["rate_pct"],
            "bank": before["bank"], "tenor": TENOR,
            "source": before["source"], "n_banks": before["n_banks"],
            "observed": False, "snapshot": before["snapshot"],
            "provenance": (
                f"bridged between observations at {before['month']} and "
                f"{after['month']}, which agree at {before['rate_pct']}%"
            ),
        })

    frame = pd.DataFrame(rows)[
        [
            "month", "rate_pct", "tenor", "bank", "source", "n_banks",
            "observed", "snapshot", "provenance",
        ]
    ]
    return frame


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=OUT_PATH)
    args = parser.parse_args()

    print("assembling 12-month deposit rate series from Agribank board rates")
    frame = build()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(args.out, index=False)

    observed = int(frame["observed"].sum())
    missing = int(frame["rate_pct"].isna().sum())
    print(
        f"\nwrote {args.out} : {len(frame)} months, {observed} observed, "
        f"{len(frame) - observed - missing} carried forward, {missing} missing"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
