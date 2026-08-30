"""Redemption and subscription fee schedules from Fmarket, per panel fund.

Why this exists. Two results in the panel need it.

The cross-manager book-flow conversion gap is not interpretable without it. A
manager whose funds charge 2% to redeem inside twelve months will retain book
flow that an otherwise identical manager loses, and none of that difference is
distribution skill. The gap has to be read net of the fee schedule or not at
all.

The larger reason is the subscription/redemption asymmetry. Macro variables and
performance both move the subscription leg and neither moves redemptions, and a
holding-period fee schedule explains why. A subscription can respond to this
month's deposit rate. A redemption is priced off the investor's own purchase
date, so the decision is governed by a private clock that no market variable
observes. Aggregated over a book with staggered entry dates, that produces
exactly the macro-insensitive redemption series the panel shows.

Fmarket is a distributor and publishes the schedule it transacts on. That makes
it a good source for what an investor actually pays and a poor source for the
fund charter, which may differ. Rows carry `source_url` so a disagreement with
the prospectus is discoverable rather than silent.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "fee_schedules.csv"

FILTER_URL = "https://api.fmarket.vn/res/products/filter"
DETAIL_URL = "https://api.fmarket.vn/res/products/{fmarket_id}"
HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "vngross/0.1 (academic research; mai@10thirtylabs.com)",
}

# Panel fund_code -> Fmarket shortName. Only VinaCapital carries an fmarket_id
# in sources.yaml, so the rest are matched by name and verified by the miss list
# this script prints rather than assumed.
FUND_TO_SHORTNAME = {
    "VINACAPITAL-VEOF": "VEOF",
    "VINACAPITAL-VESAF": "VESAF",
    "VINACAPITAL-VFF": "VFF",
    "VINACAPITAL-VIBF": "VIBF",
    "VINACAPITAL-VLBF": "VLBF",
    "DCDS": "DCDS",
    "DCBF": "DCBF",
    "DCIP": "DCIP",
    "DCDE": "DCDE",
    "SSIBF": "SSIBF",
    "SSI-SCA": "SSISCA",
    "SSI-EF": "SSIEF",
    "VLGF": "VLGF",
    "VCBF-BCF": "VCBF-BCF",
    "VCBF-MGF": "VCBF-MGF",
    "VCBF-TBF": "VCBF-TBF",
    "VCBF-FIF": "VCBF-FIF",
    "VCBF-AIF": "VCBF-AIF",
}


def catalogue() -> dict[str, dict]:
    payload = {
        "types": ["NEW_FUND", "TRADING_FUND"],
        "issuerIds": [],
        "sortOrder": "DESC",
        "sortField": "navTo6Months",
        "page": 1,
        "pageSize": 200,
        "isIpo": False,
        "fundAssetTypes": [],
    }
    response = httpx.post(FILTER_URL, json=payload, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return {r["shortName"]: r for r in response.json()["data"]["rows"]}


def schedule(fund_code: str, entry: dict) -> list[dict]:
    fmarket_id = entry["id"]
    url = DETAIL_URL.format(fmarket_id=fmarket_id)
    response = httpx.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    detail = response.json()["data"]

    rows = []
    for fee in detail.get("productFeeList") or []:
        program = fee.get("productProgram") or {}
        rows.append(
            {
                "fund_code": fund_code,
                "fmarket_id": fmarket_id,
                "short_name": entry["shortName"],
                "fee_type": fee.get("type"),
                "tier_from": fee.get("beginVolume"),
                "tier_from_operator": (fee.get("beginRelationalOperator") or {}).get("code"),
                "tier_to": fee.get("endVolume"),
                "tier_to_operator": (fee.get("endRelationalOperator") or {}).get("code"),
                # MONTH/DAY means a holding-period tier; MONEY means a size tier.
                "tier_unit": fee.get("feeUnitType"),
                "fee_pct": fee.get("fee"),
                "program": program.get("name"),
                "holding_min_days": program.get("holdingMin"),
                "source_url": url,
            }
        )
    return rows


def main() -> None:
    funds = catalogue()
    rows: list[dict] = []
    missing: list[str] = []

    for fund_code, short_name in FUND_TO_SHORTNAME.items():
        entry = funds.get(short_name)
        if entry is None:
            missing.append(f"{fund_code} (looked for '{short_name}')")
            continue
        rows.extend(schedule(fund_code, entry))

    frame = pd.DataFrame(rows)
    frame.to_csv(OUT, index=False)

    sells = frame[frame["fee_type"].eq("SELL")]
    holding = sells[sells["tier_unit"].isin({"MONTH", "DAY"})]
    print(f"{len(frame)} fee rows for {frame['fund_code'].nunique()} funds -> {OUT}")
    print(f"{len(holding)} holding-period redemption tiers across "
          f"{holding['fund_code'].nunique()} funds")
    if missing:
        print("\nNot found on Fmarket (need the prospectus instead):")
        for name in missing:
            print(f"  {name}")


if __name__ == "__main__":
    main()
