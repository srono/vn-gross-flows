"""Flow-performance specifications, run paired on net and on gross flows.

The point of this module is one comparison. The conventional flow-performance
regression is estimated on *net* flow, because net flow is all most fund
databases carry. Net flow is the difference between two decisions taken by
different people: subscriptions are new and adding investors, redemptions are
existing holders. If the two legs respond to performance differently, netting
them destroys the response, and a net-flow study reports a null where both sides
in fact moved.

Vietnamese funds disclose both legs, so the comparison can be run directly:
`paired_specifications` estimates the same equation, on the same sample, with
the same controls, three times over, with net flow, gross subscriptions and
gross redemptions as the dependent variable in turn. The three results are meant
to be read side by side; individually none of them is the finding.

The OLS engine here is deliberately small and explicit rather than a dependency.
`ols` is a plain least-squares fit with cluster-robust standard errors, checked
in the tests against a closed-form case, against numpy's own solver, and against
the identity that one-observation clusters reproduce the heteroskedasticity-
robust HC0 covariance.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

__all__ = [
    "RegressionResult",
    "ols",
    "prepare_sample",
    "add_lagged_performance",
    "performance_rank_splines",
    "flow_performance",
    "paired_specifications",
    "netting_loss",
    "quintile_table",
]

log = logging.getLogger(__name__)

# The dependent variables of the paired comparison, in reading order.
FLOW_MEASURES = (
    "net_flow_rate",
    "gross_subscription_rate",
    "gross_redemption_rate",
)

DEFAULT_CONTROLS = ("market_return", "log_nav_begin", "period_days")

# A fund's opening weeks are not ongoing flow. SSIAM's VLGF took in VND 2.15
# trillion against an opening NAV of VND 164 billion in the week to 2022-04-06,
# a subscription rate of 1,314%. That filing is correct and its identity closes;
# it is simply a launch, not a response to performance. Left in the sample it
# alone drove the net-to-churn correlation from 0.68 to 0.9955 and the
# subscription-to-redemption volatility ratio from 1.8 to 14.5.
#
# The panel keeps such rows, because they happened. The analysis sample drops
# them, because pooling a launch with ongoing flows estimates neither.
INCEPTION_PERIODS = 4
WINSOR_QUANTILE = 0.01


@dataclass
class RegressionResult:
    """A fitted specification, with everything needed to report it honestly."""

    name: str
    dependent: str
    n_obs: int
    n_funds: int
    n_clusters: int
    cluster_on: str | None
    coefficients: dict[str, float] = field(default_factory=dict)
    std_errors: dict[str, float] = field(default_factory=dict)
    r_squared: float = float("nan")
    absorbed: tuple[str, ...] = ()
    note: str = ""

    def t_stats(self) -> dict[str, float]:
        return {
            k: (self.coefficients[k] / self.std_errors[k])
            if self.std_errors.get(k)
            else float("nan")
            for k in self.coefficients
        }

    def as_frame(self) -> pd.DataFrame:
        t = self.t_stats()
        return pd.DataFrame(
            {
                "term": list(self.coefficients),
                "coef": [self.coefficients[k] for k in self.coefficients],
                "std_err": [self.std_errors.get(k, float("nan")) for k in self.coefficients],
                "t": [t[k] for k in self.coefficients],
            }
        )

    def summary(self) -> str:
        lines = [
            f"{self.name}  [dependent: {self.dependent}]",
            f"  n={self.n_obs}  funds={self.n_funds}  "
            f"clusters={self.n_clusters} on {self.cluster_on or 'none'}  "
            f"R2(within)={self.r_squared:.4f}",
        ]
        if self.absorbed:
            lines.append(f"  absorbed: {', '.join(self.absorbed)}")
        t = self.t_stats()
        for term in self.coefficients:
            lines.append(
                f"    {term:<28} {self.coefficients[term]:>12.6f}"
                f"  ({self.std_errors.get(term, float('nan')):.6f})"
                f"  t={t[term]:>7.2f}"
            )
        if self.note:
            lines.append(f"  note: {self.note}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# the estimator
# --------------------------------------------------------------------------


def ols(
    y: np.ndarray,
    X: np.ndarray,
    cluster: np.ndarray | None = None,
    dof_correction: bool = True,
) -> tuple[np.ndarray, np.ndarray, float]:
    """Least squares with cluster-robust standard errors.

    Returns (coefficients, standard errors, R-squared). `X` must already carry
    an intercept column if one is wanted; fixed effects are expected to have
    been absorbed by demeaning rather than dummied out.

    With `cluster=None` the errors are heteroskedasticity-robust (HC0). With one
    observation per cluster the sandwich reduces to HC0 exactly, which is what
    the tests assert.
    """
    y = np.asarray(y, dtype=float)
    X = np.asarray(X, dtype=float)
    n, k = X.shape
    if n <= k:
        raise ValueError(f"not enough observations: n={n}, k={k}")

    xtx = X.T @ X
    xtx_inv = np.linalg.pinv(xtx)
    beta = xtx_inv @ (X.T @ y)
    resid = y - X @ beta

    if cluster is None:
        meat = (X * resid[:, None]).T @ (X * resid[:, None])
        scale = n / (n - k) if dof_correction else 1.0
        n_clusters = n
    else:
        cluster = np.asarray(cluster)
        codes = pd.factorize(cluster)[0]
        n_clusters = int(codes.max()) + 1
        meat = np.zeros((k, k))
        for g in range(n_clusters):
            mask = codes == g
            xg_ug = X[mask].T @ resid[mask]
            meat += np.outer(xg_ug, xg_ug)
        scale = (
            (n_clusters / max(n_clusters - 1, 1)) * ((n - 1) / (n - k))
            if dof_correction
            else 1.0
        )

    cov = xtx_inv @ meat @ xtx_inv * scale
    se = np.sqrt(np.clip(np.diag(cov), 0.0, None))

    tss = float(((y - y.mean()) ** 2).sum())
    rss = float((resid**2).sum())
    r2 = 1.0 - rss / tss if tss > 0 else float("nan")
    return beta, se, r2


def _demean(frame: pd.DataFrame, columns: list[str], by: str) -> pd.DataFrame:
    """Absorb a fixed effect by within-group demeaning."""
    out = frame.copy()
    out[columns] = out[columns] - out.groupby(by, observed=True)[columns].transform("mean")
    return out


# --------------------------------------------------------------------------
# variable construction
# --------------------------------------------------------------------------


def prepare_sample(
    panel: pd.DataFrame,
    inception_periods: int = INCEPTION_PERIODS,
    winsor: float | None = WINSOR_QUANTILE,
    time_col: str = "period_end",
) -> tuple[pd.DataFrame, dict]:
    """Apply the two standard sample filters, and report exactly what they did.

    Both are conventional in the flow-performance literature and both are here
    for a reason that was measured rather than assumed. Neither silently edits
    the panel: this returns a new frame plus a record of every exclusion, in the
    same spirit as the quarantine.
    """
    frame = panel.sort_values(["fund_code", time_col]).copy()
    record: dict = {"n_input": len(frame)}

    if inception_periods:
        rank = frame.groupby("fund_code", observed=True).cumcount()
        dropped = frame[rank < inception_periods]
        frame = frame[rank >= inception_periods]
        record["inception_periods"] = inception_periods
        record["n_dropped_inception"] = len(dropped)
        record["funds_affected"] = int(dropped["fund_code"].nunique())

    if winsor:
        measures = [m for m in (*FLOW_MEASURES, "churn_rate") if m in frame.columns]
        touched = 0
        for measure in measures:
            low = frame[measure].quantile(winsor)
            high = frame[measure].quantile(1 - winsor)
            touched += int(((frame[measure] < low) | (frame[measure] > high)).sum())
            frame[measure] = frame[measure].clip(low, high)
        # Winsorising the three measures independently would break the
        # accounting identity between them, leaving net flow no longer equal to
        # subscriptions minus redemptions on any clipped row. Since the headline
        # comparison rests on net being exactly that difference, rebuild it from
        # the clipped legs wherever both are disclosed.
        legs = {"gross_subscription_rate", "gross_redemption_rate"}
        if legs <= set(frame.columns) and "net_flow_rate" in frame.columns:
            both = frame["gross_subscription_rate"].notna() & frame["gross_redemption_rate"].notna()
            frame.loc[both, "net_flow_rate"] = (
                frame.loc[both, "gross_subscription_rate"]
                - frame.loc[both, "gross_redemption_rate"]
            )
            record["n_net_rebuilt_from_legs"] = int(both.sum())

        record["winsor_quantile"] = winsor
        record["n_values_winsorised"] = touched

    record["n_output"] = len(frame)
    return frame, record


def add_lagged_performance(
    panel: pd.DataFrame,
    lags: int = 1,
    windows: tuple[int, ...] = (1, 4, 12),
    time_col: str = "period_end",
) -> pd.DataFrame:
    """Add lagged own-return and lagged excess-return windows, per fund.

    Every window is shifted by at least one period, so no contemporaneous return
    enters a regressor. A flow and the return it is supposed to be responding to
    must not share a period, or the estimate picks up the mechanical effect of
    the flow itself on that period's NAV.
    """
    out = panel.sort_values(["fund_code", time_col]).copy()
    grouped = out.groupby("fund_code", observed=True)

    for window in windows:
        out[f"ret_lag{lags}_{window}"] = grouped["gross_return"].transform(
            lambda s, w=window: s.shift(lags).rolling(w).sum()
        )
        if "excess_return" in out.columns:
            out[f"exc_lag{lags}_{window}"] = grouped["excess_return"].transform(
                lambda s, w=window: s.shift(lags).rolling(w).sum()
            )

    if "nav_begin" in out.columns:
        out["log_nav_begin"] = np.log(out["nav_begin"].where(out["nav_begin"] > 0))
    # Optional: only present once the flow measures have been derived, so this
    # function stays usable on a frame that carries returns but not yet flows.
    if "net_flow_rate" in out.columns:
        out["flow_lag1"] = grouped["net_flow_rate"].shift(1)
    return out


def performance_rank_splines(
    panel: pd.DataFrame,
    performance: str,
    time_col: str = "period_end",
    breaks: tuple[float, float] = (0.2, 0.8),
) -> pd.DataFrame:
    """Piecewise-linear performance rank, the standard convexity specification.

    Funds are ranked into [0, 1] within each period and the rank split into a
    bottom, middle and top segment. Separate slopes on the segments are what
    "convex flow-performance relationship" means: the question is whether the
    top-segment slope exceeds the bottom-segment slope.

    Carried here because net flow cannot say whether any convexity comes from
    inflows accelerating or from redemptions failing to punish. Estimated on each
    gross leg, it can.
    """
    low, high = breaks
    out = panel.copy()
    rank = out.groupby(time_col, observed=True)[performance].rank(pct=True)
    out["perf_rank"] = rank
    out["rank_low"] = rank.clip(upper=low)
    out["rank_mid"] = (rank - low).clip(lower=0.0, upper=high - low)
    out["rank_high"] = (rank - high).clip(lower=0.0)
    return out


# --------------------------------------------------------------------------
# specifications
# --------------------------------------------------------------------------


def flow_performance(
    panel: pd.DataFrame,
    dependent: str,
    regressors: list[str],
    fund_effects: bool = True,
    cluster_on: str | None = "month",
    name: str | None = None,
    time_col: str = "period_end",
) -> RegressionResult:
    """Estimate one flow-performance specification.

    Fund fixed effects are absorbed by demeaning rather than dummied, so the
    reported R-squared is a within-R-squared. Clustering defaults to the time
    dimension because the fund dimension is far too short to cluster on: with a
    handful of funds, fund-clustered standard errors are not reliable, and
    pretending otherwise would be the single easiest way to overstate this
    panel's precision.
    """
    needed = [dependent, *regressors]
    frame = panel.dropna(subset=needed).copy()
    if frame.empty:
        raise ValueError(f"no complete cases for {dependent} on {regressors}")

    if cluster_on == "month" and "month" not in frame.columns:
        frame["month"] = pd.PeriodIndex(pd.to_datetime(frame[time_col]), freq="M").astype(str)

    absorbed: tuple[str, ...] = ()
    if fund_effects:
        frame = _demean(frame, needed, by="fund_code")
        absorbed = ("fund_code",)
        design = frame[regressors].to_numpy()
        terms = list(regressors)
    else:
        design = np.column_stack(
            [np.ones(len(frame)), frame[regressors].to_numpy()]
        )
        terms = ["const", *regressors]

    cluster = frame[cluster_on].to_numpy() if cluster_on else None
    beta, se, r2 = ols(frame[dependent].to_numpy(), design, cluster=cluster)

    return RegressionResult(
        name=name or f"{dependent} ~ {' + '.join(regressors)}",
        dependent=dependent,
        n_obs=len(frame),
        n_funds=int(panel.loc[frame.index, "fund_code"].nunique())
        if "fund_code" in panel.columns
        else 0,
        n_clusters=int(pd.factorize(cluster)[0].max()) + 1 if cluster is not None else len(frame),
        cluster_on=cluster_on,
        coefficients=dict(zip(terms, beta)),
        std_errors=dict(zip(terms, se)),
        r_squared=r2,
        absorbed=absorbed,
    )


def paired_specifications(
    panel: pd.DataFrame,
    performance: str = "ret_lag1_4",
    controls: tuple[str, ...] = DEFAULT_CONTROLS,
    fund_effects: bool = True,
    cluster_on: str | None = "month",
    gross_only: bool = True,
) -> dict[str, RegressionResult]:
    """The core comparison: the same equation on net, subscriptions, redemptions.

    All three are estimated on **one** sample so the coefficients are directly
    comparable. When `gross_only` is set, that sample is restricted to rows whose
    manager discloses the gross legs, because otherwise the net regression would
    quietly run on more data than the gross ones and the comparison would be
    between different samples rather than between different dependent variables.
    """
    frame = panel.copy()
    if gross_only and "gross_legs_disclosed" in frame.columns:
        frame = frame[frame["gross_legs_disclosed"].fillna(False)]

    regressors = [performance, *[c for c in controls if c in frame.columns]]
    frame = frame.dropna(subset=[*FLOW_MEASURES, *regressors])

    results: dict[str, RegressionResult] = {}
    for measure in FLOW_MEASURES:
        results[measure] = flow_performance(
            frame,
            dependent=measure,
            regressors=regressors,
            fund_effects=fund_effects,
            cluster_on=cluster_on,
            name=f"{measure} on {performance}",
        )
    return results


def compare(results: dict[str, RegressionResult], term: str) -> pd.DataFrame:
    """Line up one coefficient across the paired specifications."""
    rows = []
    for measure, result in results.items():
        t = result.t_stats()
        rows.append(
            {
                "dependent": measure,
                "coef": result.coefficients.get(term),
                "std_err": result.std_errors.get(term),
                "t": t.get(term),
                "n": result.n_obs,
            }
        )
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# how much netting hides
# --------------------------------------------------------------------------


def netting_loss(panel: pd.DataFrame, quiet_threshold: float = 0.001) -> dict:
    """Quantify the activity a net-flow series cannot see.

    A period whose net flow is near zero can still have had large gross activity
    on both sides. That is the information a net series discards, and it is
    measurable directly rather than argued.
    """
    frame = panel.dropna(subset=["net_flow_rate", "churn_rate"])
    if "gross_legs_disclosed" in frame.columns:
        frame = frame[frame["gross_legs_disclosed"].fillna(False)]
    if frame.empty:
        return {}

    quiet = frame[frame["net_flow_rate"].abs() < quiet_threshold]
    return {
        "n_obs": len(frame),
        "quiet_threshold": quiet_threshold,
        "n_quiet_periods": len(quiet),
        "share_quiet": len(quiet) / len(frame),
        "mean_churn_when_quiet": float(quiet["churn_rate"].mean()) if len(quiet) else float("nan"),
        "max_churn_when_quiet": float(quiet["churn_rate"].max()) if len(quiet) else float("nan"),
        "corr_net_churn": float(frame["net_flow_rate"].corr(frame["churn_rate"])),
        "mean_gross_subscription_rate": float(frame["gross_subscription_rate"].mean()),
        "mean_gross_redemption_rate": float(frame["gross_redemption_rate"].mean()),
        "sd_ratio_subs_over_reds": float(
            frame["gross_subscription_rate"].std() / frame["gross_redemption_rate"].std()
        ),
    }


def quintile_table(
    panel: pd.DataFrame, performance: str = "ret_lag1_4", n_bins: int = 5
) -> pd.DataFrame:
    """Mean flow rates by past-performance bin, the non-parametric first look.

    A regression imposes a functional form. This does not, which is why it is
    worth reading first: a non-monotonic pattern here is a warning that the
    linear coefficient below it is summarising something it should not.
    """
    frame = panel.dropna(subset=[performance, *FLOW_MEASURES])
    if "gross_legs_disclosed" in frame.columns:
        frame = frame[frame["gross_legs_disclosed"].fillna(False)]
    if frame.empty:
        return pd.DataFrame()

    frame = frame.copy()
    frame["bin"] = pd.qcut(frame[performance], n_bins, labels=False, duplicates="drop") + 1
    table = frame.groupby("bin", observed=True)[list(FLOW_MEASURES)].mean() * 100.0
    table["n"] = frame.groupby("bin", observed=True).size()
    table.columns = [*(f"{c} %" for c in FLOW_MEASURES), "n"]
    return table.round(4)
