"""Growth, retention, and investor-segment analytics.

The public filings identify the stock of fund certificates held by foreign
investors.  Changes in that stock can therefore identify *net unit demand* by
foreign holders; the residual change in total certificates identifies domestic
net unit demand.  They cannot identify foreign subscriptions and redemptions
separately, investor counts, customer identity, or transfers between funds.

Every investor split in this module is guarded by physical stock constraints,
cross-filing continuity, the foreign-value identity, and an independent check
that the change in total units approximately reproduces disclosed net flow.
The remaining functions turn the corrected public panel into manager-facing
signals while keeping aggregate-market evidence separate from CRM propensity.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

from .analysis import RegressionResult, ols

__all__ = [
    "infer_investor_demand",
    "aggregate_investor_demand_monthly",
    "performance_horizon_response",
    "book_flow_conversion",
    "flow_persistence",
    "regime_transition_table",
    "manager_rotation_upper_bound",
    "aggregate_segment_demand",
    "hac_ols",
    "investor_macro_sensitivity",
    "investor_performance_response",
    "investor_demand_persistence",
    "performance_convexity",
    "CONVEXITY_BREAKS",
]

NAV_ABS_TOL_VND = 5.0
NAV_REL_TOL = 1e-9
FOREIGN_VALUE_NAV_TOL = 0.001
PRIOR_STOCK_REL_TOL = 0.001
FOREIGN_TO_TOTAL_TOL = 1.001
UNIT_FLOW_NAV_TOL = 0.01
INCEPTION_OBSERVATIONS = 4

# Months of true fund age excluded as launch months, where an inception date is
# known. Percentage flow rates are meaningless while the NAV denominator is
# small and growing fast: VLGF's fifth month shows a 1,316% subscription rate
# because a single mandate took it from VND164bn to VND2,065bn, and that one row
# is enough to flip the 3-month performance coefficient from +1.98 to -1.86.
# Estimates are stable for any threshold from 6 to 24 months, so this sits well
# inside the flat region rather than on the edge of it.
LAUNCH_EXCLUSION_MONTHS = 12

# Fund reference data, fetched by scripts/fetch_fund_reference.py. Absent is a
# supported state: without it the launch filter falls back to panel position,
# which is what this project did before the dates existed.
FUND_REFERENCE_PATH = Path(__file__).resolve().parents[1] / "data" / "fund_reference.csv"


def load_inception_dates(path: Path | None = None) -> dict[str, pd.Period]:
    """Fund inception months, keyed by fund_code. Empty dict if unavailable."""
    target = path or FUND_REFERENCE_PATH
    if not target.exists():
        return {}
    frame = pd.read_csv(target)
    if "fund_code" not in frame or "inception_date" not in frame:
        return {}
    stamps = pd.to_datetime(frame["inception_date"], errors="coerce")
    valid = frame.assign(inception_stamp=stamps).dropna(subset=["inception_stamp"])
    return {
        str(code): pd.Period(stamp, freq="M")
        for code, stamp in zip(valid["fund_code"], valid["inception_stamp"])
    }

_SPLIT_NUMERIC = (
    "nav_begin",
    "nav_end",
    "nav_per_unit_begin",
    "nav_per_unit_end",
    "units_begin",
    "units_end",
    "foreign_units",
    "foreign_value",
    "prior_foreign_units",
    "prior_foreign_value",
    "net_flow",
    "chg_investment",
    "chg_distribution",
)
_SPLIT_OUTPUTS = (
    "delta_total_units",
    "delta_foreign_units",
    "delta_domestic_units",
    "midpoint_nav_per_unit",
    "total_unit_demand_vnd",
    "foreign_unit_demand_vnd",
    "domestic_unit_demand_vnd",
    "opening_total_aum_vnd",
    "opening_foreign_aum_vnd",
    "opening_domestic_aum_vnd",
    "total_demand_rate",
    "foreign_demand_rate",
    "domestic_demand_rate",
    "unit_proxy_error_vnd",
    "unit_proxy_error_nav_rate",
)
_FLOW_COLUMNS = (
    "gross_subscription_rate",
    "gross_redemption_rate",
    "net_flow_rate",
)


def _num(value: object) -> float:
    value = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    return float(value) if pd.notna(value) else float("nan")


def _finite(value: object) -> bool:
    try:
        return bool(np.isfinite(float(value)))
    except (TypeError, ValueError):
        return False


def _invalid_split(reason: str) -> dict[str, object]:
    result: dict[str, object] = {
        "split_valid": False,
        "split_reason": reason,
        "reason": reason,  # compatibility with the first exploratory API
        "split_opening_source": None,
        "split_measure": "net_unit_demand",
        "split_caveat": (
            "Net certificate-stock change only; not gross subscriptions, gross "
            "redemptions, investor counts, customer identity, or fund transfers."
        ),
    }
    result.update({column: np.nan for column in _SPLIT_OUTPUTS})
    return result


def _stock_issue(row: pd.Series, *, prefix: str = "current") -> str | None:
    total = _num(row.get("units_end"))
    foreign = _num(row.get("foreign_units"))
    foreign_value = _num(row.get("foreign_value"))
    nav_per_unit = _num(row.get("nav_per_unit_end"))
    nav = _num(row.get("nav_end"))
    missing = [
        name
        for name, value in (
            ("units_end", total),
            ("foreign_units", foreign),
            ("foreign_value", foreign_value),
            ("nav_per_unit_end", nav_per_unit),
            ("nav_end", nav),
        )
        if not _finite(value)
    ]
    if missing:
        return f"{prefix} stock missing: {', '.join(missing)}"
    if total <= 0 or foreign < 0 or nav_per_unit <= 0 or nav <= 0:
        return f"{prefix} stock has non-positive total/NAV or negative foreign units"
    if foreign > total * FOREIGN_TO_TOTAL_TOL:
        return (
            f"{prefix} foreign_units {foreign:.6g} exceed total units "
            f"{total:.6g} (including 0.1% tolerance)"
        )
    value_error = abs(foreign_value - foreign * nav_per_unit)
    value_tolerance = FOREIGN_VALUE_NAV_TOL * abs(nav)
    if value_error > value_tolerance:
        return (
            f"{prefix} foreign_value identity gap {value_error:,.0f} VND exceeds "
            f"0.1% of total NAV ({value_tolerance:,.0f} VND)"
        )
    return None


def _opening_stock_issue(
    total: float,
    foreign: float,
    nav_per_unit: float,
    nav: float,
    prior_foreign_value: float,
) -> str | None:
    if not all(_finite(value) for value in (total, foreign, nav_per_unit, nav)):
        return "opening total/foreign units or NAV per unit missing"
    if total <= 0 or foreign < 0 or nav_per_unit <= 0 or nav <= 0:
        return "opening stock has non-positive total/NAV or negative foreign units"
    if foreign > total * FOREIGN_TO_TOTAL_TOL:
        return "opening foreign units exceed opening total units"
    if _finite(prior_foreign_value):
        error = abs(prior_foreign_value - foreign * nav_per_unit)
        if error > FOREIGN_VALUE_NAV_TOL * abs(nav):
            return "opening prior foreign-value identity exceeds 0.1% of total NAV"
    return None


def infer_investor_demand(period: pd.DataFrame) -> pd.DataFrame:
    """Infer validated foreign and domestic **net unit demand** per filing.

    The foreign leg is ``foreign_units[t] - foreign_units[t-1]``.  The domestic
    leg is the residual change in total certificates.  Both are valued at the
    midpoint of opening and closing NAV per certificate.  A first observed row
    is usable only when its same-filing prior foreign stock and opening total
    units are available and physically valid.

    Invalid rows remain in the returned frame with ``split_valid=False`` and a
    written reason.  All inferred values are null on a failed row.
    """
    if period.empty:
        out = period.copy()
        for column in ("split_valid", "split_reason", "reason", *_SPLIT_OUTPUTS):
            if column not in out:
                out[column] = pd.Series(dtype="object" if "reason" in column else "float64")
        return out

    frame = period.copy()
    for column in _SPLIT_NUMERIC:
        if column not in frame:
            frame[column] = np.nan
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    for column in ("period_start", "period_end"):
        if column not in frame:
            frame[column] = pd.NaT
        frame[column] = pd.to_datetime(frame[column], errors="coerce")
    if "fund_code" not in frame:
        frame["fund_code"] = np.nan
    frame = frame.sort_values(["fund_code", "period_end"], na_position="last").reset_index(drop=True)

    results: list[dict[str, object]] = []
    previous_by_fund: dict[str, pd.Series] = {}
    for _, row in frame.iterrows():
        code = row.get("fund_code")
        if pd.isna(code):
            results.append(_invalid_split("fund_code missing"))
            continue
        code = str(code)

        required = (
            "nav_begin",
            "nav_end",
            "nav_per_unit_begin",
            "nav_per_unit_end",
            "units_end",
            "foreign_units",
            "foreign_value",
            "net_flow",
        )
        missing = [name for name in required if not _finite(row.get(name))]
        if missing:
            results.append(_invalid_split(f"critical fields missing: {', '.join(missing)}"))
            previous_by_fund[code] = row
            continue

        nav_begin = _num(row["nav_begin"])
        nav_end = _num(row["nav_end"])
        investment = _num(row.get("chg_investment"))
        distribution = _num(row.get("chg_distribution"))
        investment = investment if _finite(investment) else 0.0
        distribution = distribution if _finite(distribution) else 0.0
        residual = nav_end - (nav_begin + investment + _num(row["net_flow"]) + distribution)
        nav_tolerance = max(NAV_ABS_TOL_VND, NAV_REL_TOL * abs(nav_end))
        if not _finite(residual) or abs(residual) > nav_tolerance:
            results.append(
                _invalid_split(
                    f"NAV identity residual {residual:,.2f} exceeds {nav_tolerance:,.2f} VND"
                )
            )
            previous_by_fund[code] = row
            continue

        issue = _stock_issue(row)
        if issue:
            results.append(_invalid_split(issue))
            previous_by_fund[code] = row
            continue

        previous = previous_by_fund.get(code)
        if previous is None:
            opening_total = _num(row.get("units_begin"))
            opening_foreign = _num(row.get("prior_foreign_units"))
            opening_source = "same_filing_prior"
            issue = _opening_stock_issue(
                opening_total,
                opening_foreign,
                _num(row.get("nav_per_unit_begin")),
                nav_begin,
                _num(row.get("prior_foreign_value")),
            )
            if issue:
                results.append(_invalid_split(f"first observed row: {issue}"))
                previous_by_fund[code] = row
                continue
        else:
            previous_issue = _stock_issue(previous, prefix="previous")
            if previous_issue:
                results.append(_invalid_split(previous_issue))
                previous_by_fund[code] = row
                continue
            start = row.get("period_start")
            previous_end = previous.get("period_end")
            if pd.isna(start) or pd.isna(previous_end):
                results.append(_invalid_split("period boundary missing; contiguity unavailable"))
                previous_by_fund[code] = row
                continue
            gap = int((start - previous_end).days)
            if not 0 <= gap <= 3:
                results.append(_invalid_split(f"non-contiguous filing pair: {gap}-day boundary gap"))
                previous_by_fund[code] = row
                continue
            opening_total = _num(previous.get("units_end"))
            opening_foreign = _num(previous.get("foreign_units"))
            opening_source = "previous_filing_close"
            prior_foreign = _num(row.get("prior_foreign_units"))
            if _finite(prior_foreign):
                relative_error = abs(prior_foreign - opening_foreign) / max(
                    1.0, abs(opening_foreign)
                )
                if relative_error > PRIOR_STOCK_REL_TOL:
                    results.append(
                        _invalid_split(
                            "same-filing prior foreign units disagree with previous filing "
                            f"by {relative_error:.4%}"
                        )
                    )
                    previous_by_fund[code] = row
                    continue

        current_total = _num(row["units_end"])
        current_foreign = _num(row["foreign_units"])
        midpoint = (_num(row["nav_per_unit_begin"]) + _num(row["nav_per_unit_end"])) / 2
        delta_total = current_total - opening_total
        delta_foreign = current_foreign - opening_foreign
        delta_domestic = delta_total - delta_foreign
        total_demand = delta_total * midpoint
        foreign_demand = delta_foreign * midpoint
        domestic_demand = delta_domestic * midpoint
        proxy_error = total_demand - _num(row["net_flow"])
        proxy_error_rate = abs(proxy_error) / abs(nav_begin)
        if not _finite(midpoint) or midpoint <= 0:
            results.append(_invalid_split("midpoint NAV per certificate missing or non-positive"))
            previous_by_fund[code] = row
            continue
        if not _finite(proxy_error_rate) or proxy_error_rate > UNIT_FLOW_NAV_TOL:
            results.append(
                _invalid_split(
                    f"unit-change/net-flow proxy error is {proxy_error_rate:.3%} of opening NAV"
                )
            )
            previous_by_fund[code] = row
            continue

        opening_foreign_aum = opening_foreign * _num(row["nav_per_unit_begin"])
        opening_domestic_aum = (opening_total - opening_foreign) * _num(
            row["nav_per_unit_begin"]
        )
        opening_total_aum = opening_foreign_aum + opening_domestic_aum
        result: dict[str, object] = {
            "split_valid": True,
            "split_reason": None,
            "reason": None,
            "split_opening_source": opening_source,
            "split_measure": "net_unit_demand",
            "split_caveat": (
                "Net certificate-stock change only; not gross subscriptions, gross "
                "redemptions, investor counts, customer identity, or fund transfers."
            ),
            "delta_total_units": delta_total,
            "delta_foreign_units": delta_foreign,
            "delta_domestic_units": delta_domestic,
            "midpoint_nav_per_unit": midpoint,
            "total_unit_demand_vnd": total_demand,
            "foreign_unit_demand_vnd": foreign_demand,
            "domestic_unit_demand_vnd": domestic_demand,
            "opening_total_aum_vnd": opening_total_aum,
            "opening_foreign_aum_vnd": opening_foreign_aum,
            "opening_domestic_aum_vnd": opening_domestic_aum,
            "total_demand_rate": total_demand / opening_total_aum
            if opening_total_aum > 0
            else np.nan,
            "foreign_demand_rate": foreign_demand / opening_foreign_aum
            if opening_foreign_aum > 0
            else np.nan,
            "domestic_demand_rate": domestic_demand / opening_domestic_aum
            if opening_domestic_aum > 0
            else np.nan,
            "unit_proxy_error_vnd": proxy_error,
            "unit_proxy_error_nav_rate": proxy_error_rate,
        }
        results.append(result)
        previous_by_fund[code] = row

    return pd.concat([frame, pd.DataFrame(results)], axis=1)


def aggregate_investor_demand_monthly(period: pd.DataFrame) -> pd.DataFrame:
    """Aggregate only fund-months whose constituent filing splits all validate."""
    if period.empty or "split_valid" not in period:
        return pd.DataFrame()
    frame = period.copy()
    frame["period_end"] = pd.to_datetime(frame["period_end"], errors="coerce")
    frame["period_start"] = pd.to_datetime(frame.get("period_start"), errors="coerce")
    frame["month_period"] = frame["period_end"].dt.to_period("M")
    frame = frame.sort_values(["fund_code", "period_end"])

    month_keys = frame[["fund_code", "month_period"]].drop_duplicates().copy()
    month_keys["fund_month_order"] = month_keys.groupby("fund_code", observed=True).cumcount() + 1
    frame = frame.merge(month_keys, on=["fund_code", "month_period"], how="left")
    valid = frame.groupby(["fund_code", "month_period"], observed=True)["split_valid"].transform(
        lambda values: bool(values.notna().all() and values.astype(bool).all())
    )
    frame = frame[valid].copy()
    if frame.empty:
        return pd.DataFrame()

    rows: list[dict[str, object]] = []
    for (fund, month), group in frame.groupby(
        ["fund_code", "month_period"], observed=True, sort=True
    ):
        group = group.sort_values("period_end")
        foreign = group["foreign_unit_demand_vnd"].sum(min_count=1)
        domestic = group["domestic_unit_demand_vnd"].sum(min_count=1)
        total = group["total_unit_demand_vnd"].sum(min_count=1)
        if not np.isclose(foreign + domestic, total, rtol=1e-10, atol=5.0):
            continue
        opening_foreign = group["opening_foreign_aum_vnd"].iloc[0]
        opening_domestic = group["opening_domestic_aum_vnd"].iloc[0]
        opening_total = opening_foreign + opening_domestic
        row: dict[str, object] = {
            "fund_code": fund,
            "month": str(month),
            "fund_month_order": int(group["fund_month_order"].iloc[0]),
            "period_start": group["period_start"].min(),
            "period_end": group["period_end"].max(),
            "n_periods": len(group),
            "split_valid": True,
            "total_unit_demand_vnd": total,
            "foreign_unit_demand_vnd": foreign,
            "domestic_unit_demand_vnd": domestic,
            "opening_total_aum_vnd": opening_total,
            "opening_foreign_aum_vnd": opening_foreign,
            "opening_domestic_aum_vnd": opening_domestic,
            "total_demand_rate": total / opening_total if opening_total > 0 else np.nan,
            "foreign_demand_rate": foreign / opening_foreign
            if opening_foreign > 0
            else np.nan,
            "domestic_demand_rate": domestic / opening_domestic
            if opening_domestic > 0
            else np.nan,
            "opening_foreign_share": opening_foreign / opening_total
            if opening_total > 0
            else np.nan,
            "mean_unit_proxy_error_nav_rate": group["unit_proxy_error_nav_rate"].mean(),
        }
        if "net_flow" in group:
            row["disclosed_net_flow_vnd"] = group["net_flow"].sum(min_count=1)
        if "market_return" in group:
            values = pd.to_numeric(group["market_return"], errors="coerce")
            row["market_return"] = (1 + values).prod(min_count=1) - 1
        for metadata in ("manager_id", "asset_class", "fund_name"):
            if metadata in group:
                nonmissing = group[metadata].dropna()
                row[metadata] = nonmissing.iloc[-1] if not nonmissing.empty else np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def _clean_month_mask(frame: pd.DataFrame) -> pd.Series:
    if "reconcile_residual_vnd" in frame and "nav_end" in frame:
        residual = pd.to_numeric(frame["reconcile_residual_vnd"], errors="coerce")
        nav = pd.to_numeric(frame["nav_end"], errors="coerce").abs()
        return residual.notna() & nav.notna() & residual.abs().le(np.maximum(5.0, 1e-9 * nav))
    required = {"nav_begin", "nav_end", "net_flow"}
    if required <= set(frame):
        investment = pd.to_numeric(frame.get("chg_investment", 0.0), errors="coerce").fillna(0)
        distribution = pd.to_numeric(frame.get("chg_distribution", 0.0), errors="coerce").fillna(0)
        residual = pd.to_numeric(frame["nav_end"], errors="coerce") - (
            pd.to_numeric(frame["nav_begin"], errors="coerce")
            + investment
            + pd.to_numeric(frame["net_flow"], errors="coerce")
            + distribution
        )
        nav = pd.to_numeric(frame["nav_end"], errors="coerce").abs()
        return residual.notna() & nav.notna() & residual.abs().le(np.maximum(5.0, 1e-9 * nav))
    return pd.Series(False, index=frame.index)


def _monthly_base(monthly: pd.DataFrame) -> pd.DataFrame:
    frame = monthly.copy()
    if frame.empty or "fund_code" not in frame or "month" not in frame:
        return pd.DataFrame()
    frame["month_period"] = pd.PeriodIndex(pd.to_datetime(frame["month"], errors="coerce"), freq="M")
    frame = frame.sort_values(["fund_code", "month_period"]).reset_index(drop=True)
    frame["fund_observation_number"] = frame.groupby("fund_code", observed=True).cumcount() + 1

    # Panel position is not fund age. Most funds here were years old when their
    # coverage begins, so counting from the first filed row treats ordinary
    # months as launch months and discards them.
    inception = load_inception_dates()
    if inception:
        starts = frame["fund_code"].map(inception)
        frame["months_since_inception"] = np.where(
            starts.notna(),
            (frame["month_period"].astype("int64") - starts.map(
                lambda p: p.ordinal if isinstance(p, pd.Period) else np.nan
            ).astype("float64")) + 1,
            np.nan,
        )
    else:
        frame["months_since_inception"] = np.nan

    frame["clean_month"] = _clean_month_mask(frame)
    return frame


def _sample_filter(frame: pd.DataFrame, *, gross: bool, sample: str = "all") -> pd.DataFrame:
    # True fund age where known, panel position where it is not.
    age = frame.get("months_since_inception")
    if age is None:
        past_launch = frame["fund_observation_number"].gt(INCEPTION_OBSERVATIONS)
    else:
        past_launch = np.where(
            age.notna(),
            age.gt(LAUNCH_EXCLUSION_MONTHS),
            frame["fund_observation_number"].gt(INCEPTION_OBSERVATIONS),
        )
    out = frame[
        frame["clean_month"] & past_launch & frame["fund_code"].ne("DCIP")
    ].copy()
    if gross and "gross_legs_disclosed" in out:
        out = out[out["gross_legs_disclosed"].fillna(False).astype(bool)]
    if sample == "equity_balanced":
        out = out[out["asset_class"].isin(["equity", "balanced"])]
    elif sample == "bond":
        out = out[out["asset_class"].eq("bond")]
    elif sample == "vinacapital":
        out = out[out["manager_id"].eq("vinacapital")]
    elif sample != "all":
        raise ValueError(f"unknown sample: {sample}")
    return out


def _exact_lagged_compound(frame: pd.DataFrame, horizon: int, return_col: str) -> pd.Series:
    result = pd.Series(np.nan, index=frame.index, dtype=float)
    for _, group in frame.groupby("fund_code", observed=True, sort=False):
        indices = group.index.to_numpy()
        months = group["month_period"].astype("int64").to_numpy()
        returns = pd.to_numeric(group[return_col], errors="coerce").where(group["clean_month"]).to_numpy()
        for position in range(horizon, len(group)):
            window = returns[position - horizon : position]
            month_window = months[position - horizon : position + 1]
            if np.isfinite(window).all() and np.all(np.diff(month_window) == 1):
                result.loc[indices[position]] = float(np.prod(1 + window) - 1)
    return result


def _exact_lag(frame: pd.DataFrame, column: str) -> pd.Series:
    result = pd.Series(np.nan, index=frame.index, dtype=float)
    for _, group in frame.groupby("fund_code", observed=True, sort=False):
        indices = group.index.to_numpy()
        months = group["month_period"].astype("int64").to_numpy()
        values = pd.to_numeric(group[column], errors="coerce").to_numpy()
        clean = group["clean_month"].to_numpy(dtype=bool)
        for position in range(1, len(group)):
            if months[position] - months[position - 1] == 1 and clean[position - 1]:
                result.loc[indices[position]] = values[position - 1]
    return result


def _winsorise(frame: pd.DataFrame, columns: Sequence[str], quantile: float | None) -> pd.DataFrame:
    out = frame.copy()
    if quantile is None or quantile <= 0:
        return out
    for column in columns:
        values = pd.to_numeric(out[column], errors="coerce")
        low, high = values.quantile([quantile, 1 - quantile])
        out[column] = values.clip(low, high)
    return out


def _fit_dummy_fe(
    frame: pd.DataFrame,
    dependent: str,
    predictor: str | Sequence[str],
    *,
    name: str,
    note: str = "",
) -> RegressionResult:
    """Fixed-effect fit on one or several predictors.

    A single predictor may be passed as a bare string; the piecewise convexity
    specification passes the three rank segments together, which is the whole
    point of it, since their slopes are only comparable when estimated jointly.
    """
    predictors = [predictor] if isinstance(predictor, str) else list(predictor)
    data = frame.dropna(subset=[dependent, *predictors, "fund_code", "month"]).copy()
    if data.empty:
        raise ValueError("no complete cases")
    fund_dummies = pd.get_dummies(data["fund_code"], prefix="fund", drop_first=True, dtype=float)
    month_dummies = pd.get_dummies(data["month"], prefix="month", drop_first=True, dtype=float)
    design = pd.concat(
        [
            pd.Series(1.0, index=data.index, name="const"),
            *(
                pd.to_numeric(data[column], errors="coerce").rename(column)
                for column in predictors
            ),
            fund_dummies,
            month_dummies,
        ],
        axis=1,
    )
    beta, se, r2 = ols(
        pd.to_numeric(data[dependent], errors="coerce").to_numpy(),
        design.to_numpy(),
        cluster=data["month"].to_numpy(),
    )
    return RegressionResult(
        name=name,
        dependent=dependent,
        n_obs=len(data),
        n_funds=int(data["fund_code"].nunique()),
        n_clusters=int(data["month"].nunique()),
        cluster_on="month",
        coefficients={
            column: float(beta[index + 1]) for index, column in enumerate(predictors)
        },
        std_errors={
            column: float(se[index + 1]) for index, column in enumerate(predictors)
        },
        r_squared=float(r2),
        absorbed=("fund_code", "month"),
        note=note,
    )


def performance_horizon_response(
    monthly: pd.DataFrame,
    horizons: tuple[int, ...] = (1, 3, 6, 12),
    *,
    winsor: float | None = None,
    exclude_funds: Iterable[str] = (),
    exclude_managers: Iterable[str] = (),
) -> dict[int, dict[str, RegressionResult]]:
    """Estimate acquisition and redemption response to lagged relative performance.

    Returns are compounded on the full clean history before inception and
    analysis filters.  Performance is ranked from 0 to 1 within month and asset
    class.  Each equation includes explicit fund and month dummies and uses
    month-clustered standard errors.  Flow coefficients are percentage points
    for a move from the bottom to the top of the contemporaneous peer rank.
    """
    base = _monthly_base(monthly)
    if base.empty:
        return {}
    return_col = "total_return" if "total_return" in base else "gross_return"
    for horizon in horizons:
        base[f"lagged_return_{horizon}m"] = _exact_lagged_compound(base, horizon, return_col)
    sample = _sample_filter(base, gross=True, sample="equity_balanced")
    sample = sample[
        ~sample["fund_code"].isin(set(exclude_funds))
        & ~sample.get("manager_id", pd.Series(index=sample.index, dtype=object)).isin(
            set(exclude_managers)
        )
    ].copy()
    sample = _winsorise(sample, _FLOW_COLUMNS, winsor)
    if winsor:
        both = sample[["gross_subscription_rate", "gross_redemption_rate"]].notna().all(axis=1)
        sample.loc[both, "net_flow_rate"] = (
            sample.loc[both, "gross_subscription_rate"]
            - sample.loc[both, "gross_redemption_rate"]
        )

    results: dict[int, dict[str, RegressionResult]] = {}
    for horizon in horizons:
        performance = f"lagged_return_{horizon}m"
        ranks = sample.groupby(["month", "asset_class"], observed=True)[performance].rank(
            method="average"
        )
        counts = sample.groupby(["month", "asset_class"], observed=True)[performance].transform(
            "count"
        )
        rank_column = f"performance_rank_{horizon}m"
        sample[rank_column] = ((ranks - 1) / (counts - 1)).where(counts > 1)
        complete = sample.dropna(subset=[rank_column, *_FLOW_COLUMNS]).copy()
        if complete.empty:
            continue
        horizon_results: dict[str, RegressionResult] = {}
        for label, measure in (
            ("subscriptions", "gross_subscription_rate"),
            ("redemptions", "gross_redemption_rate"),
            ("net", "net_flow_rate"),
        ):
            outcome = f"{measure}_percentage_points"
            complete[outcome] = complete[measure] * 100
            try:
                horizon_results[label] = _fit_dummy_fe(
                    complete,
                    outcome,
                    rank_column,
                    name=f"{label}_{horizon}m_relative_performance",
                    note=(
                        "Explicit fund and month fixed effects; month-clustered errors; "
                        "rank 0=lowest and 1=highest within month and asset class."
                    ),
                )
            except ValueError:
                continue
        if horizon_results:
            results[horizon] = horizon_results
    return results


# Sirri and Tufano (1998) split the fractional performance rank at the bottom
# and top quintiles and let the middle 60% take its own slope. Convexity is the
# finding that the top segment is steeper than the middle: investors reward
# winners harder than they punish losers.
CONVEXITY_BREAKS = (0.2, 0.8)


def performance_convexity(
    monthly: pd.DataFrame,
    horizons: tuple[int, ...] = (6, 12),
    *,
    winsor: float | None = None,
) -> pd.DataFrame:
    """Piecewise flow-performance response, estimated on each leg separately.

    The convexity literature is built on net flows, because almost no regulator
    requires the gross legs and the asymmetry has to be inferred from the shape
    of a single curve. Vietnam discloses both legs, so the same specification
    can be run three times and the question stops being what shape the net
    curve is and becomes which leg produces the shape.

    Returns one row per (horizon, dependent, segment). Read the segment slopes
    against each other rather than individually: convexity is a statement about
    their ordering, not about any one of them being nonzero.

    This panel cannot answer the question and the output should not be read as
    if it can. The median month-and-asset-class cell holds three comparable
    funds and a quarter of them hold one, so a 20% tail segment is less than a
    single fund and the fractional rank of a three-fund cell takes only the
    values 0, 0.5 and 1. The segment variables barely vary, the tail
    coefficients are estimated off almost nothing, and a large one is noise
    rather than evidence: the +7.7pp bottom-segment redemption slope at six
    months does not reappear at twelve, which is what that looks like. Sirri and
    Tufano had thousands of funds per cross-section.

    The function is kept because the first-order asymmetry is testable here even
    though the curve shape is not, and because the limit is worth recording
    rather than rediscovering. If coverage widens to more managers, revisit it.
    """
    base = _monthly_base(monthly)
    if base.empty:
        return pd.DataFrame()
    return_col = "total_return" if "total_return" in base else "gross_return"
    for horizon in horizons:
        base[f"lagged_return_{horizon}m"] = _exact_lagged_compound(base, horizon, return_col)
    sample = _sample_filter(base, gross=True, sample="equity_balanced")
    sample = _winsorise(sample, _FLOW_COLUMNS, winsor)

    low_break, high_break = CONVEXITY_BREAKS
    rows = []
    for horizon in horizons:
        performance = f"lagged_return_{horizon}m"
        ranks = sample.groupby(["month", "asset_class"], observed=True)[performance].rank(
            method="average"
        )
        counts = sample.groupby(["month", "asset_class"], observed=True)[performance].transform(
            "count"
        )
        rank = ((ranks - 1) / (counts - 1)).where(counts > 1)

        # Each segment holds only the part of the rank falling inside it, so the
        # coefficients are slopes in the same units and can be compared directly.
        sample["rank_bottom"] = rank.clip(upper=low_break)
        sample["rank_middle"] = (rank - low_break).clip(lower=0, upper=high_break - low_break)
        sample["rank_top"] = (rank - high_break).clip(lower=0)
        segments = ["rank_bottom", "rank_middle", "rank_top"]

        complete = sample.dropna(subset=[*segments, *_FLOW_COLUMNS]).copy()
        if complete.empty:
            continue
        for label, measure in (
            ("subscriptions", "gross_subscription_rate"),
            ("redemptions", "gross_redemption_rate"),
            ("net", "net_flow_rate"),
        ):
            outcome = f"{measure}_percentage_points"
            complete[outcome] = complete[measure] * 100
            try:
                fit = _fit_dummy_fe(
                    complete,
                    outcome,
                    segments,
                    name=f"{label}_{horizon}m_convexity",
                    note=(
                        "Piecewise fractional rank, breaks at 0.2 and 0.8; explicit fund "
                        "and month fixed effects; month-clustered errors."
                    ),
                )
            except ValueError:
                continue
            errors = fit.std_errors
            for segment in segments:
                rows.append(
                    {
                        "horizon_months": horizon,
                        "dependent": label,
                        "segment": segment.replace("rank_", ""),
                        "coefficient_percentage_points": fit.coefficients[segment],
                        "std_error": errors[segment],
                        "t_stat": fit.coefficients[segment] / errors[segment]
                        if errors[segment]
                        else float("nan"),
                        "n_observations": fit.n_obs,
                        "n_funds": fit.n_funds,
                        "n_months": fit.n_clusters,
                        "convexity_top_minus_middle": fit.coefficients["rank_top"]
                        - fit.coefficients["rank_middle"],
                        "note": fit.note,
                    }
                )
    return pd.DataFrame(rows)


def _vnd_flow(frame: pd.DataFrame, value_column: str, rate_column: str) -> pd.Series:
    value = (
        pd.to_numeric(frame[value_column], errors="coerce")
        if value_column in frame
        else pd.Series(np.nan, index=frame.index, dtype=float)
    )
    rate = (
        pd.to_numeric(frame[rate_column], errors="coerce")
        if rate_column in frame
        else pd.Series(np.nan, index=frame.index, dtype=float)
    )
    nav = (
        pd.to_numeric(frame["nav_begin"], errors="coerce")
        if "nav_begin" in frame
        else pd.Series(np.nan, index=frame.index, dtype=float)
    )
    fallback = rate * nav
    return value.where(value.notna(), fallback)


def book_flow_conversion(
    monthly: pd.DataFrame,
    by: Literal["fund", "manager", "asset_class"] | Sequence[str] = "fund",
    *,
    start_month: str | None = None,
    end_month: str | None = None,
) -> pd.DataFrame:
    """Report book-flow conversion, never investor-cohort retention.

    ``retained_net_per_100_subscriptions`` is aggregate net flow divided by
    aggregate subscriptions.  Redemptions can belong to older holders, so the
    measure does not say that a percentage of newly acquired customers stayed.
    """
    base = _monthly_base(monthly)
    if base.empty:
        return pd.DataFrame()
    sample = _sample_filter(base, gross=True)
    if start_month:
        sample = sample[sample["month"].ge(start_month)]
    if end_month:
        sample = sample[sample["month"].le(end_month)]
    if sample.empty:
        return pd.DataFrame()
    mapping = {"fund": ["fund_code"], "manager": ["manager_id"], "asset_class": ["asset_class"]}
    group_columns = mapping.get(by, list(by) if not isinstance(by, str) else [by])
    missing = [column for column in group_columns if column not in sample]
    if missing:
        raise ValueError(f"grouping columns missing: {missing}")
    sample["subscriptions_vnd"] = _vnd_flow(sample, "subscriptions", "gross_subscription_rate")
    sample["redemptions_vnd"] = _vnd_flow(sample, "redemptions", "gross_redemption_rate").abs()
    sample["net_flow_vnd"] = _vnd_flow(sample, "net_flow", "net_flow_rate")
    sample = sample.dropna(subset=["subscriptions_vnd", "redemptions_vnd", "net_flow_vnd"])

    rows = []
    grouper = group_columns[0] if len(group_columns) == 1 else group_columns
    for key, group in sample.groupby(grouper, observed=True, sort=True):
        key_values = (key,) if len(group_columns) == 1 else tuple(key)
        subscriptions = group["subscriptions_vnd"].sum()
        redemptions = group["redemptions_vnd"].sum()
        net = group["net_flow_vnd"].sum()
        row = dict(zip(group_columns, key_values))
        row.update(
            {
                "sample_start": group["month"].min(),
                "sample_end": group["month"].max(),
                "n_observations": len(group),
                "n_months": int(group["month"].nunique()),
                "n_funds": int(group["fund_code"].nunique()),
                "total_subscriptions_vnd": subscriptions,
                "total_redemptions_vnd": redemptions,
                "total_net_flow_vnd": net,
                "redemptions_per_100_subscriptions": 100 * redemptions / subscriptions
                if subscriptions > 0
                else np.nan,
                "retained_net_per_100_subscriptions": 100 * net / subscriptions
                if subscriptions > 0
                else np.nan,
                "metric_scope": "aggregate_book_flow_not_customer_cohort_retention",
            }
        )
        # Backward-compatible compact aliases; the long names are preferred in
        # publication because they state the denominator unambiguously.
        row["redemption_per_100_subs"] = row["redemptions_per_100_subscriptions"]
        row["retained_net_per_100_subs"] = row["retained_net_per_100_subscriptions"]
        if group_columns == ["fund_code"]:
            for metadata in ("manager_id", "asset_class", "fund_name"):
                if metadata in group:
                    values = group[metadata].dropna()
                    row[metadata] = values.iloc[-1] if not values.empty else np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def flow_persistence(
    monthly: pd.DataFrame,
    *,
    sample: Literal["all", "equity_balanced", "bond", "vinacapital"] = "all",
    winsor: float | None = None,
) -> dict[str, RegressionResult]:
    """Regress each flow leg on its exact-consecutive prior-month value."""
    base = _monthly_base(monthly)
    if base.empty:
        return {}
    for measure in _FLOW_COLUMNS:
        base[f"lag_{measure}"] = _exact_lag(base, measure)
    selected = _sample_filter(base, gross=True, sample=sample)
    results: dict[str, RegressionResult] = {}
    for label, measure in (
        ("subscriptions", "gross_subscription_rate"),
        ("redemptions", "gross_redemption_rate"),
        ("net", "net_flow_rate"),
    ):
        lag = f"lag_{measure}"
        data = selected.dropna(subset=[measure, lag]).copy()
        data = _winsorise(data, [measure, lag], winsor)
        if data.empty:
            continue
        try:
            results[label] = _fit_dummy_fe(
                data,
                measure,
                lag,
                name=f"{sample}_{label}_persistence",
                note="Exact consecutive months; explicit fund/month fixed effects.",
            )
        except ValueError:
            continue
    return results


def _regime_rows(base: pd.DataFrame) -> pd.DataFrame:
    frame = base.copy()
    frame["regime"] = pd.Series(None, index=frame.index, dtype=object)
    # Float 0/1 plus NaN avoids pandas 3's strict bool-into-float assignment.
    frame["next_negative"] = np.nan
    for _, group in frame.groupby("fund_code", observed=True, sort=False):
        indices = group.index.to_numpy()
        months = group["month_period"].astype("int64").to_numpy()
        flows = pd.to_numeric(group["net_flow_rate"], errors="coerce").to_numpy()
        clean = group["clean_month"].to_numpy(dtype=bool)
        streak = 0
        for position, index in enumerate(indices):
            consecutive = (
                position > 0
                and months[position] - months[position - 1] == 1
                and clean[position - 1]
                and clean[position]
            )
            if not clean[position] or not np.isfinite(flows[position]):
                streak = 0
                continue
            if flows[position] < 0:
                streak = streak + 1 if consecutive else 1
                frame.loc[index, "regime"] = "first_negative" if streak == 1 else "2plus_negative"
            else:
                streak = 0
                frame.loc[index, "regime"] = "nonnegative"
            if (
                position + 1 < len(indices)
                and months[position + 1] - months[position] == 1
                and clean[position + 1]
                and np.isfinite(flows[position + 1])
            ):
                frame.loc[index, "next_negative"] = float(flows[position + 1] < 0)
    return frame


def _wilson(successes: int, observations: int, z: float = 1.96) -> tuple[float, float]:
    if observations == 0:
        return np.nan, np.nan
    p = successes / observations
    denominator = 1 + z**2 / observations
    centre = (p + z**2 / (2 * observations)) / denominator
    half = z * np.sqrt(p * (1 - p) / observations + z**2 / (4 * observations**2)) / denominator
    return centre - half, centre + half


def regime_transition_table(
    monthly: pd.DataFrame,
    *,
    sample: Literal["all", "equity_balanced", "bond", "vinacapital"] = "all",
) -> pd.DataFrame:
    """Probability the next exact calendar month is negative by current streak."""
    base = _monthly_base(monthly)
    if base.empty or "net_flow_rate" not in base:
        return pd.DataFrame()
    frame = _regime_rows(base)
    selected = _sample_filter(frame, gross=False, sample=sample).dropna(
        subset=["regime", "next_negative"]
    )
    rows = []
    for regime in ("nonnegative", "first_negative", "2plus_negative"):
        group = selected[selected["regime"].eq(regime)]
        n = len(group)
        negative = int(group["next_negative"].astype(bool).sum()) if n else 0
        low, high = _wilson(negative, n)
        rows.append(
            {
                "sample": sample,
                "regime": regime,
                "n_transitions": n,
                "next_negative_count": negative,
                "next_negative_probability": negative / n if n else np.nan,
                "next_negative_ci95_low": low,
                "next_negative_ci95_high": high,
            }
        )
    result = pd.DataFrame(rows)
    # Compatibility aliases retain the first exploratory API while the longer
    # names make the event being measured explicit.
    result["next_negative_prob"] = result["next_negative_probability"]
    result["next_nonnegative_prob"] = 1.0 - result["next_negative_probability"]
    return result


def manager_rotation_upper_bound(monthly: pd.DataFrame) -> pd.DataFrame:
    """Upper-bound simultaneous sibling-fund offsets; never inferred transfers."""
    columns = [
        "manager_id",
        "month",
        "n_funds",
        "n_funds_positive",
        "n_funds_negative",
        "positive_net_flow_vnd",
        "negative_net_flow_abs_vnd",
        "sibling_offset_upper_bound_vnd",
        "rotation_upper_bound_vnd",
        "upper_bound_share_of_outflow",
        "metric_scope",
    ]
    base = _monthly_base(monthly)
    if base.empty or "manager_id" not in base:
        return pd.DataFrame(columns=columns)
    sample = _sample_filter(base, gross=False)
    sample["net_flow_vnd"] = _vnd_flow(sample, "net_flow", "net_flow_rate")
    sample = sample.dropna(subset=["net_flow_vnd"])
    rows = []
    for (manager, month), group in sample.groupby(["manager_id", "month"], observed=True):
        if group["fund_code"].nunique() < 2:
            continue
        positive = group.loc[group["net_flow_vnd"] > 0, "net_flow_vnd"].sum()
        negative = group.loc[group["net_flow_vnd"] < 0, "net_flow_vnd"].abs().sum()
        if positive <= 0 or negative <= 0:
            continue
        upper = min(positive, negative)
        rows.append(
            {
                "manager_id": manager,
                "month": month,
                "n_funds": int(group["fund_code"].nunique()),
                "n_funds_positive": int((group["net_flow_vnd"] > 0).sum()),
                "n_funds_negative": int((group["net_flow_vnd"] < 0).sum()),
                "positive_net_flow_vnd": positive,
                "negative_net_flow_abs_vnd": negative,
                "sibling_offset_upper_bound_vnd": upper,
                "rotation_upper_bound_vnd": upper,  # backward-compatible alias
                "upper_bound_share_of_outflow": upper / negative,
                "metric_scope": "upper_bound_not_observed_customer_transfers",
            }
        )
    return pd.DataFrame(rows, columns=columns)


def aggregate_segment_demand(
    monthly_demand: pd.DataFrame,
    *,
    sample: Literal["all", "equity_balanced", "bond", "vinacapital"] = "all",
) -> pd.DataFrame:
    """Create AUM-weighted monthly foreign/domestic net-demand rates."""
    if monthly_demand.empty:
        return pd.DataFrame()
    frame = monthly_demand.copy()
    if "fund_month_order" in frame:
        frame = frame[frame["fund_month_order"].gt(INCEPTION_OBSERVATIONS)]
    frame = frame[frame["fund_code"].ne("DCIP")]
    if sample == "equity_balanced":
        frame = frame[frame["asset_class"].isin(["equity", "balanced"])]
    elif sample == "bond":
        frame = frame[frame["asset_class"].eq("bond")]
    elif sample == "vinacapital":
        frame = frame[frame["manager_id"].eq("vinacapital")]
    elif sample != "all":
        raise ValueError(f"unknown sample: {sample}")
    rows = []
    for month, group in frame.groupby("month", observed=True, sort=True):
        foreign_aum = group["opening_foreign_aum_vnd"].sum(min_count=1)
        domestic_aum = group["opening_domestic_aum_vnd"].sum(min_count=1)
        foreign_demand = group["foreign_unit_demand_vnd"].sum(min_count=1)
        domestic_demand = group["domestic_unit_demand_vnd"].sum(min_count=1)
        row = {
            "sample": sample,
            "month": month,
            "n_funds": int(group["fund_code"].nunique()),
            "foreign_opening_aum_vnd": foreign_aum,
            "domestic_opening_aum_vnd": domestic_aum,
            "foreign_unit_demand_vnd": foreign_demand,
            "domestic_unit_demand_vnd": domestic_demand,
            "foreign_demand_rate": foreign_demand / foreign_aum if foreign_aum > 0 else np.nan,
            "domestic_demand_rate": domestic_demand / domestic_aum if domestic_aum > 0 else np.nan,
        }
        if "market_return" in group:
            weights = pd.to_numeric(group["opening_total_aum_vnd"], errors="coerce")
            returns = pd.to_numeric(group["market_return"], errors="coerce")
            valid = weights.gt(0) & returns.notna()
            row["local_market_return"] = (
                np.average(returns[valid], weights=weights[valid]) if valid.any() else np.nan
            )
        rows.append(row)
    return pd.DataFrame(rows)


def hac_ols(
    y: np.ndarray,
    X: np.ndarray,
    *,
    max_lags: int = 3,
) -> tuple[np.ndarray, np.ndarray, float]:
    """OLS with Bartlett-kernel Newey-West standard errors."""
    y = np.asarray(y, dtype=float)
    X = np.asarray(X, dtype=float)
    valid = np.isfinite(y) & np.isfinite(X).all(axis=1)
    y, X = y[valid], X[valid]
    n, k = X.shape
    if n <= k:
        raise ValueError(f"not enough observations: n={n}, k={k}")
    inverse = np.linalg.pinv(X.T @ X)
    beta = inverse @ X.T @ y
    residual = y - X @ beta
    scores = X * residual[:, None]
    meat = scores.T @ scores
    for lag in range(1, min(max_lags, n - 1) + 1):
        weight = 1 - lag / (max_lags + 1)
        covariance = scores[lag:].T @ scores[:-lag]
        meat += weight * (covariance + covariance.T)
    covariance = inverse @ meat @ inverse * n / (n - k)
    standard_errors = np.sqrt(np.clip(np.diag(covariance), 0, None))
    total = float(((y - y.mean()) ** 2).sum())
    residual_sum = float((residual**2).sum())
    r_squared = 1 - residual_sum / total if total > 0 else np.nan
    return beta, standard_errors, r_squared


def investor_performance_response(
    monthly: pd.DataFrame,
    monthly_demand: pd.DataFrame,
    horizons: tuple[int, ...] = (1, 3, 6, 12),
    *,
    sample: Literal["all", "equity_balanced", "bond", "vinacapital"] = "equity_balanced",
    minimum_segment_aum_vnd: float = 1_000_000_000.0,
    minimum_segment_share: float = 0.01,
    winsor: float | None = 0.01,
) -> dict[int, dict[str, RegressionResult]]:
    """Compare foreign and domestic net demand response to relative performance.

    This is a net certificate-demand comparison, not a gross-flow comparison.
    Segment observations require at least ``minimum_segment_aum_vnd`` and
    ``minimum_segment_share`` at the opening boundary so tiny denominators do
    not create extreme rates.  Return ranks are formed on the eligible product
    peer set before segment-specific denominator filters are applied.
    """
    base = _monthly_base(monthly)
    if base.empty or monthly_demand.empty:
        return {}
    return_col = "total_return" if "total_return" in base else "gross_return"
    for horizon in horizons:
        base[f"lagged_return_{horizon}m"] = _exact_lagged_compound(
            base, horizon, return_col
        )
    keep = [
        "fund_code",
        "month",
        "month_period",
        "clean_month",
        "asset_class",
        "manager_id",
        *[f"lagged_return_{horizon}m" for horizon in horizons],
    ]
    keep = [column for column in keep if column in base]
    demand = monthly_demand.merge(
        base[keep], on=["fund_code", "month"], how="left", suffixes=("", "_panel")
    )
    if "fund_month_order" in demand:
        demand = demand[demand["fund_month_order"].gt(INCEPTION_OBSERVATIONS)]
    demand = demand[demand["fund_code"].ne("DCIP")]
    if sample == "equity_balanced":
        demand = demand[demand["asset_class"].isin(["equity", "balanced"])]
    elif sample == "bond":
        demand = demand[demand["asset_class"].eq("bond")]
    elif sample == "vinacapital":
        demand = demand[demand["manager_id"].eq("vinacapital")]
    elif sample != "all":
        raise ValueError(f"unknown sample: {sample}")
    demand["opening_foreign_share"] = (
        demand["opening_foreign_aum_vnd"] / demand["opening_total_aum_vnd"]
    )

    results: dict[int, dict[str, RegressionResult]] = {}
    for horizon in horizons:
        performance = f"lagged_return_{horizon}m"
        ranks = demand.groupby(["month", "asset_class"], observed=True)[performance].rank(
            method="average"
        )
        counts = demand.groupby(["month", "asset_class"], observed=True)[performance].transform(
            "count"
        )
        rank_column = f"performance_rank_{horizon}m"
        demand[rank_column] = ((ranks - 1) / (counts - 1)).where(counts > 1)
        horizon_results: dict[str, RegressionResult] = {}
        for segment in ("foreign", "domestic"):
            outcome = f"{segment}_demand_rate"
            denominator = f"opening_{segment}_aum_vnd"
            share = (
                demand["opening_foreign_share"]
                if segment == "foreign"
                else 1 - demand["opening_foreign_share"]
            )
            data = demand[
                demand[denominator].ge(minimum_segment_aum_vnd)
                & share.ge(minimum_segment_share)
            ].dropna(subset=[outcome, rank_column]).copy()
            if data.empty:
                continue
            if winsor:
                low, high = data[outcome].quantile([winsor, 1 - winsor])
                data[outcome] = data[outcome].clip(low, high)
            data[f"{outcome}_percentage_points"] = data[outcome] * 100
            try:
                horizon_results[segment] = _fit_dummy_fe(
                    data,
                    f"{outcome}_percentage_points",
                    rank_column,
                    name=f"{sample}_{segment}_{horizon}m_relative_performance",
                    note=(
                        "Validated net certificate demand; segment opening AUM >= "
                        f"{minimum_segment_aum_vnd:,.0f} VND and share >= "
                        f"{minimum_segment_share:.1%}; explicit fund/month fixed effects."
                    ),
                )
            except ValueError:
                continue
        if horizon_results:
            results[horizon] = horizon_results
    return results


def investor_demand_persistence(
    monthly_demand: pd.DataFrame,
    *,
    sample: Literal["all", "equity_balanced", "bond", "vinacapital"] = "all",
    minimum_segment_aum_vnd: float = 1_000_000_000.0,
    minimum_segment_share: float = 0.01,
    winsor: float | None = 0.01,
) -> dict[str, RegressionResult]:
    """Estimate exact-consecutive persistence in each segment's net demand."""
    if monthly_demand.empty:
        return {}
    frame = monthly_demand.copy()
    frame["month_period"] = pd.PeriodIndex(
        pd.to_datetime(frame["month"], errors="coerce"), freq="M"
    )
    frame = frame.sort_values(["fund_code", "month_period"]).reset_index(drop=True)
    frame["month_ordinal"] = frame["month_period"].astype("int64")
    frame["opening_foreign_share"] = (
        frame["opening_foreign_aum_vnd"] / frame["opening_total_aum_vnd"]
    )
    for segment in ("foreign", "domestic"):
        outcome = f"{segment}_demand_rate"
        frame[f"lag_{outcome}"] = frame.groupby("fund_code", observed=True)[outcome].shift(1)
        previous_month = frame.groupby("fund_code", observed=True)["month_ordinal"].shift(1)
        frame.loc[
            frame["month_ordinal"].sub(previous_month).ne(1), f"lag_{outcome}"
        ] = np.nan
    if "fund_month_order" in frame:
        frame = frame[frame["fund_month_order"].gt(INCEPTION_OBSERVATIONS)]
    frame = frame[frame["fund_code"].ne("DCIP")]
    if sample == "equity_balanced":
        frame = frame[frame["asset_class"].isin(["equity", "balanced"])]
    elif sample == "bond":
        frame = frame[frame["asset_class"].eq("bond")]
    elif sample == "vinacapital":
        frame = frame[frame["manager_id"].eq("vinacapital")]
    elif sample != "all":
        raise ValueError(f"unknown sample: {sample}")

    results: dict[str, RegressionResult] = {}
    for segment in ("foreign", "domestic"):
        outcome = f"{segment}_demand_rate"
        lag = f"lag_{outcome}"
        denominator = f"opening_{segment}_aum_vnd"
        share = (
            frame["opening_foreign_share"]
            if segment == "foreign"
            else 1 - frame["opening_foreign_share"]
        )
        data = frame[
            frame[denominator].ge(minimum_segment_aum_vnd)
            & share.ge(minimum_segment_share)
        ].dropna(subset=[outcome, lag]).copy()
        data = _winsorise(data, [outcome, lag], winsor)
        if data.empty:
            continue
        try:
            results[segment] = _fit_dummy_fe(
                data,
                outcome,
                lag,
                name=f"{sample}_{segment}_net_demand_persistence",
                note="Validated net certificate demand; exact consecutive fund-months.",
            )
        except ValueError:
            continue
    return results


