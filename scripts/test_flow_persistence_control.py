"""Does the performance response survive controlling for flow persistence?

The gross-flow literature reports that fund flows are highly persistent, that
persistence dominates performance as a predictor of future flows, and that
omitting it produces incorrect inferences about the performance-flow relation.
The primary specification in `performance_horizon_response` absorbs fund and
month effects but carries no lagged flow term, and this project's own backtest
already scores flow momentum above performance out of sample. The headline
+3.21pp is therefore open to the charge that it is persistence wearing a
performance label.

Three specifications per horizon, same sample and same fixed effects:

  baseline   performance rank only, as published
  own lag    plus the previous month's rate of the same leg
  both lags  plus both legs, since a fund losing money may also be selling less

Lags are exact-consecutive: a gap in the archive breaks the lag rather than
bridging it. Including a lagged dependent variable alongside fund effects is
biased by construction, but the bias is of order 1/T and T is around fifty
months here, so it is small relative to the coefficient being defended. It
still argues for reading the direction of the change rather than the second
decimal of it.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from vngross.growth import (
    _exact_lag,
    _exact_lagged_compound,
    _fit_dummy_fe,
    _monthly_base,
    _sample_filter,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "output" / "growth_research"
HORIZONS = (3, 6, 12)


def build() -> pd.DataFrame:
    monthly = pd.read_csv(ROOT / "data" / "output" / "vngross_fund_month.csv")
    base = _monthly_base(monthly)
    return_col = "total_return" if "total_return" in base else "gross_return"
    for horizon in HORIZONS:
        base[f"lagged_return_{horizon}m"] = _exact_lagged_compound(
            base, horizon, return_col
        )
    for column in ("gross_subscription_rate", "gross_redemption_rate"):
        base[f"lag_{column}"] = _exact_lag(base, column)
    return _sample_filter(base, gross=True, sample="equity_balanced")


def main() -> None:
    sample = build()
    rows = []

    for horizon in HORIZONS:
        performance = f"lagged_return_{horizon}m"
        ranks = sample.groupby(["month", "asset_class"], observed=True)[
            performance
        ].rank(method="average")
        counts = sample.groupby(["month", "asset_class"], observed=True)[
            performance
        ].transform("count")
        rank_column = f"performance_rank_{horizon}m"
        sample[rank_column] = ((ranks - 1) / (counts - 1)).where(counts > 1)

        for label, measure in (
            ("subscriptions", "gross_subscription_rate"),
            ("redemptions", "gross_redemption_rate"),
        ):
            own = f"lag_{measure}"
            other = (
                "lag_gross_redemption_rate"
                if measure == "gross_subscription_rate"
                else "lag_gross_subscription_rate"
            )
            specs = {
                "baseline": [rank_column],
                "own_lag": [rank_column, own],
                "both_lags": [rank_column, own, other],
            }
            for spec_name, predictors in specs.items():
                # One sample for all three specs so the comparison is like for
                # like: a coefficient that moves because the rows moved proves
                # nothing about persistence.
                complete = sample.dropna(
                    subset=[rank_column, measure, own, other]
                ).copy()
                if complete.empty:
                    continue
                outcome = f"{measure}_percentage_points"
                complete[outcome] = complete[measure] * 100
                for column in (own, other):
                    complete[column] = complete[column] * 100
                try:
                    fit = _fit_dummy_fe(
                        complete,
                        outcome,
                        predictors,
                        name=f"{label}_{horizon}m_{spec_name}",
                    )
                except ValueError:
                    continue
                rows.append(
                    {
                        "horizon_months": horizon,
                        "dependent": label,
                        "specification": spec_name,
                        "performance_coef_pp": fit.coefficients[rank_column],
                        "performance_t": fit.coefficients[rank_column]
                        / fit.std_errors[rank_column],
                        "own_lag_coef": fit.coefficients.get(own),
                        "own_lag_t": (
                            fit.coefficients[own] / fit.std_errors[own]
                            if own in fit.coefficients
                            else None
                        ),
                        "n_observations": fit.n_obs,
                        "n_months": fit.n_clusters,
                    }
                )

    table = pd.DataFrame(rows)
    table.to_csv(OUT / "performance_persistence_control.csv", index=False)

    pd.set_option("display.width", 220)
    for label in ("subscriptions", "redemptions"):
        print(f"=== {label} ===")
        block = table[table["dependent"].eq(label)]
        print(
            block.pivot(
                index="horizon_months",
                columns="specification",
                values=["performance_coef_pp", "performance_t"],
            )[
                [
                    ("performance_coef_pp", s)
                    for s in ("baseline", "own_lag", "both_lags")
                ]
                + [("performance_t", s) for s in ("baseline", "own_lag", "both_lags")]
            ]
            .round(2)
            .to_string()
        )
        lag = block[block["specification"].eq("own_lag")]
        print(
            "  own-lag coefficient: "
            + ", ".join(
                f"{int(r.horizon_months)}m {r.own_lag_coef:.2f} (t={r.own_lag_t:.2f})"
                for r in lag.itertuples()
            )
        )
        print(f"  n = {block['n_observations'].iloc[0]} fund-months")
        print()


if __name__ == "__main__":
    main()
