"""Does global macro drive foreign demand once one mandate stops being it?

The brief concluded that generic global indicators give no reliable foreign
demand signal. That test pooled every fund, and VLGF alone is 39% of all
foreign activity in the panel while the top two funds are 53%. VLGF is 98%
foreign, and a single period is 46% of every subscription it has ever taken:
it is one institutional mandate, and a mandate does not respond to the VIX on
a monthly schedule. A pooled null is what a dominant idiosyncratic series
produces regardless of whether the underlying relationship exists.

So the same test is run twice, with and without the funds that are really one
decision-maker, and both are reported. If global macro only appears once VLGF
is removed, the earlier null was about VLGF and not about foreign investors.

Evidence is scored on month means. Every regressor here varies only across
months, so fund-months are not independent observations for it.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from vngross.analysis import ols

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "interim" / "global_macro"
OUT = ROOT / "data" / "output" / "growth_research"

# Funds whose foreign side is one mandate rather than a population of investors.
MANDATE_FUNDS = {"VLGF", "VINACAPITAL-VLBF"}

LEVELS = {"DGS10": "us10y_pct", "FEDFUNDS": "fed_funds_pct", "VIXCLS": "vix"}
RETURNS = {
    "SP500": "sp500_return",
    "DTWEXBGS": "broad_dollar_return",
    "GC_F": "gold_return",
    "VND_X": "usdvnd_return",
}
DRIVERS = [*LEVELS.values(), *RETURNS.values()]
CONTROLS = ("market_return", "time_trend", "log_opening_aum")


def _monthly(name: str, column: str, as_return: bool) -> pd.DataFrame:
    frame = pd.read_csv(CACHE / f"{name}.csv")
    date_col, value_col = frame.columns[0], frame.columns[1]
    frame[date_col] = pd.to_datetime(frame[date_col])
    frame[value_col] = pd.to_numeric(frame[value_col], errors="coerce")
    frame = frame.dropna(subset=[value_col])
    monthly = (
        frame.set_index(date_col)[value_col].resample("ME").last().to_frame("value")
    )
    monthly["month"] = monthly.index.to_period("M").astype(str)
    if as_return:
        monthly[column] = np.log(monthly["value"]).diff()
    else:
        monthly[column] = monthly["value"]
    return monthly[["month", column]].reset_index(drop=True)


def load() -> pd.DataFrame:
    frame = pd.read_csv(OUT / "investor_net_demand_monthly.csv")
    frame = frame[frame["split_valid"].fillna(False)].copy()

    for name, column in LEVELS.items():
        frame = frame.merge(_monthly(name, column, False), on="month", how="left")
    for name, column in RETURNS.items():
        frame = frame.merge(_monthly(name, column, True), on="month", how="left")

    # Everything the investor could have known before the month began.
    order = frame.sort_values("month")["month"].unique()
    lookup = {m: i for i, m in enumerate(order)}
    frame["month_index"] = frame["month"].map(lookup)
    lagged = frame.groupby("month", as_index=False)[DRIVERS].first()
    lagged["month_index"] = lagged["month"].map(lookup) + 1
    frame = frame.drop(columns=DRIVERS).merge(
        lagged.drop(columns="month"), on="month_index", how="left"
    )

    frame["time_trend"] = frame["month_index"] / 12.0
    frame["log_opening_aum"] = np.log(
        frame["opening_total_aum_vnd"].where(frame["opening_total_aum_vnd"] > 0)
    )
    return frame


def _log_marginal(y: np.ndarray, X: np.ndarray, g: float) -> float:
    n = len(y)
    centred = y - y.mean()
    total = float(centred @ centred)
    if X.shape[1] == 0:
        return -0.5 * (n - 1) * np.log(total)
    Xc = X - X.mean(axis=0)
    try:
        beta = np.linalg.solve(Xc.T @ Xc, Xc.T @ centred)
    except np.linalg.LinAlgError:
        return -np.inf
    r2 = min(max(float(centred @ Xc @ beta) / total, 0.0), 1 - 1e-12)
    k = X.shape[1]
    return (
        0.5 * (n - 1 - k) * np.log(1 + g)
        - 0.5 * (n - 1) * np.log(1 + g * (1 - r2))
        - 0.5 * (n - 1) * np.log(total)
    )


def analyse(frame: pd.DataFrame, dependent: str, label: str) -> pd.DataFrame:
    sample = frame.dropna(subset=[dependent, *DRIVERS, *CONTROLS, "month"]).copy()
    columns = [dependent, *DRIVERS, *CONTROLS]
    sample[columns] = sample[columns] - sample.groupby("fund_code")[columns].transform(
        "mean"
    )

    control_matrix = np.column_stack(
        [np.ones(len(sample)), sample[list(CONTROLS)].to_numpy()]
    )

    def strip(vector: np.ndarray) -> np.ndarray:
        coef, *_ = np.linalg.lstsq(control_matrix, vector, rcond=None)
        return vector - control_matrix @ coef

    stripped = pd.DataFrame({"month": sample["month"].to_numpy()})
    stripped[dependent] = strip(sample[dependent].to_numpy())
    for name in DRIVERS:
        stripped[name] = strip(sample[name].to_numpy())

    collapsed = stripped.groupby("month", as_index=False).mean()
    n_months = len(collapsed)
    y = collapsed[dependent].to_numpy()
    X = collapsed[DRIVERS].to_numpy()
    X = (X - X.mean(axis=0)) / X.std(axis=0)

    g = float(n_months)
    evidence = {
        mask: _log_marginal(
            y,
            X[:, [i for i in range(len(DRIVERS)) if mask >> i & 1]],
            g,
        )
        for mask in range(1 << len(DRIVERS))
    }
    keys = list(evidence)
    logs = np.array([evidence[k] for k in keys])
    weights = np.exp(logs - logs.max())
    weights /= weights.sum()
    posterior = dict(zip(keys, weights))

    rows = []
    for index, name in enumerate(DRIVERS):
        pip = sum(w for mask, w in posterior.items() if mask >> index & 1)
        rows.append(
            {
                "sample": label,
                "dependent": dependent,
                "driver": name,
                "posterior_inclusion_probability": pip,
                "n_fund_months": len(sample),
                "n_months_effective": n_months,
                "n_funds": sample["fund_code"].nunique(),
            }
        )
    best = max(posterior, key=posterior.get)
    rows.append(
        {
            "sample": label,
            "dependent": dependent,
            "driver": "__best_model__",
            "posterior_inclusion_probability": posterior[best],
            "n_fund_months": len(sample),
            "n_months_effective": n_months,
            "n_funds": sample["fund_code"].nunique(),
            "best_model_terms": ", ".join(
                DRIVERS[i] for i in range(len(DRIVERS)) if best >> i & 1
            )
            or "(none)",
        }
    )
    return pd.DataFrame(rows)


def main() -> None:
    frame = load()
    without = frame[~frame["fund_code"].isin(MANDATE_FUNDS)]

    results = []
    for dependent in ("foreign_demand_rate", "domestic_demand_rate"):
        results.append(analyse(frame, dependent, "all funds"))
        results.append(analyse(without, dependent, "excluding mandate funds"))
    table = pd.concat(results, ignore_index=True)
    table.to_csv(OUT / "foreign_macro_sensitivity.csv", index=False)

    pd.set_option("display.width", 200)
    for dependent in ("foreign_demand_rate", "domestic_demand_rate"):
        print(f"=== {dependent} ===")
        block = table[table["dependent"].eq(dependent)]
        pivot = block[block["driver"].ne("__best_model__")].pivot(
            index="driver", columns="sample", values="posterior_inclusion_probability"
        )
        print(pivot.round(3).to_string())
        for _, row in block[block["driver"].eq("__best_model__")].iterrows():
            print(
                f"  best model [{row['sample']}]: {row.get('best_model_terms')} "
                f"({row['n_funds']} funds, {row['n_months_effective']} months)"
            )
        print()


if __name__ == "__main__":
    main()
