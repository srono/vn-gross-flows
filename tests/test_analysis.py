"""Tests for the analysis module.

The estimator is hand-rolled rather than taken from a library, so it is checked
against results that are known independently: numpy's own least-squares solver,
the closed-form HC0 sandwich, and a synthetic panel whose true coefficients are
set by construction.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from vngross.analysis import (
    FLOW_MEASURES,
    add_lagged_performance,
    compare,
    flow_performance,
    netting_loss,
    ols,
    paired_specifications,
    performance_rank_splines,
    quintile_table,
)


# --- the estimator --------------------------------------------------------


@pytest.fixture()
def toy():
    rng = np.random.default_rng(0)
    n = 200
    x1 = rng.normal(size=n)
    x2 = rng.normal(size=n)
    y = 1.5 + 2.0 * x1 - 0.5 * x2 + rng.normal(scale=0.3, size=n)
    X = np.column_stack([np.ones(n), x1, x2])
    return y, X


def test_coefficients_match_numpy_lstsq(toy) -> None:
    y, X = toy
    beta, _, _ = ols(y, X)
    expected, *_ = np.linalg.lstsq(X, y, rcond=None)
    assert np.allclose(beta, expected)


def test_recovers_known_coefficients(toy) -> None:
    y, X = toy
    beta, _, _ = ols(y, X)
    assert beta[0] == pytest.approx(1.5, abs=0.1)
    assert beta[1] == pytest.approx(2.0, abs=0.1)
    assert beta[2] == pytest.approx(-0.5, abs=0.1)


def test_r_squared_matches_definition(toy) -> None:
    y, X = toy
    beta, _, r2 = ols(y, X)
    resid = y - X @ beta
    expected = 1 - (resid**2).sum() / ((y - y.mean()) ** 2).sum()
    assert r2 == pytest.approx(expected)


def test_singleton_clusters_reproduce_hc0(toy) -> None:
    """One observation per cluster is HC0 by construction; assert it."""
    y, X = toy
    _, se_robust, _ = ols(y, X, cluster=None, dof_correction=False)
    _, se_cluster, _ = ols(
        y, X, cluster=np.arange(len(y)), dof_correction=False
    )
    assert np.allclose(se_robust, se_cluster)


def test_clustering_changes_standard_errors_not_coefficients(toy) -> None:
    y, X = toy
    groups = np.repeat(np.arange(20), 10)
    beta_a, se_a, _ = ols(y, X)
    beta_b, se_b, _ = ols(y, X, cluster=groups)
    assert np.allclose(beta_a, beta_b)
    assert not np.allclose(se_a, se_b)


def test_correlated_clusters_inflate_standard_errors() -> None:
    """With within-cluster correlation, clustered errors must exceed naive ones.

    This is the whole reason for clustering, so it is worth asserting rather
    than assuming.
    """
    rng = np.random.default_rng(7)
    n_groups, per = 30, 20
    groups = np.repeat(np.arange(n_groups), per)
    shock = np.repeat(rng.normal(scale=2.0, size=n_groups), per)
    x = np.repeat(rng.normal(size=n_groups), per) + rng.normal(scale=0.1, size=n_groups * per)
    y = 1.0 * x + shock + rng.normal(scale=0.1, size=n_groups * per)
    X = np.column_stack([np.ones(len(y)), x])

    _, se_naive, _ = ols(y, X)
    _, se_clustered, _ = ols(y, X, cluster=groups)
    assert se_clustered[1] > se_naive[1]


def test_too_few_observations_raises() -> None:
    with pytest.raises(ValueError):
        ols(np.array([1.0, 2.0]), np.ones((2, 3)))


# --- panel construction ---------------------------------------------------


def _panel(n_funds: int = 6, n_periods: int = 60, seed: int = 3) -> pd.DataFrame:
    """A synthetic panel where the two gross legs respond in opposite ways.

    Subscriptions load positively on lagged performance and redemptions load
    positively too, so their difference, the net flow, has almost no loading.
    That is the exact pattern the paired specification exists to detect.
    """
    rng = np.random.default_rng(seed)
    rows = []
    dates = pd.date_range("2023-01-06", periods=n_periods, freq="7D")
    for f in range(n_funds):
        nav = 1e11 * (1 + f)
        for t, day in enumerate(dates):
            ret = rng.normal(scale=0.02)
            rows.append(
                {
                    "fund_code": f"F{f}",
                    "period_start": day - pd.Timedelta(days=6),
                    "period_end": day,
                    "gross_return": ret,
                    "excess_return": ret - 0.001,
                    "market_return": rng.normal(scale=0.015),
                    "nav_begin": nav,
                    "period_days": 6,
                    "gross_legs_disclosed": True,
                }
            )
    frame = pd.DataFrame(rows)
    frame = add_lagged_performance(frame, windows=(1, 4))
    driver = frame["ret_lag1_4"].fillna(0.0)
    noise = rng.normal(scale=0.002, size=len(frame))
    frame["gross_subscription_rate"] = 0.012 + 0.30 * driver + noise
    frame["gross_redemption_rate"] = 0.006 + 0.28 * driver + rng.normal(scale=0.002, size=len(frame))
    frame["net_flow_rate"] = frame.gross_subscription_rate - frame.gross_redemption_rate
    frame["churn_rate"] = frame.gross_subscription_rate + frame.gross_redemption_rate
    return frame


def test_lagged_performance_never_uses_the_current_period() -> None:
    frame = _panel(n_funds=2, n_periods=10)
    one = frame[frame.fund_code == "F0"].sort_values("period_end")
    # The 1-period window at row i must equal the return at row i-1.
    assert one["ret_lag1_1"].iloc[3] == pytest.approx(one["gross_return"].iloc[2])
    assert pd.isna(one["ret_lag1_1"].iloc[0])


def test_lagged_window_sums_the_right_number_of_periods() -> None:
    frame = _panel(n_funds=1, n_periods=12)
    one = frame.sort_values("period_end")
    expected = one["gross_return"].iloc[1:5].sum()
    assert one["ret_lag1_4"].iloc[5] == pytest.approx(expected)


def test_rank_splines_partition_the_rank() -> None:
    """The three segments must sum back to the rank, with nothing lost."""
    frame = performance_rank_splines(_panel(), "ret_lag1_4")
    # min_count keeps an all-missing row missing instead of summing it to zero.
    parts = frame[["rank_low", "rank_mid", "rank_high"]].sum(axis=1, min_count=1)
    ranked = frame["perf_rank"].notna()
    assert ranked.sum() > 0
    assert np.allclose(parts[ranked], frame.loc[ranked, "perf_rank"])
    assert parts[~ranked].isna().all()


def test_rank_splines_isolate_the_top_and_bottom() -> None:
    """A top-decile fund loads only the high segment beyond the break."""
    frame = performance_rank_splines(_panel(), "ret_lag1_4", breaks=(0.2, 0.8))
    ranked = frame.dropna(subset=["perf_rank"])

    top = ranked.loc[ranked.perf_rank.idxmax()]
    assert top.perf_rank == pytest.approx(1.0)
    assert top.rank_low == pytest.approx(0.2)
    assert top.rank_mid == pytest.approx(0.6)
    assert top.rank_high == pytest.approx(0.2)

    # With six funds the worst rank is 1/6, inside the bottom segment, so the
    # middle and top segments must both be exactly zero for it.
    bottom = ranked.loc[ranked.perf_rank.idxmin()]
    assert bottom.rank_low == pytest.approx(bottom.perf_rank)
    assert bottom.rank_mid == pytest.approx(0.0)
    assert bottom.rank_high == pytest.approx(0.0)


# --- the paired comparison ------------------------------------------------


def test_paired_specifications_detect_cancellation() -> None:
    """Both legs load on performance; the net of them barely does."""
    results = paired_specifications(_panel(), performance="ret_lag1_4")
    subs = results["gross_subscription_rate"].coefficients["ret_lag1_4"]
    reds = results["gross_redemption_rate"].coefficients["ret_lag1_4"]
    net = results["net_flow_rate"].coefficients["ret_lag1_4"]

    assert subs == pytest.approx(0.30, abs=0.05)
    assert reds == pytest.approx(0.28, abs=0.05)
    # The net loading is the difference, an order of magnitude smaller.
    assert abs(net) < 0.1 * abs(subs)
    assert net == pytest.approx(subs - reds, abs=1e-9)


def test_paired_specifications_share_one_sample() -> None:
    results = paired_specifications(_panel(), performance="ret_lag1_4")
    counts = {r.n_obs for r in results.values()}
    assert len(counts) == 1, "the three regressions must run on the same rows"


def test_gross_only_restriction_excludes_net_only_managers() -> None:
    frame = _panel()
    frame.loc[frame.fund_code == "F0", "gross_legs_disclosed"] = False
    frame.loc[frame.fund_code == "F0", ["gross_subscription_rate", "gross_redemption_rate"]] = np.nan

    results = paired_specifications(frame, performance="ret_lag1_4", gross_only=True)
    assert results["net_flow_rate"].n_funds == 5


def test_fund_effects_are_absorbed_not_dummied() -> None:
    result = flow_performance(
        _panel(), "net_flow_rate", ["ret_lag1_4"], fund_effects=True
    )
    assert result.absorbed == ("fund_code",)
    assert "const" not in result.coefficients


def test_without_fund_effects_an_intercept_appears() -> None:
    result = flow_performance(
        _panel(), "net_flow_rate", ["ret_lag1_4"], fund_effects=False
    )
    assert "const" in result.coefficients


def test_clusters_default_to_the_time_dimension() -> None:
    result = flow_performance(_panel(), "net_flow_rate", ["ret_lag1_4"])
    assert result.cluster_on == "month"
    # Far more month clusters than fund clusters, which is the point.
    assert result.n_clusters > result.n_funds


def test_compare_lines_up_one_term() -> None:
    results = paired_specifications(_panel(), performance="ret_lag1_4")
    table = compare(results, "ret_lag1_4")
    assert list(table["dependent"]) == list(FLOW_MEASURES)
    assert table["coef"].notna().all()


def test_specification_with_no_complete_cases_raises() -> None:
    frame = _panel()
    frame["ret_lag1_4"] = np.nan
    with pytest.raises(ValueError):
        flow_performance(frame, "net_flow_rate", ["ret_lag1_4"])


# --- descriptive diagnostics ---------------------------------------------


def test_netting_loss_reports_hidden_activity() -> None:
    stats = netting_loss(_panel())
    assert stats["n_obs"] > 0
    assert 0.0 <= stats["share_quiet"] <= 1.0
    assert stats["mean_gross_subscription_rate"] > stats["mean_gross_redemption_rate"]


def test_netting_loss_ignores_net_only_rows() -> None:
    frame = _panel()
    frame.loc[frame.fund_code == "F0", "gross_legs_disclosed"] = False
    stats = netting_loss(frame)
    assert stats["n_obs"] == len(frame[frame.fund_code != "F0"])


def test_quintile_table_is_non_parametric_and_complete() -> None:
    table = quintile_table(_panel(), "ret_lag1_4")
    assert len(table) == 5
    assert table["n"].sum() > 0
    for measure in FLOW_MEASURES:
        assert f"{measure} %" in table.columns
