"""Fund-level reference data from Fmarket: inception, ongoing fees, documents.

Metadata, not panel values. Every flow figure in this project traces to an
Appendix XXIV filing and nothing here changes that; these columns exist to
control and to date, not to measure.

The inception date is the point of the exercise. `_sample_filter` currently
drops each fund's first four *panel* observations as launch months, but panel
position is not fund age. DCDS was seventeen years old when its coverage
starts, so those four rows are ordinary months being thrown away, while a fund
that genuinely launched inside the window may need more than four dropped.
`months_since_inception` replaces a proxy with the quantity it was proxying for.

Fee columns are the distributor's published terms. The charter PDFs in
`documents` are the binding version and can disagree; both are recorded so a
disagreement is discoverable.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import httpx
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
REFERENCE_OUT = ROOT / "data" / "fund_reference.csv"
DOCUMENTS_OUT = ROOT / "data" / "fund_documents.csv"

FILTER_URL = "https://api.fmarket.vn/res/products/filter"
DETAIL_URL = "https://api.fmarket.vn/res/products/{fmarket_id}"
HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "vngross/0.1 (academic research; mai@10thirtylabs.com)",
}

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


def _date(epoch_ms: float | None) -> dt.date | None:
    if not epoch_ms:
        return None
    return dt.datetime.fromtimestamp(epoch_ms / 1000).date()


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


def main() -> None:
    funds = catalogue()
    reference: list[dict] = []
    documents: list[dict] = []
    missing: list[str] = []

    for fund_code, short_name in FUND_TO_SHORTNAME.items():
        entry = funds.get(short_name)
        if entry is None:
            missing.append(f"{fund_code} (looked for '{short_name}')")
            continue

        fmarket_id = entry["id"]
        url = DETAIL_URL.format(fmarket_id=fmarket_id)
        detail = httpx.get(url, headers=HEADERS, timeout=60).json()["data"]

        reference.append(
            {
                "fund_code": fund_code,
                "fmarket_id": fmarket_id,
                "short_name": short_name,
                "fund_name_vi": detail.get("name"),
                "manager_name": (detail.get("owner") or {}).get("shortName"),
                "inception_date": _date(detail.get("firstIssueAt")),
                "management_fee_pct": detail.get("managementFee"),
                "performance_fee_pct": detail.get("performanceFee"),
                "min_holding_days": detail.get("holdingMin"),
                "issue_price_vnd": detail.get("price"),
                "source_url": url,
                "retrieved_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            }
        )

        for doc in detail.get("productDocuments") or []:
            documents.append(
                {
                    "fund_code": fund_code,
                    "document_name": doc.get("fileName"),
                    "applies_from": doc.get("applyAt"),
                    "url": doc.get("url"),
                    "source_url": url,
                }
            )

    pd.DataFrame(reference).to_csv(REFERENCE_OUT, index=False)
    pd.DataFrame(documents).to_csv(DOCUMENTS_OUT, index=False)
    print(f"{len(reference)} funds -> {REFERENCE_OUT.name}")
    print(f"{len(documents)} documents -> {DOCUMENTS_OUT.name}")
    if missing:
        print("\nNot on Fmarket (use the prospectus):")
        for name in missing:
            print(f"  {name}")


if __name__ == "__main__":
    main()
