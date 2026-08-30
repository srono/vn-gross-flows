"""Do the outside options compete with the deposit rate, or explain it away?

The deposit-rate coefficient in insights.macro_sensitivity is estimated with no
other outside option in the model. Vietnamese households can also hold gold or
dollars, and those move with the deposit rate, so a lone rate coefficient is
carrying whatever it correlates with. Three passes:

  1. Each outside option alone, through the same control ladder, so the numbers
     are comparable with the existing table rather than a parallel method.
  2. All of them jointly, which is the question that matters: does the deposit
     rate survive being made to compete?
  3. Exact Bayesian model averaging over every subset. With k predictors there
     are 2**k models and a conjugate normal-inverse-gamma prior gives each one a
     closed-form marginal likelihood, so no sampler is needed. The output is a
     posterior inclusion probability per variable, which is the honest answer to
     "we tested six things on 46 months" that a table of six t-stats is not.

Every variable tested is reported. Nulls included.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from vngross.analysis import ols

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "interim" / "global_macro"
OUT = ROOT / "data" / "output" / "growth_research"

# A fund whose median month subscribes more than this share of its own NAV is
# parking cash, not gathering assets. Same threshold and reason as insights.py.
TURNOVER_THRESHOLD = 0.20

DRIVERS = {
    "deposit_rate_pct": "Vietnam 12m deposit rate, level (pp)",
    "gold_return": "World gold, monthly log return",
    "usdvnd_return": "USD/VND, monthly log return",
}
CONTROLS = ("ret_lag1_6", "market_return", "time_trend", "log_nav_begin")


def _load_panel() -> pd.DataFrame:
    frame = pd.read_csv(ROOT / "data" / "output" / "vngross_fund_month.csv")
    frame = frame[frame["gross_legs_disclosed"].fillna(False)]

    # Drop the cash-parking vehicles, matching insights._exclude_turnover.
    median = frame.groupby("fund_code")["gross_subscription_rate"].median()
    frame = frame[frame["fund_code"].map(median).lt(TURNOVER_THRESHOLD)]

    for symbol, column in (("GC_F", "gold"), ("VND_X", "usdvnd")):
        series = pd.read_csv(CACHE / f"{symbol}.csv")
        series[f"{column}_return"] = np.log(series["close"]).diff()
        frame = frame.merge(
            series[["month", f"{column}_return"]], on="month", how="left"
        )

    stamps = pd.to_datetime(frame["period_end"])
    frame["time_trend"] = (stamps - stamps.min()).dt.days / 365.25
    frame["log_nav_begin"] = np.log(frame["nav_begin"].where(frame["nav_begin"] > 0))
    frame["ret_lag1_6"] = frame.groupby("fund_code")["total_return"].transform(
        lambda s: s.shift(1).rolling(6, min_periods=6).apply(lambda w: np.prod(1 + w) - 1)
    )
    return frame


def _demean(sample: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    out = sample.copy()
    out[columns] = out[columns] - out.groupby("fund_code")[columns].transform("mean")
    return out


def _fit(frame: pd.DataFrame, dependent: str, regressors: list[str]) -> dict | None:
    sample = frame.dropna(subset=[dependent, *regressors, "month"])
    if len(sample) <= len(regressors) + 2:
        return None
    columns = [dependent, *regressors]
    sample = _demean(sample, columns)
    beta, std_err, r2 = ols(
        sample[dependent].to_numpy(),
        sample[regressors].to_numpy(),
        cluster=sample["month"].astype(str).to_numpy(),
    )
    return {
        "beta": beta,
        "se": std_err,
        "r2": r2,
        "n": len(sample),
        "clusters": sample["month"].nunique(),
    }


def ladder(frame: pd.DataFrame, dependent: str) -> pd.DataFrame:
    """Each driver alone, controls added cumulatively."""
    rows = []
    for group, subset in frame.groupby("asset_class_group"):
        for driver, label in DRIVERS.items():
            for depth in range(len(CONTROLS) + 1):
                regressors = [driver, *CONTROLS[:depth]]
                fit = _fit(subset, dependent, regressors)
                if fit is None:
                    continue
                rows.append(
                    {
                        "group": group,
                        "driver": driver,
                        "driver_label": label,
                        "controls": ", ".join(CONTROLS[:depth]) or "none",
                        "coef": fit["beta"][0],
                        "std_err": fit["se"][0],
                        "t": fit["beta"][0] / fit["se"][0],
                        "n_obs": fit["n"],
                        "n_clusters": fit["clusters"],
                        "within_r2": fit["r2"],
                    }
                )
    return pd.DataFrame(rows)


def joint(frame: pd.DataFrame, dependent: str) -> pd.DataFrame:
    """All outside options at once, under the full control set."""
    rows = []
    drivers = list(DRIVERS)
    for group, subset in frame.groupby("asset_class_group"):
        regressors = [*drivers, *CONTROLS]
        fit = _fit(subset, dependent, regressors)
        if fit is None:
            continue
        for index, name in enumerate(drivers):
            rows.append(
                {
                    "group": group,
                    "driver": name,
                    "coef_joint": fit["beta"][index],
                    "std_err": fit["se"][index],
                    "t": fit["beta"][index] / fit["se"][index],
                    "n_obs": fit["n"],
                    "n_clusters": fit["clusters"],
                }
            )
    return pd.DataFrame(rows)


def _log_marginal(y: np.ndarray, X: np.ndarray, g: float) -> float:
    """Log marginal likelihood under Zellner's g-prior, integrated in closed form.

    A g-prior puts the prior covariance proportional to (X'X)^-1, which makes the
    evidence available analytically. g = n is the unit-information prior, so the
    prior carries the weight of a single observation and does not quietly do the
    work that the 46 months are supposed to do.
    """
    n = len(y)
    centred = y - y.mean()
    total_ss = float(centred @ centred)
    if X.shape[1] == 0:
        return -0.5 * (n - 1) * np.log(total_ss)
    Xc = X - X.mean(axis=0)
    gram = Xc.T @ Xc
    try:
        beta = np.linalg.solve(gram, Xc.T @ centred)
    except np.linalg.LinAlgError:
        return -np.inf
    model_ss = float(centred @ Xc @ beta)
    r2 = model_ss / total_ss if total_ss > 0 else 0.0
    r2 = min(max(r2, 0.0), 1 - 1e-12)
    k = X.shape[1]
    return (
        0.5 * (n - 1 - k) * np.log(1 + g)
        - 0.5 * (n - 1) * np.log(1 + g * (1 - r2))
        - 0.5 * (n - 1) * np.log(total_ss)
    )


def bayesian_inclusion(frame: pd.DataFrame, dependent: str) -> pd.DataFrame:
    """Posterior inclusion probability per driver, over all 2**k subsets.

    Controls are partialled out first rather than entered as candidates, because
    the question is which outside options matter, not whether a time trend does.

    The evidence is computed on month means, not on fund-months. Every driver
    here varies only across months, so 335 fund-months carry the information of
    37 months; scoring them as independent multiplies the evidence by roughly
    the number of funds and returns inclusion probabilities of 1.0000 that mean
    nothing. Collapsing after residualising is the Bayesian counterpart of the
    month clustering the frequentist fits already use.
    """
    drivers = list(DRIVERS)
    rows = []
    for group, subset in frame.groupby("asset_class_group"):
        sample = subset.dropna(subset=[dependent, *drivers, *CONTROLS, "month"])
        if len(sample) <= len(drivers) + len(CONTROLS) + 2:
            continue
        columns = [dependent, *drivers, *CONTROLS]
        sample = _demean(sample, columns)

        # Residualise on the controls so the evidence compares like with like.
        control_matrix = np.column_stack(
            [np.ones(len(sample)), sample[list(CONTROLS)].to_numpy()]
        )

        def strip(vector: np.ndarray) -> np.ndarray:
            coef, *_ = np.linalg.lstsq(control_matrix, vector, rcond=None)
            return vector - control_matrix @ coef

        stripped = pd.DataFrame({"month": sample["month"].to_numpy()})
        stripped[dependent] = strip(sample[dependent].to_numpy())
        for name in drivers:
            stripped[name] = strip(sample[name].to_numpy())

        # One observation per month: the effective sample for a macro regressor.
        collapsed = stripped.groupby("month", as_index=False).mean()
        n_months = len(collapsed)
        if n_months <= len(drivers) + 2:
            continue

        y = collapsed[dependent].to_numpy()
        X = collapsed[drivers].to_numpy()
        X = (X - X.mean(axis=0)) / X.std(axis=0)

        g = float(n_months)
        evidence = {}
        for mask in range(1 << len(drivers)):
            picked = [i for i in range(len(drivers)) if mask >> i & 1]
            evidence[mask] = _log_marginal(y, X[:, picked] if picked else X[:, :0], g)

        keys = list(evidence)
        logs = np.array([evidence[k] for k in keys])
        weights = np.exp(logs - logs.max())
        weights /= weights.sum()
        posterior = dict(zip(keys, weights))

        for index, name in enumerate(drivers):
            pip = sum(w for mask, w in posterior.items() if mask >> index & 1)
            rows.append(
                {
                    "group": group,
                    "driver": name,
                    "posterior_inclusion_probability": pip,
                    "prior_inclusion_probability": 0.5,
                    "bayes_factor_vs_prior": (pip / (1 - pip)) if pip < 1 else np.inf,
                    "n_fund_months": len(sample),
                    "n_months_effective": n_months,
                }
            )
        best = max(posterior, key=posterior.get)
        rows.append(
            {
                "group": group,
                "driver": "__best_model__",
                "posterior_inclusion_probability": posterior[best],
                "prior_inclusion_probability": np.nan,
                "bayes_factor_vs_prior": np.nan,
                "n_fund_months": len(sample),
                "n_months_effective": n_months,
                "best_model_terms": ", ".join(
                    drivers[i] for i in range(len(drivers)) if best >> i & 1
                )
                or "(none)",
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    frame = _load_panel()
    frame["asset_class_group"] = np.where(
        frame["asset_class"].eq("bond"), "bond", "equity_balanced"
    )
    OUT.mkdir(parents=True, exist_ok=True)

    for dependent in ("gross_subscription_rate", "gross_redemption_rate"):
        tag = "subscriptions" if "subscription" in dependent else "redemptions"
        ladder(frame, dependent).to_csv(
            OUT / f"outside_options_ladder_{tag}.csv", index=False
        )
        joint(frame, dependent).to_csv(
            OUT / f"outside_options_joint_{tag}.csv", index=False
        )
        bayesian_inclusion(frame, dependent).to_csv(
            OUT / f"outside_options_bma_{tag}.csv", index=False
        )
        print(f"wrote three tables for {tag}")


if __name__ == "__main__":
    main()
