"""Macro series: VN-Index, the deposit rate, and the Fmarket cross-check."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pandas as pd

__all__ = [
    "DEPOSIT_RATE_PATH",
    "vnindex_daily",
    "load_deposit_rate",
    "fmarket_nav_history",
    "fmarket_fund_ids",
    "attach_all_macro",
    "cross_check_nav_per_unit",
]

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
DEPOSIT_RATE_PATH = ROOT / "data" / "deposit_rate_12m.csv"

FMARKET_FILTER_URL = "https://api.fmarket.vn/res/products/filter"
FMARKET_NAV_URL = "https://api.fmarket.vn/res/product/get-nav-history"
_FMARKET_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "vngross/0.1 (academic research; mai@10thirtylabs.com)",
}


def vnindex_daily(start: date, end: date) -> pd.DataFrame:
    """Daily VN-Index closes, as `time` and `close`.

    Sourced from vnstock. Returned unfilled: `attach_market_return` handles
    non-trading boundary dates by taking the last close at or before each one,
    which is correct for a return window and does not invent observations.
    """
    from vnstock import Vnstock

    frame = (
        Vnstock()
        .stock(symbol="VNINDEX", source="VCI")
        .quote.history(
            start=str(start), end=str(end), interval="1D"
        )
    )
    frame = frame.rename(columns=str.lower)[["time", "close"]].copy()
    frame["time"] = pd.to_datetime(frame["time"])
    frame = frame[
        (frame["time"] >= pd.Timestamp(start)) & (frame["time"] <= pd.Timestamp(end))
    ]
    return frame.sort_values("time").reset_index(drop=True)


def load_deposit_rate(path: Path | None = None) -> pd.DataFrame:
    """Load the curated 12-month deposit rate series.

    Deliberately loaded from a curated CSV rather than scraped. No clean
    high-frequency 12-month series is published: the World Bank and IMF figures
    are annual and 3-month tenor, wrong on both counts, and the big-four banks
    render their board rates client-side so their historical tables were never
    captured by the Wayback Machine. Every row therefore carries `provenance`,
    and `attach_deposit_rate` pushes that column into the panel so the curation
    is visible at the row level rather than buried in documentation.
    """
    path = Path(path or DEPOSIT_RATE_PATH)
    if not path.exists():
        raise FileNotFoundError(
            f"no curated deposit rate series at {path}. See METHOD.md; this "
            f"series is assembled by hand with per-observation provenance and "
            f"is intentionally not scraped."
        )

    frame = pd.read_csv(path)
    required = {"month", "rate_pct", "provenance"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"{path} is missing required columns: {sorted(missing)}")

    frame["month"] = pd.to_datetime(frame["month"]).dt.to_period("M").astype(str)
    frame["rate_pct"] = pd.to_numeric(frame["rate_pct"], errors="coerce")
    if frame["provenance"].isna().any() or (frame["provenance"] == "").any():
        raise ValueError(f"{path} has rows without provenance; every row needs one")
    return frame.sort_values("month").reset_index(drop=True)


def fmarket_fund_ids() -> pd.DataFrame:
    """List Fmarket's fund catalogue: id, short name, full name."""
    import httpx

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
    response = httpx.post(
        FMARKET_FILTER_URL, json=payload, headers=_FMARKET_HEADERS, timeout=60
    )
    response.raise_for_status()
    rows = response.json().get("data", {}).get("rows", [])
    return pd.DataFrame(
        [
            {"fmarket_id": r.get("id"), "short_name": r.get("shortName"), "name": r.get("name")}
            for r in rows
        ]
    )


def fmarket_nav_history(
    fund_code: str, fmarket_id: int | None = None, to_date: date | None = None
) -> pd.DataFrame:
    """NAV per certificate history from Fmarket, for cross-checking only.

    Fmarket returns NAV per unit and nothing else: no units outstanding, no total
    NAV, no gross flow legs. It can never substitute for a filing. Its value is
    that it is independent, so agreement on `nav_per_unit_end` is strong evidence
    the parse is clean.
    """
    import httpx

    if fmarket_id is None:
        from .discover import load_sources

        for cfg in load_sources().get("managers", {}).values():
            for meta in (cfg.get("funds") or {}).values():
                if (meta or {}).get("code") == fund_code:
                    fmarket_id = (meta or {}).get("fmarket_id")
                    break
        if fmarket_id is None:
            raise KeyError(f"no fmarket_id registered for {fund_code!r}")

    # `fromDate` may be null with isAllData=1, but `toDate` must be a real
    # YYYYMMDD or the API rejects the request with "Dinh dang ngay khong hop le".
    response = httpx.post(
        FMARKET_NAV_URL,
        json={
            "isAllData": 1,
            "productId": int(fmarket_id),
            "fromDate": None,
            "toDate": (to_date or date.today()).strftime("%Y%m%d"),
        },
        headers=_FMARKET_HEADERS,
        timeout=60,
    )
    response.raise_for_status()
    rows = response.json().get("data") or []
    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(columns=["date", "nav_per_unit", "fund_code"])

    date_column = next(
        (c for c in ("navDate", "createAt", "date") if c in frame.columns), None
    )
    nav_column = next((c for c in ("nav", "navValue") if c in frame.columns), None)
    if date_column is None or nav_column is None:
        raise ValueError(f"unexpected Fmarket payload columns: {list(frame.columns)}")

    out = pd.DataFrame(
        {
            "date": pd.to_datetime(frame[date_column]).dt.date,
            "nav_per_unit": pd.to_numeric(frame[nav_column], errors="coerce"),
            "fund_code": fund_code,
        }
    )
    return out.dropna(subset=["nav_per_unit"]).sort_values("date").reset_index(drop=True)