def investor_macro_sensitivity(
    monthly_demand: pd.DataFrame,
    macro_monthly: pd.DataFrame,
    *,
    predictor_scales: dict[str, float],
    sample: Literal["all", "equity_balanced", "bond", "vinacapital"] = "all",
    observed_only: dict[str, str] | None = None,
    include_lag1: bool = True,
    controls: tuple[str, ...] = ("trend", "local_market_return"),
    winsor: float | None = 0.01,
    hac_lags: int = 3,
) -> pd.DataFrame:
    """Exploratory HAC associations between segment demand and macro variables.

    ``predictor_scales`` maps each predictor to the economically readable change
    used for ``effect_percentage_points`` (for example, 1.0 for a one-percentage-
    point deposit-rate change, 10.0 for ten VIX points, or 0.10 for a 10% equity
    return).  Caller-supplied macro data keeps this function deterministic and
    offline.  Lag-one rows are suitable as operational triggers; contemporaneous
    rows are descriptive only.
    """
    demand = aggregate_segment_demand(monthly_demand, sample=sample)
    if demand.empty or macro_monthly.empty:
        return pd.DataFrame()
    macro = macro_monthly.copy()
    macro["month_period"] = pd.PeriodIndex(pd.to_datetime(macro["month"], errors="coerce"), freq="M")
    macro = macro.sort_values("month_period").drop_duplicates("month_period", keep="last")
    for predictor in predictor_scales:
        macro[predictor] = pd.to_numeric(macro.get(predictor), errors="coerce")
        if include_lag1:
            macro[f"{predictor}_lag1"] = macro[predictor].shift(1)
    observed_only = observed_only or {}
    for predictor, flag in observed_only.items():
        if flag in macro:
            macro[flag] = macro[flag].fillna(False).astype(bool)
            if include_lag1:
                macro[f"{flag}_lag1"] = macro[flag].shift(1).fillna(False).astype(bool)
    demand["month_period"] = pd.PeriodIndex(pd.to_datetime(demand["month"]), freq="M")
    merged = demand.merge(macro, on="month_period", how="left", suffixes=("", "_macro"))
    merged = merged.sort_values("month_period")
    merged["trend"] = np.arange(len(merged), dtype=float)
    if "local_market_return" not in merged:
        merged["local_market_return"] = np.nan

    rows = []
    timings = ("current", "lag1") if include_lag1 else ("current",)
    for segment in ("foreign", "domestic"):
        outcome = f"{segment}_demand_rate"
        for predictor, scale in predictor_scales.items():
            for timing in timings:
                predictor_column = predictor if timing == "current" else f"{predictor}_lag1"
                columns = [outcome, predictor_column, *controls]
                data = merged.dropna(subset=columns).copy()
                flag = observed_only.get(predictor)
                if flag:
                    flag_column = flag if timing == "current" else f"{flag}_lag1"
                    data = data[data[flag_column].fillna(False).astype(bool)]
                if winsor and not data.empty:
                    low, high = data[outcome].quantile([winsor, 1 - winsor])
                    data[outcome] = data[outcome].clip(low, high)
                if len(data) <= len(controls) + 2:
                    continue
                design = np.column_stack(
                    [
                        np.ones(len(data)),
                        data[predictor_column].to_numpy(dtype=float),
                        *[data[control].to_numpy(dtype=float) for control in controls],
                    ]
                )
                beta, se, r2 = hac_ols(
                    data[outcome].to_numpy(dtype=float), design, max_lags=hac_lags
                )
                effect = beta[1] * scale * 100
                effect_se = se[1] * scale * 100
                rows.append(
                    {
                        "sample": sample,
                        "segment": segment,
                        "predictor": predictor,
                        "timing": timing,
                        "predictor_change": scale,
                        "effect_percentage_points": effect,
                        "effect_std_error": effect_se,
                        "t_stat": effect / effect_se if effect_se else np.nan,
                        "n_months": len(data),
                        "n_funds_mean": data["n_funds"].mean(),
                        "sample_start": str(data["month_period"].min()),
                        "sample_end": str(data["month_period"].max()),
                        "hac_lags": hac_lags,
                        "controls": "+".join(controls) if controls else "none",
                        "r_squared": r2,
                        "interpretation": "exploratory_association_not_causal",
                    }
                )
    return pd.DataFrame(rows)
