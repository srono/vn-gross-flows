"""Fetch the outside-option macro series that FRED does not carry.

Vietnamese household savings compete across bank deposits, gold, property and
direct equity. Only the deposit rate was tested, so a deposit-rate coefficient
is carrying every correlated alternative on its back. This adds the two series
that can be fetched under a stable licence; the rest need hand curation and are
listed in OUTSIDE_OPTION_GAPS for the record.

World gold is a knowingly imperfect proxy. The price a Vietnamese household
faces is the SJC bar in VND, which has traded at a large and independently
moving premium over world gold. Treat a world-gold result as a lower bound on
the gold channel, never as a measurement of it.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "interim" / "global_macro"

_UA = "vngross/0.1 (academic research; mai@10thirtylabs.com)"

YAHOO_SERIES = {
    "GC=F": {
        "column": "gold_usd",
        "label": "COMEX gold front-month settlement, USD/oz",
        "transform": "monthly last, log return",
        "caveat": "world gold, not the SJC bar price a Vietnamese household faces",
    },
    "VND=X": {
        "column": "usdvnd",
        "label": "USD/VND spot",
        "transform": "monthly last, log return",
        "caveat": "interbank-style quote, not a Vietcombank board rate",
    },
}

# Series a household actually responds to that no free API publishes monthly.
# Recorded so the absence is a stated limitation rather than a silent one.
OUTSIDE_OPTION_GAPS = {
    "sjc_gold_vnd": "SJC gold bar, VND. The correct gold variable. Needs curation.",
    "cpi_yoy_pct": "Vietnam CPI (GSO). Unlocks the real deposit rate.",
    "vsd_new_accounts": "VSD monthly new retail securities accounts. Direct-equity channel.",
    "property": "No public monthly Vietnam residential price index exists.",
    "credit_m2": "SBV publishes irregularly; not a monthly series.",
}


def fetch(symbol: str) -> Path:
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        "?interval=1mo&period1=1577836800&period2=1790000000"
    )
    request = Request(url, headers={"User-Agent": _UA})  # noqa: S310 - fixed host
    with urlopen(request, timeout=60) as response:  # noqa: S310
        payload = json.loads(response.read())

    result = payload["chart"]["result"][0]
    frame = pd.DataFrame(
        {
            "timestamp": result["timestamp"],
            "close": result["indicators"]["quote"][0]["close"],
        }
    )
    frame["month"] = (
        pd.to_datetime(frame["timestamp"], unit="s").dt.to_period("M").astype(str)
    )
    frame = frame.dropna(subset=["close"])[["month", "close"]]
    # A month-bucketed bar can repeat if the window straddles a boundary.
    frame = frame.groupby("month", as_index=False)["close"].last()

    path = CACHE / f"{symbol.replace('=', '_')}.csv"
    frame.to_csv(path, index=False)
    return path


def main() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    for symbol, config in YAHOO_SERIES.items():
        path = fetch(symbol)
        frame = pd.read_csv(path)
        print(
            f"{config['column']:10} {len(frame):>4} months  "
            f"{frame['month'].min()}..{frame['month'].max()}  -> {path.name}"
        )
    print("\nNot fetched (needs curation):")
    for name, why in OUTSIDE_OPTION_GAPS.items():
        print(f"  {name:18} {why}")


if __name__ == "__main__":
    main()
