"""Manager-facing readings of the panel: what the gross legs imply for a book.

`analysis.py` answers a research question, whether netting the two legs destroys
a flow-performance response that is really there. This module answers the
operating questions that follow once the answer turns out to be yes: which leg
does performance actually move, how fast a book leaks when nothing is done, when
in the year the leak happens, what macro condition sets the level of new money,
and whether any of it can be forecast well enough to plan on.

Every function here takes an assembled panel and returns a table. None of them
read or write files, because the point of the separation is that a reading can
be re-derived from the panel at any time and never becomes a third copy of the
data. `run.py` does the IO.

Two conventions carry over from `analysis.py` and are load-bearing:

* Flow rates are scaled by **beginning**-of-period NAV, so a month's flow is
  never inside its own denominator.
* A fund's opening periods are dropped before anything is estimated. A launch is
  not a response to performance, and pooling one with ongoing flow estimates
  neither.

One convention is new here. Some funds are not comparable to the rest on flow at
all: a cash-management vehicle turns over most of its NAV every month by design,
and left in a pooled mean it is not an outlier so much as a different question.
`turnover_outliers` finds them by behaviour rather than by name, so a vehicle
added to the panel later is caught without editing a list.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .analysis import FLOW_MEASURES, ols

__all__ = [
    "TURNOVER_THRESHOLD",
    "turnover_outliers",
    "leg_response",
    "retention_table",
    "seasonality_table",
    "macro_sensitivity",
    "forecast_backtest",
    "flow_regime",
]

log = logging.getLogger(__name__)

# A fund whose median month subscribes more than this share of its own opening
# NAV is not gathering assets, it is being used as a place to park cash. DCIP is
# the case in this sample: 67% of all bond gross subscriptions against 15% of
# bond NAV, and a median monthly subscription rate near 25%. Including it, the
# deposit-rate coefficient on bond redemptions is -0.0415 (t = -4.7); excluding
# it, the effect vanishes entirely (+0.0021, t = +0.3). The whole bond
# redemption result was one vehicle, which is why this filter is a default
# rather than an option.
TURNOVER_THRESHOLD = 0.20

# Redemption rates are read as a monthly hazard and annualised. Above this the
# arithmetic still works but the interpretation stops being useful, because a
# book turning over that fast is not losing investors so much as recycling them.
_MAX_SENSIBLE_HAZARD = 0.99


def _label_aggs(frame: pd.DataFrame) -> dict[str, tuple[str, str]]:
    """Carry the descriptive columns through a per-fund groupby, if present.

    Only columns that exist are carried. Substituting a different column under
    an expected name would put fund codes in an `asset_class` field, which reads
    as data rather than as an absence.
    """
    return {
        name: (name, "first")
        for name in ("asset_class", "manager_id", "fund_name")
        if name in frame.columns
    }


def turnover_outliers(
    panel: pd.DataFrame, threshold: float = TURNOVER_THRESHOLD
) -> list[str]:
    """Fund codes whose median monthly subscription rate exceeds `threshold`.

    Detected rather than listed, so that a cash vehicle added to the panel after
    this was written is excluded on the same evidence rather than silently
    pooled into a mean it does not belong in. The median is deliberate: a mean
    would flag any fund that had one large launch month.
    """
    if "gross_subscription_rate" not in panel.columns:
        return []
    median = panel.groupby("fund_code", observed=True)["gross_subscription_rate"].median()
    return sorted(median[median > threshold].index.astype(str))


def _exclude_turnover(panel: pd.DataFrame, exclude: bool) -> pd.DataFrame:
    if not exclude:
        return panel
    outliers = turnover_outliers(panel)
    if not outliers:
        return panel
    log.info("excluding high-turnover vehicles from the reading: %s", ", ".join(outliers))
    return panel[~panel["fund_code"].isin(outliers)]


# --------------------------------------------------------------------------
# which leg does performance move
# --------------------------------------------------------------------------


def leg_response(
    panel: pd.DataFrame,
    performance: str = "ret_lag1_6",
    n_bins: int = 5,
    exclude_turnover: bool = True,
) -> pd.DataFrame:
    """Mean flow rate per leg by past-performance bin, with the spread attached.

    This is `analysis.quintile_table` read for a different purpose, so it shares
    that function's shape and adds the row a manager actually acts on: the
    top-minus-bottom spread of each leg separately.

    A net-flow panel shows one number for that spread and invites the reading
    that performance drives flow. Split, the same sample says the subscription
    leg carries essentially all of it while the redemption leg barely moves, and
    on this sample moves the wrong way. The two readings imply opposite things
    about whether a retention problem can be fixed with performance, which is
    why the spread row is part of the table rather than left to the reader.

    `panel` must already carry the lagged performance column, which
    `analysis.add_lagged_performance` adds.
    """
    frame = _exclude_turnover(panel, exclude_turnover)
    measures = [m for m in FLOW_MEASURES if m in frame.columns]
    frame = frame.dropna(subset=[performance, *measures])
    if "gross_legs_disclosed" in frame.columns:
        frame = frame[frame["gross_legs_disclosed"].fillna(False)]
    if frame.empty or frame[performance].nunique() < n_bins:
        return pd.DataFrame()

    frame = frame.copy()
    frame["bin"] = pd.qcut(frame[performance], n_bins, labels=False, duplicates="drop") + 1
    grouped = frame.groupby("bin", observed=True)

    table = (grouped[measures].mean() * 100.0).round(4)
    table.insert(0, f"{performance} %", (grouped[performance].mean() * 100.0).round(4))
    table["n"] = grouped.size()
    table.index = table.index.astype(str)

    top, bottom = table.iloc[-1], table.iloc[0]
    spread = {c: round(top[c] - bottom[c], 4) for c in table.columns if c != "n"}
    spread["n"] = table["n"].sum()
    table.loc["top-bottom"] = spread
    return table


# --------------------------------------------------------------------------
# how fast a book leaks
# --------------------------------------------------------------------------


def retention_table(
    panel: pd.DataFrame, min_months: int = 18, exclude_turnover: bool = True
) -> pd.DataFrame:
    """Annualised attrition and implied holding half-life, per fund.

    Because redemptions barely respond to performance, a fund's redemption rate
    behaves like a structural property of its book rather than a verdict on its
    returns, and annualising it gives a number that can be compared across funds
    and managed against. The half-life is what the same hazard implies for how
    long a unit stays on the register, which is the form the number is usually
    wanted in.

    Reports geometric mean, median, and stress (worst 10th percentile) of redemption
    rates to capture the full distribution. AUM-weighted metrics scale by opening NAV.

    `organic_growth_pct` compounds both legs and answers whether the fund grows
    before any market return, which is the question the two rates exist to
    answer jointly.
    """
    frame = _exclude_turnover(panel, exclude_turnover)
    needed = {"gross_subscription_rate", "gross_redemption_rate"}
    if not needed <= set(frame.columns):
        return pd.DataFrame()
    frame = frame.dropna(subset=list(needed))
    if frame.empty:
        return pd.DataFrame()

    grouped = frame.groupby("fund_code", observed=True)
    
    # Base aggregations
    table = grouped.agg(
        **_label_aggs(frame),
        n_months=("gross_redemption_rate", "size"),
        subscription_rate_mean=("gross_subscription_rate", "mean"),
        redemption_rate_mean=("gross_redemption_rate", "mean"),
        redemption_rate_median=("gross_redemption_rate", "median"),
        redemption_rate_p90=("gross_redemption_rate", lambda x: x.quantile(0.9)),
    )
    
    # Geometric mean redemption rate
    def geomean(x):
        x_clipped = x.clip(0.0, _MAX_SENSIBLE_HAZARD)
        return float(np.exp(np.log(1.0 - x_clipped).mean()) if len(x_clipped) > 0 else np.nan)
    
    table["redemption_rate_geomean"] = grouped["gross_redemption_rate"].apply(
        lambda x: 1.0 - geomean(x)
    )
    
    # AUM-weighted metrics if nav_begin is available
    if "nav_begin" in frame.columns:
        def aum_weighted(x, weights):
            return float((x * weights).sum() / weights.sum() if weights.sum() > 0 else np.nan)
        
        table["redemption_rate_aum_wtd"] = grouped.apply(
            lambda g: aum_weighted(g["gross_redemption_rate"], g["nav_begin"])
        )
    
    table = table[table["n_months"] >= min_months]
    if table.empty:
        return table

    # Calculate attrition and half-life from geometric mean
    hazard = table["redemption_rate_geomean"].clip(0.0, _MAX_SENSIBLE_HAZARD)
    survival = 1.0 - hazard
    table["implied_aum_attrition_pct"] = (1.0 - survival**12) * 100.0
    
    with np.errstate(divide="ignore"):
        table["implied_aum_half_life_years"] = np.where(
            hazard > 0, np.log(2.0) / (-np.log(survival) * 12.0), np.inf
        )
    
    # Organic growth from mean rates
    table["organic_growth_pct"] = (
        (1.0 + table["subscription_rate_mean"] - table["redemption_rate_mean"]) ** 12 - 1.0
    ) * 100.0
    
    order = [c for c in ("asset_class", "implied_aum_attrition_pct") if c in table.columns]
    return table.sort_values(order).round(4)


# --------------------------------------------------------------------------
# when in the year it happens
# --------------------------------------------------------------------------


def seasonality_table(
    panel: pd.DataFrame, time_col: str = "period_end", exclude_turnover: bool = True
) -> pd.DataFrame:
    """Mean flow rate by calendar month, demeaned within fund.

    Demeaning within fund matters more than it looks: funds enter and leave the
    panel at different dates, so a raw calendar mean partly reports which funds
    happened to be observed in which months. Within-fund deviations ask the
    question actually intended, whether a given fund does worse in February than
    in its own average month.

    The separation by leg is the point. A month that is weak because
    subscriptions stop and a month that is weak because redemptions arrive call
    for different responses, campaign timing against liquidity buffer, and a net
    series cannot tell the two apart.
    
    Reports standard errors, leave-one-year-out range, and observation counts
    to assess robustness of seasonal patterns.
    """
    frame = _exclude_turnover(panel, exclude_turnover)
    measures = [m for m in (*FLOW_MEASURES, "churn_rate") if m in frame.columns]
    if not measures:
        return pd.DataFrame()
    frame = frame.dropna(subset=measures).copy()
    if frame.empty:
        return pd.DataFrame()

    frame["_month_of_year"] = pd.to_datetime(frame[time_col]).dt.month
    frame["_year"] = pd.to_datetime(frame[time_col]).dt.year
    demeaned = frame[measures] - frame.groupby("fund_code", observed=True)[measures].transform("mean")
    demeaned["_month_of_year"] = frame["_month_of_year"]
    demeaned["_year"] = frame["_year"]

    grouped = demeaned.groupby("_month_of_year", observed=True)
    table = (grouped[measures].mean() * 100.0).round(4)
    
    # Standard errors
    for measure in measures:
        se = grouped[measure].sem() * 100.0
        table[f"{measure}_se"] = se.round(4)
    
    # Leave-one-year-out range
    years = sorted(frame["_year"].unique())
    if len(years) > 1:
        for measure in measures:
            loo_means = []
            for leave_out_year in years:
                subset = demeaned[demeaned["_year"] != leave_out_year]
                loo_mean = subset.groupby("_month_of_year", observed=True)[measure].mean() * 100.0
                loo_means.append(loo_mean)
            loo_df = pd.DataFrame(loo_means)
            table[f"{measure}_loo_range"] = (loo_df.max() - loo_df.min()).round(4)
    
    table["n"] = grouped.size()
    table.index = pd.Index(
        [pd.Timestamp(2000, m, 1).strftime("%b") for m in table.index], name="month"
    )
    return table


# --------------------------------------------------------------------------
# what sets the level of new money
# --------------------------------------------------------------------------


def macro_sensitivity(
    panel: pd.DataFrame,
    dependent: str = "gross_subscription_rate",
    driver: str = "deposit_rate_pct",
    control_sets: tuple[tuple[str, ...], ...] | None = None,
    cluster_on: str = "month",
    exclude_turnover: bool = True,
) -> pd.DataFrame:
    """One coefficient, re-estimated against progressively harder controls.

    A single specification cannot establish that the deposit rate moves fund
    subscriptions, because in this sample the rate spiked while the equity
    market crashed and a rate coefficient will happily absorb the crash. What
    can be shown is whether the coefficient survives being given every chance to
    disappear, so this returns the whole ladder rather than the row that reads
    best.

    Read the last row, not the first. The controls are cumulative and the final
    specification is the defensible one; the earlier rows are there to show how
    much of the raw effect was the confound.

    The driver is a macro series shared by every fund, so it is identified off
    the time dimension alone. Clustering by month prices the cross-sectional
    correlation but not the serial correlation in a regressor this persistent,
    and the panel holds roughly three genuine regime changes. Treat the sign as
    the finding and the t-statistics as optimistic.
    """
    if control_sets is None:
        control_sets = (
            (),
            ("ret_lag1_6",),
            ("ret_lag1_6", "market_return"),
            ("ret_lag1_6", "market_return", "time_trend"),
            ("ret_lag1_6", "market_return", "time_trend", "log_nav_begin"),
        )

    frame = _exclude_turnover(panel, exclude_turnover).copy()
    if driver not in frame.columns or dependent not in frame.columns:
        return pd.DataFrame()
    if "gross_legs_disclosed" in frame.columns:
        frame = frame[frame["gross_legs_disclosed"].fillna(False)]

    if "time_trend" not in frame.columns and "period_end" in frame.columns:
        stamps = pd.to_datetime(frame["period_end"])
        frame["time_trend"] = (stamps - stamps.min()).dt.days / 365.25
    if "log_nav_begin" not in frame.columns and "nav_begin" in frame.columns:
        frame["log_nav_begin"] = np.log(frame["nav_begin"].where(frame["nav_begin"] > 0))

    rows = []
    for controls in control_sets:
        regressors = [driver, *[c for c in controls if c in frame.columns]]
        sample = frame.dropna(subset=[dependent, *regressors, cluster_on])
        if len(sample) <= len(regressors) + 1:
            continue
        columns = [dependent, *regressors]
        sample = sample.copy()
        sample[columns] = sample[columns] - sample.groupby("fund_code", observed=True)[
            columns
        ].transform("mean")
        beta, std_err, r_squared = ols(
            sample[dependent].to_numpy(),
            sample[regressors].to_numpy(),
            cluster=sample[cluster_on].astype(str).to_numpy(),
        )
        rows.append(
            {
                "controls": ", ".join(controls) if controls else "none",
                "coef": beta[0],
                "std_err": std_err[0],
                "t": beta[0] / std_err[0] if std_err[0] else float("nan"),
                "n_obs": len(sample),
                "n_clusters": sample[cluster_on].nunique(),
                "within_r2": r_squared,
            }
        )
    return pd.DataFrame(rows).round(6)


# --------------------------------------------------------------------------
# can any of it be forecast
# --------------------------------------------------------------------------


def _expanding_forecast(
    frame: pd.DataFrame,
    dependent: str,
    regressors: list[str],
    time_col: str,
    min_train_periods: int,
    min_train_rows: int,
) -> pd.DataFrame:
    """One walk-forward pass. Everything is fit on strictly earlier periods."""
    periods = sorted(frame[time_col].unique())
    out = []
    for index, period in enumerate(periods):
        if index < min_train_periods:
            continue
        train = frame[frame[time_col] < period]
        test = frame[frame[time_col] == period]
        if len(train) < min_train_rows or test.empty:
            continue

        columns = [dependent, *regressors]
        # Fund effects come from training rows only. Demeaning on the pooled
        # frame would leak the test period's own level into its prediction and
        # is the single easiest way to manufacture out-of-sample skill.
        fund_means = train.groupby("fund_code", observed=True)[columns].mean()
        grand_mean = train[columns].mean()

        def centre(part: pd.DataFrame) -> np.ndarray:
            offsets = part[["fund_code"]].join(fund_means, on="fund_code")
            offsets = offsets[columns].fillna(grand_mean)
            return part[columns].to_numpy() - offsets.to_numpy()

        centred_train, centred_test = centre(train), centre(test)
        
        # Handle case with no regressors (just mean forecast)
        if len(regressors) == 0:
            level = (
                fund_means[dependent]
                .reindex(test["fund_code"])
                .fillna(grand_mean[dependent])
                .to_numpy()
            )
            predicted = level
        else:
            design = centred_train[:, 1:]
            # Check for empty or degenerate design matrix
            if design.size == 0 or design.shape[1] == 0:
                level = (
                    fund_means[dependent]
                    .reindex(test["fund_code"])
                    .fillna(grand_mean[dependent])
                    .to_numpy()
                )
                predicted = level
            else:
                try:
                    beta = np.linalg.pinv(design.T @ design) @ (design.T @ centred_train[:, 0])
                    level = (
                        fund_means[dependent]
                        .reindex(test["fund_code"])
                        .fillna(grand_mean[dependent])
                        .to_numpy()
                    )
                    predicted = centred_test[:, 1:] @ beta + level
                except np.linalg.LinAlgError:
                    # Fall back to mean forecast if SVD fails
                    level = (
                        fund_means[dependent]
                        .reindex(test["fund_code"])
                        .fillna(grand_mean[dependent])
                        .to_numpy()
                    )
                    predicted = level
        
        out.append(
            pd.DataFrame(
                {
                    time_col: period,
                    "fund_code": test["fund_code"].to_numpy(),
                    "actual": test[dependent].to_numpy(),
                    "predicted": predicted,
                }
            )
        )
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def forecast_backtest(
    panel: pd.DataFrame,
    targets: tuple[str, ...] = ("gross_subscription_rate", "gross_redemption_rate"),
    momentum_suffix: str = "_trail3",
    macro: tuple[str, ...] = ("ret_lag1_6", "market_return", "deposit_rate_pct"),
    time_col: str = "month",
    min_train_periods: int = 24,
    min_train_rows: int = 50,
    exclude_turnover: bool = True,
) -> pd.DataFrame:
    """Does market data beat extrapolating the fund's own recent flow?

    The comparison is the whole point. An in-sample regression on macro
    variables reports a large R-squared and invites a forecasting model to be
    built on it, so the baseline is deliberately cheap: the fund's own trailing
    three-month average flow, which costs nothing and is available to anyone.

    Both models are estimated walk-forward on an expanding window with fund
    effects taken from training rows only, so neither sees its own period. A
    macro model that cannot beat a moving average under those conditions should
    not be run in production, whatever its in-sample fit.

    Returns one row per target with the out-of-sample R-squared, RMSE, MAE, and
    observation counts. Multiple baseline models are included: historical mean,
    last period, trailing 3-month average, seasonal (same month last year),
    performance-based, and lagged market/macro. All features are strictly prior-known.
    R-squared is computed against the realised variance of the evaluation
    sample, so a negative value means the model is worse than predicting that
    sample's mean.
    """
    frame = _exclude_turnover(panel, exclude_turnover).copy()
    if time_col not in frame.columns:
        return pd.DataFrame()
    frame = frame.sort_values(["fund_code", time_col])

    # Construct strictly prior-known features
    for target in targets:
        if target in frame.columns:
            grouped = frame.groupby("fund_code", observed=True)[target]
            # Trailing 3-month average
            frame[target + momentum_suffix] = grouped.transform(
                lambda s: s.shift(1).rolling(3, min_periods=1).mean()
            )
            # Last period
            frame[target + "_last"] = grouped.shift(1)
            # Seasonal (same month last year)
            frame[target + "_seasonal"] = grouped.shift(12)

    # Ensure macro features are lagged by 1 period to avoid contemporaneous bridging
    lagged_macro = []
    for m in macro:
        if m in frame.columns:
            lagged_col = m + "_lag1"
            if lagged_col not in frame.columns:
                frame[lagged_col] = frame.groupby("fund_code", observed=True)[m].shift(1)
            lagged_macro.append(lagged_col)

    rows = []
    for target in targets:
        momentum = target + momentum_suffix
        last_val = target + "_last"
        seasonal_val = target + "_seasonal"
        
        if target not in frame.columns or momentum not in frame.columns:
            continue
        
        available = [c for c in lagged_macro if c in frame.columns]
        sample = frame.dropna(subset=[target, momentum, *available])
        if sample.empty:
            continue

        # Define all baseline and enhanced models
        model_specs = [
            ("historical_mean", []),  # Will use training mean
            ("last", [last_val]),
            ("trailing3", [momentum]),
            ("seasonal", [seasonal_val]),
            ("performance", ["ret_lag1_6"]),
            ("lagged_market", ["market_return_lag1"]),
            ("lagged_macro", available),
            ("momentum_only", [momentum]),
            ("plus_macro", [momentum, *available]),
        ]
        
        results = {}
        for label, regressors in model_specs:
            # Filter regressors that exist
            valid_regressors = [r for r in regressors if r in sample.columns]
            if label == "historical_mean":
                # Special case: just use training mean
                results[label] = _expanding_forecast_mean(
                    sample, target, time_col, min_train_periods, min_train_rows
                )
            elif valid_regressors or label == "historical_mean":
                results[label] = _expanding_forecast(
                    sample, target, valid_regressors if valid_regressors else [],
                    time_col, min_train_periods, min_train_rows
                )
        
        if any(r.empty for r in results.values() if isinstance(r, pd.DataFrame)):
            continue

        row = {"target": target}
        for label, predictions in results.items():
            if isinstance(predictions, pd.DataFrame) and not predictions.empty:
                actual = predictions["actual"].to_numpy()
                predicted = predictions["predicted"].to_numpy()
                variance = float(((actual - actual.mean()) ** 2).mean())
                mse = float(((actual - predicted) ** 2).mean())
                mae = float(np.abs(actual - predicted).mean())
                row[f"{label}_r2_oos"] = 1.0 - mse / variance if variance > 0 else float("nan")
                row[f"{label}_rmse"] = float(np.sqrt(mse))
                row[f"{label}_mae"] = mae
                if label == "momentum_only":
                    row["n_obs"] = len(predictions)
                    row["n_periods"] = predictions[time_col].nunique()
        
        # Compute macro gain
        if "plus_macro_r2_oos" in row and "momentum_only_r2_oos" in row:
            row["macro_gain"] = row["plus_macro_r2_oos"] - row["momentum_only_r2_oos"]
        
        rows.append(row)

    return pd.DataFrame(rows).round(4)


def _expanding_forecast_mean(
    frame: pd.DataFrame,
    dependent: str,
    time_col: str,
    min_train_periods: int,
    min_train_rows: int,
) -> pd.DataFrame:
    """Baseline forecast using only historical mean."""
    periods = sorted(frame[time_col].unique())
    out = []
    for index, period in enumerate(periods):
        if index < min_train_periods:
            continue
        train = frame[frame[time_col] < period]
        test = frame[frame[time_col] == period]
        if len(train) < min_train_rows or test.empty:
            continue

        # Fund-specific means, fallback to grand mean
        fund_means = train.groupby("fund_code", observed=True)[dependent].mean()
        grand_mean = train[dependent].mean()
        
        predicted = (
            fund_means.reindex(test["fund_code"])
            .fillna(grand_mean)
            .to_numpy()
        )
        
        out.append(
            pd.DataFrame(
                {
                    time_col: period,
                    "fund_code": test["fund_code"].to_numpy(),
                    "actual": test[dependent].to_numpy(),
                    "predicted": predicted,
                }
            )
        )
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


# --------------------------------------------------------------------------
# where the book stands now
# --------------------------------------------------------------------------


def flow_regime(
    panel: pd.DataFrame,
    since: str | pd.Period | None = None,
    months: int = 10,
    time_col: str = "month",
) -> pd.DataFrame:
    """Net flow over a recent window, per fund, scaled by closing NAV.

    Deliberately not filtered for turnover outliers and not winsorised. Every
    other function here is trying to estimate a relationship and wants a clean
    sample; this one is trying to report what happened, and a vehicle that
    redeemed twice its NAV is exactly the thing a manager needs to see rather
    than the thing to exclude.

    `cumulative_net_flow_pct_nav` is scaled by closing NAV so that it stays readable when
    the flow is a multiple of the fund, and it is a description rather than a
    rate: the flow rates elsewhere use opening NAV per period, and the two are
    not comparable. Uses sum(min_count=1) to handle missing values properly.
    """
    if time_col not in panel.columns:
        return pd.DataFrame()
    frame = panel.copy()
    periods = pd.PeriodIndex(frame[time_col].astype(str), freq="M")
    frame = frame.assign(_period=periods)

    if since is None:
        cutoff = periods.max() - (months - 1)
    else:
        cutoff = pd.Period(str(since), freq="M")
    frame = frame[frame["_period"] >= cutoff]
    if frame.empty:
        return pd.DataFrame()

    grouped = frame.sort_values("_period").groupby("fund_code", observed=True)
    table = grouped.agg(
        **_label_aggs(frame),
        n_months=("_period", "nunique"),
        subscriptions=("subscriptions", lambda x: x.sum(min_count=1)),
        redemptions=("redemptions", lambda x: x.sum(min_count=1)),
        cumulative_net_flow=("net_flow", lambda x: x.sum(min_count=1)),
        nav_end=("nav_end", "last"),
    )
    table["cumulative_net_flow_pct_nav"] = table["cumulative_net_flow"] / table["nav_end"] * 100.0
    # A fund negative in every observed month is a different signal from one
    # with a single bad month of the same size, and only the panel can say which.
    negatives = frame[frame["net_flow"] < 0].groupby("fund_code", observed=True)["_period"].nunique()
    table["months_negative"] = negatives.reindex(table.index).fillna(0).astype(int)
    table["window_from"] = str(cutoff)
    return table.sort_values("cumulative_net_flow").round(4)
