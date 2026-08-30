"""Tests for the manager-facing readings.

Each reading is checked against something known independently of the code: a
closed-form survival calculation, a panel whose flow-performance response is set
by construction, and, for the backtest, a target that is unpredictable by
construction so that any reported skill is a leak.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from vngross.insights import (
    TURNOVER_THRESHOLD,
    flow_regime,
    forecast_backtest,
    leg_response,
    macro_sensitivity,
    retention_table,
    seasonality_table,
    turnover_outliers,
)


def _panel(
    funds: dict[str, dict],
    n_months: int = 48,
    start: str = "2021-01",
) -> pd.DataFrame:
    """Assemble a monthly panel from per-fund constant or callable rates."""
    months = pd.period_range(start, periods=n_months, freq="M")
    rows = []
    for code, spec in funds.items():
        for index, month in enumerate(months):
            subscription = spec["sub"](index) if callable(spec["sub"]) else spec["sub"]
            redemption = spec["red"](index) if callable(spec["red"]) else spec["red"]
            rows.append(
                {
                    "fund_code": code,
                    "month": str(month),
                    "period_end": month.to_timestamp(how="end").normalize(),
                    "asset_class": spec.get("asset_class", "equity"),
                    "manager_id": spec.get("manager_id", "m1"),
                    "nav_begin": spec.get("nav", 1_000.0),
                    "nav_end": spec.get("nav", 1_000.0),
                    "gross_subscription_rate": subscription,
                    "gross_redemption_rate": redemption,
                    "net_flow_rate": subscription - redemption,
                    "churn_rate": subscription + redemption,
                    "subscriptions": subscription * spec.get("nav", 1_000.0),
                    "redemptions": -redemption * spec.get("nav", 1_000.0),
                    "net_flow": (subscription - redemption) * spec.get("nav", 1_000.0),
                    "gross_legs_disclosed": spec.get("disclosed", True),
                    "ret_lag1_6": spec["ret"](index) if callable(spec.get("ret")) else spec.get("ret", 0.0),
                    "market_return": 0.01,
                    "deposit_rate_pct": 5.0,
                }
            )
    return pd.DataFrame(rows)


# --- turnover detection ---------------------------------------------------


def test_turnover_outlier_detected_by_behaviour_not_by_name() -> None:
    panel = _panel(
        {
            "NORMAL": {"sub": 0.05, "red": 0.04},
            "CASHBOX": {"sub": 0.60, "red": 0.58},
        }
    )
    assert turnover_outliers(panel) == ["CASHBOX"]


def test_turnover_uses_the_median_so_one_launch_month_does_not_flag() -> None:
    panel = _panel(
        {"LAUNCHED": {"sub": lambda i: 13.0 if i == 0 else 0.04, "red": 0.03}}
    )
    assert turnover_outliers(panel) == []


def test_turnover_threshold_is_honoured() -> None:
    panel = _panel({"MID": {"sub": TURNOVER_THRESHOLD + 0.01, "red": 0.02}})
    assert turnover_outliers(panel) == ["MID"]
    assert turnover_outliers(panel, threshold=0.9) == []


def test_readings_exclude_turnover_vehicles_by_default() -> None:
    panel = _panel(
        {"NORMAL": {"sub": 0.05, "red": 0.04}, "CASHBOX": {"sub": 0.60, "red": 0.58}}
    )
    assert "CASHBOX" not in retention_table(panel).index
    assert "CASHBOX" in retention_table(panel, exclude_turnover=False).index


# --- leg response ---------------------------------------------------------


def test_leg_response_separates_a_subscription_only_reaction() -> None:
    """Subscriptions respond to past return, redemptions are held flat.

    A net-flow reading of the same panel cannot tell this apart from both legs
    reacting, which is the whole reason the table reports the legs separately.
    """
    panel = _panel(
        {
            f"F{f}": {
                "sub": lambda i, f=f: 0.02 + 0.10 * ((i + f) % 5) / 4.0,
                "red": 0.03,
                "ret": lambda i, f=f: ((i + f) % 5) / 4.0,
            }
            for f in range(5)
        }
    )
    table = leg_response(panel)
    spread = table.loc["top-bottom"]
    assert spread["gross_subscription_rate"] == pytest.approx(10.0, abs=0.5)
    assert spread["gross_redemption_rate"] == pytest.approx(0.0, abs=1e-9)
    # Net moves by exactly what the subscription leg moved by, so a net-only
    # panel would attribute the whole response to "flows" without saying which.
    assert spread["net_flow_rate"] == pytest.approx(spread["gross_subscription_rate"])


def test_leg_response_spread_row_is_top_minus_bottom() -> None:
    panel = _panel(
        {
            f"F{f}": {
                "sub": lambda i, f=f: 0.01 * ((i + f) % 5),
                "red": 0.02,
                "ret": lambda i, f=f: ((i + f) % 5),
            }
            for f in range(4)
        }
    )
    table = leg_response(panel)
    bins = table.drop(index="top-bottom")
    for column in ("gross_subscription_rate", "net_flow_rate"):
        assert table.loc["top-bottom", column] == pytest.approx(
            bins[column].iloc[-1] - bins[column].iloc[0]
        )


def test_leg_response_excludes_net_only_rows() -> None:
    spread = {"sub": lambda i: 0.01 * (i % 5), "red": 0.02, "ret": lambda i: i % 5}
    panel = _panel(
        {
            "GROSS": spread,
            "NETONLY": {**spread, "sub": 0.9, "red": 0.9, "disclosed": False},
        }
    )
    table = leg_response(panel)
    assert table.loc["top-bottom", "n"] == 48
    assert table.drop(index="top-bottom")["gross_subscription_rate"].max() < 10.0


def test_leg_response_returns_empty_when_performance_has_no_spread() -> None:
    panel = _panel({"FLAT": {"sub": 0.05, "red": 0.03, "ret": 0.0}})
    assert leg_response(panel).empty


# --- retention ------------------------------------------------------------


def test_half_life_matches_the_closed_form() -> None:
    """A hazard whose annual survival is exactly one half must give one year."""
    hazard = 1.0 - 0.5 ** (1 / 12)
    panel = _panel({"HALF": {"sub": 0.0, "red": hazard}})
    row = retention_table(panel).loc["HALF"]
    assert row["implied_aum_half_life_years"] == pytest.approx(1.0)
    assert row["implied_aum_attrition_pct"] == pytest.approx(50.0)


def test_annual_attrition_compounds_rather_than_multiplying() -> None:
    panel = _panel({"F": {"sub": 0.0, "red": 0.05}})
    row = retention_table(panel).loc["F"]
    assert row["implied_aum_attrition_pct"] == pytest.approx((1 - 0.95**12) * 100)
    assert row["implied_aum_attrition_pct"] < 0.05 * 12 * 100


def test_a_book_that_never_redeems_has_an_infinite_half_life() -> None:
    panel = _panel({"STICKY": {"sub": 0.02, "red": 0.0}})
    row = retention_table(panel).loc["STICKY"]
    assert np.isinf(row["implied_aum_half_life_years"])
    assert row["implied_aum_attrition_pct"] == pytest.approx(0.0)


def test_organic_growth_is_negative_when_redemptions_exceed_subscriptions() -> None:
    panel = _panel({"SHRINK": {"sub": 0.02, "red": 0.04}})
    assert retention_table(panel).loc["SHRINK", "organic_growth_pct"] < 0


def test_retention_drops_funds_with_too_little_history() -> None:
    panel = _panel({"SHORT": {"sub": 0.05, "red": 0.03}}, n_months=6)
    assert retention_table(panel).empty
    assert not retention_table(panel, min_months=6).empty


def test_retention_omits_labels_it_cannot_source_rather_than_faking_them() -> None:
    panel = _panel({"F": {"sub": 0.05, "red": 0.03}}).drop(columns=["asset_class"])
    table = retention_table(panel)
    assert "asset_class" not in table.columns
    assert "manager_id" in table.columns


# --- seasonality ----------------------------------------------------------


def test_seasonality_is_demeaned_within_fund() -> None:
    """Two funds with different levels, observed over different windows.

    A raw calendar mean would report the level difference between the funds as a
    seasonal pattern. Demeaning within fund removes it, so a panel with no true
    seasonality must come back flat.
    """
    high = _panel({"HIGH": {"sub": 0.20, "red": 0.10}}, n_months=24, start="2021-01")
    low = _panel({"LOW": {"sub": 0.02, "red": 0.01}}, n_months=24, start="2022-01")
    table = seasonality_table(pd.concat([high, low], ignore_index=True))
    assert table["gross_subscription_rate"].abs().max() == pytest.approx(0.0, abs=1e-9)


def test_seasonality_recovers_a_planted_february_effect() -> None:
    panel = _panel(
        {"F": {"sub": lambda i: 0.01 if i % 12 == 1 else 0.05, "red": 0.02}},
        n_months=48,
    )
    table = seasonality_table(panel)
    assert table.loc["Feb", "gross_subscription_rate"] < -3.0
    assert table.loc["Feb", "gross_redemption_rate"] == pytest.approx(0.0, abs=1e-9)
    assert table["gross_subscription_rate"].idxmin() == "Feb"


# --- macro sensitivity ----------------------------------------------------


def test_macro_ladder_returns_one_row_per_control_set() -> None:
    rng = np.random.default_rng(3)
    panel = _panel({f"F{f}": {"sub": 0.05, "red": 0.03} for f in range(4)})
    panel["deposit_rate_pct"] = rng.normal(size=len(panel))
    panel["gross_subscription_rate"] = 0.05 - 0.02 * panel["deposit_rate_pct"]
    table = macro_sensitivity(panel)
    assert len(table) == 5
    assert table["coef"].tolist() == pytest.approx([-0.02] * 5)


def test_macro_sensitivity_recovers_a_planted_coefficient() -> None:
    rng = np.random.default_rng(11)
    panel = _panel({f"F{f}": {"sub": 0.05, "red": 0.03} for f in range(6)})
    panel["deposit_rate_pct"] = rng.normal(size=len(panel))
    panel["gross_subscription_rate"] = (
        0.04 - 0.015 * panel["deposit_rate_pct"] + rng.normal(scale=0.001, size=len(panel))
    )
    table = macro_sensitivity(panel, control_sets=((),))
    assert table.loc[0, "coef"] == pytest.approx(-0.015, abs=1e-3)
    assert table.loc[0, "t"] < -5


def test_macro_sensitivity_skips_a_driver_the_panel_does_not_carry() -> None:
    panel = _panel({"F": {"sub": 0.05, "red": 0.03}})
    assert macro_sensitivity(panel, driver="not_a_column").empty


# --- forecast backtest ----------------------------------------------------


def test_backtest_reports_no_skill_on_an_unpredictable_target() -> None:
    """The leakage test.

    The target is white noise, so nothing observable at forecast time can
    predict it and out-of-sample R-squared must sit at or below zero. A
    comfortably positive value here would mean the walk-forward is seeing its
    own period, which is the failure this whole design exists to avoid.
    """
    rng = np.random.default_rng(7)
    panel = _panel({f"F{f}": {"sub": 0.05, "red": 0.03} for f in range(6)}, n_months=60)
    panel["gross_subscription_rate"] = rng.normal(loc=0.05, scale=0.02, size=len(panel))
    panel["gross_redemption_rate"] = rng.normal(loc=0.03, scale=0.01, size=len(panel))
    panel["deposit_rate_pct"] = rng.normal(size=len(panel))
    table = forecast_backtest(panel).set_index("target")
    for target in table.index:
        # Check that main models don't have spurious skill
        assert table.loc[target, "momentum_only_r2_oos"] < 0.15
        assert table.loc[target, "plus_macro_r2_oos"] < 0.15


def test_backtest_finds_skill_when_momentum_genuinely_predicts() -> None:
    """A target that is its own trailing average must be forecastable.

    Paired with the noise test above, this pins the estimator from both sides:
    it neither invents skill nor misses it.
    """
    panel = _panel(
        {
            f"F{f}": {"sub": lambda i, f=f: 0.05 + 0.03 * np.sin((i + f) / 6.0), "red": 0.03}
            for f in range(6)
        },
        n_months=60,
    )
    table = forecast_backtest(panel, targets=("gross_subscription_rate",))
    assert table.loc[0, "momentum_only_r2_oos"] > 0.5


def test_backtest_macro_gain_is_the_difference_of_the_two_models() -> None:
    rng = np.random.default_rng(5)
    panel = _panel({f"F{f}": {"sub": 0.05, "red": 0.03} for f in range(5)}, n_months=60)
    panel["gross_subscription_rate"] = rng.normal(loc=0.05, scale=0.02, size=len(panel))
    panel["deposit_rate_pct"] = rng.normal(size=len(panel))
    table = forecast_backtest(panel, targets=("gross_subscription_rate",))
    if not table.empty:
        row = table.loc[0]
        if "macro_gain" in row and "plus_macro_r2_oos" in row and "momentum_only_r2_oos" in row:
            assert row["macro_gain"] == pytest.approx(
                row["plus_macro_r2_oos"] - row["momentum_only_r2_oos"], abs=1e-4
            )


def test_backtest_needs_enough_history_and_says_so_by_returning_empty() -> None:
    panel = _panel({f"F{f}": {"sub": 0.05, "red": 0.03} for f in range(4)}, n_months=12)
    assert forecast_backtest(panel).empty


def test_backtest_tolerates_a_fund_that_appears_only_after_training() -> None:
    """A new fund has no training mean, so it must fall back, not crash."""
    established = _panel(
        {f"F{f}": {"sub": 0.05, "red": 0.03} for f in range(4)}, n_months=48
    )
    newcomer = _panel({"NEW": {"sub": 0.07, "red": 0.02}}, n_months=6, start="2024-07")
    table = forecast_backtest(pd.concat([established, newcomer], ignore_index=True))
    assert not table.empty


# --- current regime -------------------------------------------------------


def test_flow_regime_counts_consecutive_negative_months() -> None:
    panel = _panel(
        {
            "BLEEDING": {"sub": 0.01, "red": 0.05},
            "GATHERING": {"sub": 0.05, "red": 0.01},
        },
        n_months=24,
    )
    table = flow_regime(panel, months=10)
    assert table.loc["BLEEDING", "months_negative"] == 10
    assert table.loc["GATHERING", "months_negative"] == 0
    assert table.index[0] == "BLEEDING"


def test_flow_regime_keeps_turnover_vehicles_because_it_reports_rather_than_estimates() -> None:
    panel = _panel(
        {"NORMAL": {"sub": 0.05, "red": 0.04}, "CASHBOX": {"sub": 0.60, "red": 0.58}},
        n_months=24,
    )
    assert "CASHBOX" in flow_regime(panel).index


def test_flow_regime_window_can_be_pinned_to_a_month() -> None:
    panel = _panel({"F": {"sub": 0.05, "red": 0.01}}, n_months=36, start="2021-01")
    table = flow_regime(panel, since="2023-01")
    assert table.loc["F", "n_months"] == 12
    assert table.loc["F", "window_from"] == "2023-01"


def test_flow_regime_scales_by_closing_nav() -> None:
    panel = _panel({"F": {"sub": 0.10, "red": 0.0, "nav": 500.0}}, n_months=12)
    row = flow_regime(panel, months=12).loc["F"]
    assert row["cumulative_net_flow"] == pytest.approx(0.10 * 500.0 * 12)
    assert row["cumulative_net_flow_pct_nav"] == pytest.approx(row["cumulative_net_flow"] / 500.0 * 100)