# Fmarket stamps a fund's NAV with the day it is published, which is the day
# after the dealing date the filing closes on. Measured across all five VCBF
# funds: matching on period_end + 1 agrees within 0.1% on 96% of rows, against
# 1% when matching period_end exactly. Aligning on the wrong day turns every
# sharp market move into a phantom parse error, which is exactly what a nearest-
# date match produced on 2025-04-09, a day the index fell about 9%.
FMARKET_PUBLICATION_LAG_DAYS = 1


def cross_check_nav_per_unit(
    panel: pd.DataFrame,
    tolerance_pct: float = 0.1,
    lag_days: int = FMARKET_PUBLICATION_LAG_DAYS,
) -> pd.DataFrame:
    """Compare every parsed `nav_per_unit_end` against Fmarket's independent series.

    Two independent sources agreeing on the same number is the strongest evidence
    available that the parse is clean, which is why this runs over the whole panel
    rather than a sample. Matching is on an exact date, offset by the publication
    lag: a tolerant nearest-date match would hide real errors behind date slack
    and invent errors on volatile days.
    """
    if panel.empty:
        return pd.DataFrame()

    results: list[pd.DataFrame] = []
    for fund_code in sorted(panel["fund_code"].dropna().unique()):
        try:
            reference = fmarket_nav_history(fund_code)
        except Exception as exc:  # noqa: BLE001 - a missing cross-check is not fatal
            log.warning("Fmarket lookup failed for %s: %s", fund_code, exc)
            continue
        if reference.empty:
            continue

        # Fmarket occasionally repeats a date; the later record is the revision.
        reference = reference.drop_duplicates(subset="date", keep="last")
        lookup = pd.Series(
            reference["nav_per_unit"].to_numpy(),
            index=pd.to_datetime(reference["date"]),
        )

        subset = panel.loc[panel["fund_code"] == fund_code]
        period_end = pd.to_datetime(subset["period_end"])
        match_on = period_end + pd.Timedelta(days=lag_days)

        merged = pd.DataFrame(
            {
                "fund_code": fund_code,
                "period_end": period_end.to_numpy(),
                "fmarket_date": match_on.to_numpy(),
                "parsed": subset["nav_per_unit_end"].to_numpy(),
                "fmarket": match_on.map(lookup).to_numpy(),
            }
        )
        merged["diff_pct"] = (
            (merged["parsed"] - merged["fmarket"]) / merged["fmarket"] * 100.0
        )
        # Nullable boolean: a row Fmarket has no observation for is neither
        # agreement nor disagreement, and must not be counted as either.
        merged["agrees"] = (merged["diff_pct"].abs() <= tolerance_pct).astype("boolean")
        merged.loc[merged["fmarket"].isna(), "agrees"] = pd.NA
        results.append(merged)

    return pd.concat(results, ignore_index=True) if results else pd.DataFrame()


def attach_all_macro(
    panel: pd.DataFrame, deposit_rate_path: Path | None = None
) -> pd.DataFrame:
    """Attach the VN-Index return window and, when available, the deposit rate.

    The market join is required and raises on failure. The deposit rate is
    optional: it is a curated series that may not be present yet, and the fund
    flow panel is independently useful without it. A missing series is logged
    loudly rather than silently producing a panel that looks complete.
    """
    from .panel import attach_deposit_rate, attach_market_return

    if panel.empty:
        return panel.copy()

    start = pd.to_datetime(panel["period_start"]).min().date()
    end = pd.to_datetime(panel["period_end"]).max().date()
    out = attach_market_return(panel, vnindex_daily(start, end))

    try:
        out = attach_deposit_rate(out, load_deposit_rate(deposit_rate_path))
    except FileNotFoundError as exc:
        log.warning("deposit rate not attached: %s", exc)
        out["deposit_rate_pct"] = pd.NA
        out["deposit_rate_provenance"] = pd.NA
    return out
