"""Panel assembly: flow measures, macro joins, monthly rollup.

Design rules enforced here, from spec section 4:

  4.2  Gross legs stay separate all the way through. Net flow is derived.
  4.3  Flows are scaled by beginning-of-period NAV, never closing or average.
  4.4  Returns align to each filing's own period window, not a calendar week.
  4.5  Rows failing the identity are quarantined with a written reason, never
       dropped silently.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from .appendix_xxiv import Filing
from .reconcile import (
    chain_continuity,
    check_net_flow_consistency,
    proxy_divergence,
    reconcile,
)

__all__ = [
    "PanelResult",
    "deduplicate",
    "build_fund_period_panel",
    "attach_market_return",
    "attach_deposit_rate",
    "to_monthly",
]

log = logging.getLogger(__name__)

# A dealing period is a week for most funds and a day for some. Anything outside
# this range means the header dates were misread, not that a fund filed yearly.
MIN_PERIOD_DAYS = 0
MAX_PERIOD_DAYS = 35

FLOW_COLUMNS = [
    "gross_subscription_rate",
    "gross_redemption_rate",
    "net_flow_rate",
    "churn_rate",
    "flow_asymmetry",
]


@dataclass
class PanelResult:
    panel: pd.DataFrame
    superseded: pd.DataFrame
    quarantine: pd.DataFrame
    continuity_breaks: pd.DataFrame
    diagnostics: pd.DataFrame


def _derive_flow_measures(frame: pd.DataFrame) -> pd.DataFrame:
    """Add the scaled flow measures.

    Every rate uses `nav_begin` as the denominator. Using closing or average NAV
    would put the flow inside its own denominator and manufacture part of the
    flow-performance relationship the panel exists to measure.
    """
    frame = frame.copy()
    nav_begin = frame["nav_begin"].where(frame["nav_begin"] > 0)

    # A blank gross leg means one of two different things, and conflating them
    # would be a fabrication. When the disclosed net flow is zero, the fund
    # simply had no flow that period and zero is the right rate. When the net
    # flow is non-zero but the legs are blank, the manager did not disclose the
    # decomposition at all: SSIAM files a reduced Appendix XXIV with only line
    # 3.2. That is unknown, not zero, and must stay missing.
    legs_absent = frame["subscriptions"].isna() & frame["redemptions"].isna()
    no_flow = legs_absent & frame["net_flow"].fillna(0.0).eq(0.0)
    undisclosed = legs_absent & ~no_flow

    subs = frame["subscriptions"].fillna(0.0).mask(undisclosed)
    reds = frame["redemptions"].fillna(0.0).abs().mask(undisclosed)

    frame["gross_subscription_rate"] = subs / nav_begin
    frame["gross_redemption_rate"] = reds / nav_begin
    frame["net_flow_rate"] = frame["net_flow"] / nav_begin
    frame["churn_rate"] = (subs + reds) / nav_begin
    frame["gross_legs_disclosed"] = ~undisclosed

    gross_total = subs + reds
    frame["flow_asymmetry"] = ((subs - reds) / gross_total).where(gross_total > 0)
    return frame


def deduplicate(filings: list[Filing]) -> tuple[list[Filing], list[dict]]:
    """Collapse filings that describe the same fund-period.

    VCBF republishes a filing under a new sequence suffix without withdrawing
    the old one: vcbaif_bc_tuan_20250528_1.xlsx and _3.xlsx are byte-distinct
    files carrying identical figures. Two rows for one dealing period would
    double-count the flow and break chain continuity, so they collapse to one.

    When republished figures differ, the later suffix is treated as the
    restatement and kept. A later report can also extend the same opening
    boundary: DCBF published both 2025-01-17/21 and 2025-01-17/23 as weekly
    reports. The longer window supersedes the shorter cumulative snapshot;
    retaining both would count the shared days twice. Every displaced row is
    returned for the audit trail rather than discarded.
    """
    ordered = sorted(
        filings,
        key=lambda f: (
            f.fund_code or "",
            f.period_end or pd.Timestamp.min.date(),
            f.source or "",
        ),
    )

    chosen: dict[tuple, Filing] = {}
    superseded: list[dict] = []
    for filing in ordered:
        key = (filing.fund_code, filing.period_start, filing.period_end)
        previous = chosen.get(key)
        if previous is None:
            chosen[key] = filing
            continue
        identical = previous.values == filing.values
        chosen[key] = filing  # later source wins
        superseded.append(
            {
                **previous.as_row(),
                "superseded_by": filing.source,
                "reason": (
                    "duplicate republication, identical figures"
                    if identical
                    else "superseded by a restatement with different figures"
                ),
            }
        )

    by_opening: dict[tuple, list[Filing]] = {}
    undated: list[Filing] = []
    for filing in chosen.values():
        if filing.period_start is None or filing.period_end is None:
            undated.append(filing)
            continue
        by_opening.setdefault((filing.fund_code, filing.period_start), []).append(
            filing
        )

    kept = list(undated)
    for group in by_opening.values():
        winner = max(group, key=lambda f: (f.period_end, f.source or ""))
        kept.append(winner)
        for filing in group:
            if filing is winner:
                continue
            superseded.append(
                {
                    **filing.as_row(),
                    "superseded_by": winner.source,
                    "reason": (
                        "superseded by a later filing with the same opening "
                        "boundary and a longer period"
                    ),
                }
            )

    return kept, superseded


def build_fund_period_panel(
    filings: list[Filing], fund_meta: dict[str, dict] | None = None
) -> PanelResult:
    """Assemble the fund-period panel and its audit frames.

    A filing enters the panel only if it passes the reconciliation identity.
    Failures go to `quarantine` carrying the residual and a written reason, so
    the exclusion list is itself auditable.
    """
    filings, superseded = deduplicate(filings)

    kept: list[dict] = []
    kept_filings: list[Filing] = []
    quarantined: list[dict] = []
    diagnostics: list[dict] = []

    for filing in filings:
        row = filing.as_row()
        identity = reconcile(filing)
        net_check = check_net_flow_consistency(filing)

        row["reconcile_residual_vnd"] = identity.residual_vnd
        row["net_flow_residual_vnd"] = net_check.residual_vnd

        diagnostic = proxy_divergence(filing)
        diagnostic["source"] = filing.source
        diagnostics.append(diagnostic)

        reasons = []
        if not identity.passed:
            reasons.append(f"identity: {identity.detail}")
        if not net_check.passed:
            reasons.append(f"net flow: {net_check.detail}")
        if row.get("nav_begin") in (None, 0) or pd.isna(row.get("nav_begin")):
            reasons.append("nav_begin missing or zero; flow rates undefined")
        if row.get("period_end") is None:
            reasons.append("period_end missing; cannot place row in time")
        if row.get("period_days") is not None and not (
            MIN_PERIOD_DAYS <= row["period_days"] <= MAX_PERIOD_DAYS
        ):
            reasons.append(
                f"period_days {row['period_days']} outside plausible range "
                f"[{MIN_PERIOD_DAYS}, {MAX_PERIOD_DAYS}]; header dates suspect"
            )

        if reasons:
            quarantined.append({**row, "quarantine_reason": "; ".join(reasons)})
            continue
        kept.append(row)
        kept_filings.append(filing)

    panel = pd.DataFrame(kept)
    if not panel.empty:
        panel = _derive_flow_measures(panel)
        if fund_meta:
            meta = pd.DataFrame.from_dict(fund_meta, orient="index")
            meta.index.name = "fund_code"
            panel = panel.merge(meta.reset_index(), on="fund_code", how="left")
        panel = panel.sort_values(["fund_code", "period_end"]).reset_index(drop=True)

    # Continuity is checked on the exact accepted objects. A quarantined filing
    # cannot bridge two panel rows: doing so would hide the gap created by its
    # exclusion and make continuity_breaks disagree with the published panel.
    breaks = [c.as_row() for c in chain_continuity(kept_filings) if not c.passed]

    return PanelResult(
        panel=panel,
        superseded=pd.DataFrame(superseded),
        quarantine=pd.DataFrame(quarantined),
        continuity_breaks=pd.DataFrame(breaks),
        diagnostics=pd.DataFrame(diagnostics),
    )


def attach_market_return(
    panel: pd.DataFrame, vnindex_daily: pd.DataFrame
) -> pd.DataFrame:
    """Join VN-Index return over each filing's own period window.

    Uses the last available close at or before each boundary date, so a boundary
    falling on a weekend or a Tet holiday does not create a gap. Aligning to a
    fixed calendar week instead would misalign flow and return by a day or more
    whenever a dealing period shifts around a public holiday.
    """
    if panel.empty:
        return panel.copy()
    if vnindex_daily is None or vnindex_daily.empty:
        raise ValueError("vnindex_daily is empty; cannot attach market return")

    index = vnindex_daily.rename(columns=str.lower)[["time", "close"]].copy()
    # Pin both sides to the same datetime resolution. A panel read back from CSV
    # parses to second resolution while the index series arrives in nanoseconds,
    # and pandas 3 refuses to merge_asof across differing units where pandas 2
    # silently coerced. Normalising here keeps the join working on both.
    index["time"] = pd.to_datetime(index["time"]).astype("datetime64[ns]")
    index = index.dropna(subset=["close"]).sort_values("time").reset_index(drop=True)

    out = panel.copy()
    for boundary, column in (("period_start", "index_begin"), ("period_end", "index_end")):
        keys = pd.to_datetime(out[boundary]).astype("datetime64[ns]")
        frame = pd.DataFrame({"time": keys, "_row": range(len(out))}).sort_values("time")
        merged = pd.merge_asof(
            frame, index, on="time", direction="backward", allow_exact_matches=True
        )
        out[column] = merged.sort_values("_row")["close"].to_numpy()

    # The index level at period_start is the last close at or before the first
    # dealing day, which is the level flows and NAV are struck against.
    out["market_return"] = out["index_end"] / out["index_begin"] - 1.0
    out["excess_return"] = out["gross_return"] - out["market_return"]
    return out


def attach_deposit_rate(
    panel: pd.DataFrame, deposit_monthly: pd.DataFrame
) -> pd.DataFrame:
    """Join the monthly deposit rate on each row's period-end month.

    The `provenance` column is carried into the panel deliberately. The series is
    hand-curated, not scraped, and the panel should make that visible at the row
    level rather than burying it in documentation.
    """
    if panel.empty:
        return panel.copy()
    if deposit_monthly is None or deposit_monthly.empty:
        raise ValueError("deposit_monthly is empty; cannot attach deposit rate")

    rates = deposit_monthly.copy()
    rates["month"] = pd.PeriodIndex(pd.to_datetime(rates["month"]), freq="M")
    keep = ["month", "rate_pct", "provenance"]
    for optional in ("rate_low_pct", "rate_high_pct", "tenor", "source", "bank"):
        if optional in rates.columns:
            keep.append(optional)
    rates = rates[keep].rename(
        columns={
            "rate_pct": "deposit_rate_pct",
            "provenance": "deposit_rate_provenance",
            "rate_low_pct": "deposit_rate_low_pct",
            "rate_high_pct": "deposit_rate_high_pct",
            "tenor": "deposit_rate_tenor",
            "source": "deposit_rate_source",
            "bank": "deposit_rate_bank",
        }
    )

    out = panel.copy()
    out["month"] = pd.PeriodIndex(pd.to_datetime(out["period_end"]), freq="M")
    out = out.merge(rates, on="month", how="left")
    out["month"] = out["month"].astype(str)
    return out


def to_monthly(panel: pd.DataFrame) -> pd.DataFrame:
    """Roll the fund-period panel up to fund-month.

    Flows sum, returns compound, and NAV is taken from the month's first opening
    and last closing. `reconcile_residual_vnd` therefore exposes any missing or
    excluded period inside a month instead of implying that every rollup closes.
    A gap crossing a month boundary can still have zero monthly residual, so
    continuity_breaks.csv remains the authoritative completeness audit.
    """
    if panel.empty:
        return panel.copy()

    frame = panel.copy()
    frame["period_end"] = pd.to_datetime(frame["period_end"])
    frame["period_start"] = pd.to_datetime(frame["period_start"])
    frame["month"] = frame["period_end"].dt.to_period("M")
    frame = frame.sort_values(["fund_code", "period_end"])

    def _compound(series: pd.Series) -> float:
        clean = series.dropna()
        if clean.empty:
            return float("nan")
        return float((1.0 + clean).prod() - 1.0)

    grouped = frame.groupby(["fund_code", "month"], sort=True)
    monthly = grouped.agg(
        period_start=("period_start", "min"),
        period_end=("period_end", "max"),
        n_periods=("period_end", "size"),
        nav_begin=("nav_begin", "first"),
        nav_end=("nav_end", "last"),
        nav_per_unit_begin=("nav_per_unit_begin", "first"),
        nav_per_unit_end=("nav_per_unit_end", "last"),
        subscriptions=("subscriptions", "sum"),
        redemptions=("redemptions", "sum"),
        net_flow=("net_flow", "sum"),
        chg_investment=("chg_investment", "sum"),
        chg_distribution=("chg_distribution", "sum"),
        period_days=("period_days", "sum"),
    ).reset_index()

    monthly["gross_return"] = grouped["gross_return"].apply(_compound).to_numpy()
    monthly["reconcile_residual_vnd"] = monthly["nav_end"] - (
        monthly["nav_begin"]
        + monthly["chg_investment"].fillna(0.0)
        + monthly["net_flow"].fillna(0.0)
        + pd.to_numeric(monthly["chg_distribution"], errors="coerce").fillna(0.0)
    )
    if "market_return" in frame.columns:
        monthly["market_return"] = grouped["market_return"].apply(_compound).to_numpy()
        monthly["excess_return"] = monthly["gross_return"] - monthly["market_return"]
    for column in ("deposit_rate_pct", "deposit_rate_provenance", "asset_class",
                   "fund_name", "manager_id"):
        if column in frame.columns:
            monthly[column] = grouped[column].last().to_numpy()

    monthly = _derive_flow_measures(monthly)
    monthly["month"] = monthly["month"].astype(str)
    return monthly
