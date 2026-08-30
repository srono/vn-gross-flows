"""Deterministic tests for growth and investor-segment analytics."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from vngross.growth import (
    aggregate_investor_demand_monthly,
    book_flow_conversion,
    flow_persistence,
    hac_ols,
    infer_investor_demand,
    investor_macro_sensitivity,
    investor_performance_response,
    investor_demand_persistence,
    manager_rotation_upper_bound,
    performance_horizon_response,
    regime_transition_table,
)


def _period_rows() -> pd.DataFrame:
    """Three constant-NAV/unit periods with known unit-demand decomposition."""
    return pd.DataFrame(
        [
            {
                "fund_code": "F",
                "period_start": "2024-01-01",
                "period_end": "2024-01-07",
                "nav_begin": 100_000.0,
                "nav_end": 101_000.0,
                "nav_per_unit_begin": 100.0,
                "nav_per_unit_end": 100.0,
                "units_begin": 1_000.0,
                "units_end": 1_010.0,
                "foreign_units": 204.0,
                "foreign_value": 20_400.0,
                "prior_foreign_units": 200.0,
                "prior_foreign_value": 20_000.0,
                "net_flow": 1_000.0,
                "chg_investment": 0.0,
                "chg_distribution": 0.0,
                "market_return": 0.01,
                "manager_id": "m",
                "asset_class": "equity",
            },
            {
                "fund_code": "F",
                "period_start": "2024-01-07",
                "period_end": "2024-01-14",
                "nav_begin": 101_000.0,
                "nav_end": 100_500.0,
                "nav_per_unit_begin": 100.0,
                "nav_per_unit_end": 100.0,
                "units_begin": 1_010.0,
                "units_end": 1_005.0,
                "foreign_units": 202.0,
                "foreign_value": 20_200.0,
                "prior_foreign_units": 204.0,
                "prior_foreign_value": 20_400.0,
                "net_flow": -500.0,
                "chg_investment": 0.0,
                "chg_distribution": 0.0,
                "market_return": -0.005,
                "manager_id": "m",
                "asset_class": "equity",
            },
            {
                "fund_code": "F",
                "period_start": "2024-01-14",
                "period_end": "2024-01-21",
                "nav_begin": 100_500.0,
                "nav_end": 100_700.0,
                "nav_per_unit_begin": 100.0,
                "nav_per_unit_end": 100.0,
                "units_begin": 1_005.0,
                "units_end": 1_007.0,
                "foreign_units": 203.0,
                "foreign_value": 20_300.0,
                "prior_foreign_units": 202.0,
                "prior_foreign_value": 20_200.0,
                "net_flow": 200.0,
                "chg_investment": 0.0,
                "chg_distribution": 0.0,
                "market_return": 0.002,
                "manager_id": "m",
                "asset_class": "equity",
            },
        ]
    )


def _monthly_panel(n_months: int = 18, n_funds: int = 4) -> pd.DataFrame:
    """Balanced deterministic panel whose relative performance rotates by month."""
    rows: list[dict] = []
    funds = [f"F{i}" for i in range(n_funds)]
    for fund_index, fund in enumerate(funds):
        nav = 1_000_000_000_000.0 + fund_index * 10_000_000_000.0
        for month_index, month in enumerate(pd.period_range("2023-01", periods=n_months, freq="M")):
            # The rank rotates, avoiding a time-invariant regressor absorbed by fund FE.
            score = ((fund_index + month_index) % n_funds) / max(n_funds - 1, 1)
            total_return = -0.02 + 0.04 * score
            subscription_rate = 0.02 + 0.004 * ((month_index - 1 + fund_index) % n_funds)
            redemption_rate = 0.01 + 0.001 * ((month_index + 2 * fund_index) % 3)
            net_rate = subscription_rate - redemption_rate
            subscriptions = subscription_rate * nav
            redemptions = -redemption_rate * nav
            net_flow = net_rate * nav
            nav_end = nav + net_flow
            rows.append(
                {
                    "fund_code": fund,
                    "manager_id": "m0" if fund_index < 2 else "m1",
                    "asset_class": "equity",
                    "month": str(month),
                    "period_end": month.to_timestamp("M"),
                    "nav_begin": nav,
                    "nav_end": nav_end,
                    "subscriptions": subscriptions,
                    "redemptions": redemptions,
                    "net_flow": net_flow,
                    "gross_subscription_rate": subscription_rate,
                    "gross_redemption_rate": redemption_rate,
                    "net_flow_rate": net_rate,
                    "gross_legs_disclosed": True,
                    "total_return": total_return,
                    "market_return": 0.005,
                    "reconcile_residual_vnd": 0.0,
                }
            )
            nav = nav_end
    return pd.DataFrame(rows)


# Investor split -----------------------------------------------------------


def test_first_row_uses_same_filing_prior_and_decomposes_units() -> None:
    result = infer_investor_demand(_period_rows())
    first = result.iloc[0]
    assert bool(first.split_valid)
    assert first.split_opening_source == "same_filing_prior"
    assert first.delta_total_units == pytest.approx(10.0)
    assert first.delta_foreign_units == pytest.approx(4.0)
    assert first.delta_domestic_units == pytest.approx(6.0)
    assert first.foreign_unit_demand_vnd == pytest.approx(400.0)
    assert first.domestic_unit_demand_vnd == pytest.approx(600.0)
    assert first.foreign_unit_demand_vnd + first.domestic_unit_demand_vnd == pytest.approx(
        first.total_unit_demand_vnd
    )
    assert first.unit_proxy_error_nav_rate == pytest.approx(0.0)


def test_first_row_without_prior_foreign_stock_is_invalid_and_null() -> None:
    data = _period_rows().iloc[[0]].copy()
    data["prior_foreign_units"] = np.nan
    result = infer_investor_demand(data).iloc[0]
    assert not bool(result.split_valid)
    assert "first observed row" in result.split_reason
    assert pd.isna(result.foreign_unit_demand_vnd)
    assert pd.isna(result.domestic_demand_rate)


def test_foreign_above_total_rejected_and_invalid_previous_poisons_pair() -> None:
    data = _period_rows()
    data.loc[0, "foreign_units"] = 2_000.0
    data.loc[0, "foreign_value"] = 200_000.0
    result = infer_investor_demand(data)
    assert not bool(result.iloc[0].split_valid)
    assert "exceed total" in result.iloc[0].split_reason
    assert not bool(result.iloc[1].split_valid)
    assert "previous foreign_units" in result.iloc[1].split_reason
    assert pd.isna(result.iloc[1].foreign_unit_demand_vnd)


def test_prior_nonzero_when_previous_zero_fails() -> None:
    data = _period_rows()
    data.loc[0, ["foreign_units", "foreign_value"]] = [0.0, 0.0]
    data.loc[0, ["prior_foreign_units", "prior_foreign_value"]] = [0.0, 0.0]
    data.loc[1, "prior_foreign_units"] = 10.0
    data.loc[1, "foreign_units"] = 1.0
    data.loc[1, "foreign_value"] = 100.0
    result = infer_investor_demand(data)
    assert not bool(result.iloc[1].split_valid)
    assert "disagree" in result.iloc[1].split_reason


def test_foreign_value_tolerance_is_scaled_by_total_nav() -> None:
    data = _period_rows().iloc[[0]].copy()
    # VND 50 error is 50% of foreign value but below 0.1% of total NAV (VND 101).
    data.loc[data.index[0], ["foreign_units", "foreign_value"]] = [1.0, 150.0]
    data.loc[data.index[0], ["prior_foreign_units", "prior_foreign_value"]] = [1.0, 100.0]
    result = infer_investor_demand(data).iloc[0]
    assert bool(result.split_valid)


def test_missing_net_flow_and_unit_proxy_break_are_invalid() -> None:
    missing = _period_rows().iloc[[0]].copy()
    missing["net_flow"] = np.nan
    assert "net_flow" in infer_investor_demand(missing).iloc[0].split_reason

    broken = _period_rows()
    broken.loc[1, "units_end"] += 20.0
    # Keep current foreign stock physically valid and NAV identity intact; only unit proxy fails.
    result = infer_investor_demand(broken).iloc[1]
    assert not bool(result.split_valid)
    assert "proxy error" in result.split_reason
    assert pd.isna(result.delta_total_units)


def test_noncontiguous_pair_is_invalid() -> None:
    data = _period_rows()
    data.loc[1, "period_start"] = "2024-01-12"
    result = infer_investor_demand(data).iloc[1]
    assert not bool(result.split_valid)
    assert "non-contiguous" in result.split_reason


def test_monthly_split_requires_every_period_and_preserves_identity() -> None:
    split = infer_investor_demand(_period_rows())
    monthly = aggregate_investor_demand_monthly(split)
    assert len(monthly) == 1
    row = monthly.iloc[0]
    assert row.n_periods == 3
    assert row.total_unit_demand_vnd == pytest.approx(700.0)
    assert row.foreign_unit_demand_vnd == pytest.approx(300.0)
    assert row.domestic_unit_demand_vnd == pytest.approx(400.0)
    assert row.foreign_unit_demand_vnd + row.domestic_unit_demand_vnd == pytest.approx(
        row.total_unit_demand_vnd
    )
    assert row.manager_id == "m"

    split.loc[1, "split_valid"] = False
    assert aggregate_investor_demand_monthly(split).empty


# Performance, conversion, persistence ------------------------------------


def test_performance_horizon_keeps_pre_filter_lag_history() -> None:
    panel = _monthly_panel()
    result = performance_horizon_response(panel, horizons=(3,))
    # Observation 5 can use observations 2-4: 14 usable months x 4 funds.
    assert result[3]["subscriptions"].n_obs == 56
    assert result[3]["subscriptions"].n_funds == 4
    assert result[3]["subscriptions"].n_clusters == 14
    assert result[3]["subscriptions"].absorbed == ("fund_code", "month")
    assert result[3]["subscriptions"].cluster_on == "month"


def test_performance_exact_gap_and_dirty_month_invalidate_windows() -> None:
    panel = _monthly_panel()
    gap_index = panel[(panel.fund_code == "F0") & (panel.month == "2023-03")].index[0]
    gap_result = performance_horizon_response(panel.drop(gap_index), horizons=(3,))
    assert gap_result[3]["net"].n_obs == 54

    dirty = panel.copy()
    dirty.loc[(dirty.fund_code == "F0") & (dirty.month == "2023-03"), "reconcile_residual_vnd"] = 1e9
    dirty_result = performance_horizon_response(dirty, horizons=(3,))
    assert dirty_result[3]["net"].n_obs == 54


def test_performance_peer_rank_spans_bottom_to_top() -> None:
    panel = _monthly_panel()
    # Flow is exactly 1pp + 2pp times the prior-month rotating performance rank.
    prior_rank: dict[tuple[str, str], float] = {}
    funds = sorted(panel.fund_code.unique())
    for month in sorted(panel.month.unique()):
        current = panel[panel.month == month].set_index("fund_code").total_return
        ranks = (current.rank(method="average") - 1) / (len(funds) - 1)
        for fund, rank in ranks.items():
            prior_rank[(fund, month)] = float(rank)
    for fund in funds:
        months = sorted(panel[panel.fund_code == fund].month)
        for current, previous in zip(months[1:], months[:-1]):
            value = 0.01 + 0.02 * prior_rank[(fund, previous)]
            mask = (panel.fund_code == fund) & (panel.month == current)
            panel.loc[mask, "gross_subscription_rate"] = value
            panel.loc[mask, "subscriptions"] = value * panel.loc[mask, "nav_begin"]
            panel.loc[mask, "net_flow_rate"] = value - panel.loc[mask, "gross_redemption_rate"]
            panel.loc[mask, "net_flow"] = panel.loc[mask, "net_flow_rate"] * panel.loc[mask, "nav_begin"]
    result = performance_horizon_response(panel, horizons=(1,))[1]["subscriptions"]
    coefficient = next(iter(result.coefficients.values()))
    assert coefficient == pytest.approx(2.0, abs=1e-8)


def test_book_conversion_uses_vnd_and_tuple_grouping() -> None:
    panel = _monthly_panel(n_months=8)
    result = book_flow_conversion(panel, by=("manager_id", "asset_class"))
    assert set(result.manager_id) == {"m0", "m1"}
    assert (result.n_months == 4).all()
    assert (result.n_funds == 2).all()
    assert np.allclose(
        result.redemptions_per_100_subscriptions
        + result.retained_net_per_100_subscriptions,
        100.0,
    )
    assert result.metric_scope.eq("aggregate_book_flow_not_customer_cohort_retention").all()


def test_book_conversion_filters_dirty_and_dcip() -> None:
    panel = _monthly_panel(n_months=8)
    dirty_index = panel[(panel.fund_code == "F0") & (panel.month == "2023-08")].index[0]
    panel.loc[dirty_index, "reconcile_residual_vnd"] = 1e9
    dcip = panel[panel.fund_code == "F0"].copy()
    dcip["fund_code"] = "DCIP"
    result = book_flow_conversion(pd.concat([panel, dcip]), by="fund")
    assert "DCIP" not in result.fund_code.to_list()
    assert result.set_index("fund_code").loc["F0", "n_observations"] == 3


def test_flow_persistence_uses_exact_pairs_and_fixed_effects() -> None:
    panel = _monthly_panel()
    result = flow_persistence(panel, sample="equity_balanced")
    assert result["subscriptions"].n_obs == 56
    assert result["subscriptions"].absorbed == ("fund_code", "month")
    gap = panel.drop(panel[(panel.fund_code == "F0") & (panel.month == "2023-10")].index)
    gap_result = flow_persistence(gap, sample="equity_balanced")
    assert gap_result["subscriptions"].n_obs == 54


def test_segment_performance_and_persistence_respect_labels_and_exact_months() -> None:
    panel = _monthly_panel()
    split_rows = []
    for _, row in panel.iterrows():
        total = row.nav_begin
        # Meaningful 30/70 opening segment split with deterministic net demand.
        foreign_rate = 0.005 + 0.25 * row.net_flow_rate
        domestic_rate = 0.01 + 0.50 * row.net_flow_rate
        split_rows.append(
            {
                "fund_code": row.fund_code,
                "manager_id": row.manager_id,
                "asset_class": row.asset_class,
                "month": row.month,
                "fund_month_order": int(pd.Period(row.month, freq="M").ordinal - pd.Period("2023-01", freq="M").ordinal + 1),
                "opening_total_aum_vnd": total,
                "opening_foreign_aum_vnd": total * 0.30,
                "opening_domestic_aum_vnd": total * 0.70,
                "foreign_unit_demand_vnd": total * 0.30 * foreign_rate,
                "domestic_unit_demand_vnd": total * 0.70 * domestic_rate,
                "foreign_demand_rate": foreign_rate,
                "domestic_demand_rate": domestic_rate,
                "market_return": row.market_return,
            }
        )
    demand = pd.DataFrame(split_rows)
    response = investor_performance_response(panel, demand, horizons=(3,))
    assert set(response[3]) == {"foreign", "domestic"}
    assert response[3]["foreign"].n_obs == 56
    assert "net certificate demand" in response[3]["foreign"].note.lower()

    persistence = investor_demand_persistence(demand, sample="equity_balanced")
    assert set(persistence) == {"foreign", "domestic"}
    assert persistence["foreign"].n_obs == 56
    assert persistence["foreign"].absorbed == ("fund_code", "month")

    gap = demand.drop(demand[(demand.fund_code == "F0") & (demand.month == "2023-10")].index)
    gap_result = investor_demand_persistence(gap, sample="equity_balanced")
    assert gap_result["foreign"].n_obs == 54


def test_segment_minimum_share_excludes_tiny_foreign_denominator() -> None:
    panel = _monthly_panel()
    demand = pd.DataFrame(
        {
            "fund_code": panel.fund_code,
            "manager_id": panel.manager_id,
            "asset_class": panel.asset_class,
            "month": panel.month,
            "fund_month_order": panel.groupby("fund_code").cumcount() + 1,
            "opening_total_aum_vnd": panel.nav_begin,
            "opening_foreign_aum_vnd": panel.nav_begin * 0.001,
            "opening_domestic_aum_vnd": panel.nav_begin * 0.999,
            "foreign_unit_demand_vnd": panel.nav_begin * 0.001 * 0.01,
            "domestic_unit_demand_vnd": panel.nav_begin * 0.999 * 0.01,
            "foreign_demand_rate": 0.01,
            "domestic_demand_rate": 0.01,
            "market_return": panel.market_return,
        }
    )
    response = investor_performance_response(panel, demand, horizons=(3,))
    assert "foreign" not in response[3]
    assert "domestic" in response[3]


# Regimes and sibling offsets ---------------------------------------------


def _transition_panel() -> pd.DataFrame:
    flows = [0.01, 0.01, 0.01, 0.01, 0.02, -0.01, -0.02, -0.03]
    rows = []
    for index, (month, flow) in enumerate(zip(pd.period_range("2024-01", periods=8, freq="M"), flows)):
        rows.append(
            {
                "fund_code": "F",
                "manager_id": "m",
                "asset_class": "equity",
                "month": str(month),
                "nav_begin": 100.0,
                "nav_end": 100.0 + flow * 100,
                "net_flow": flow * 100,
                "net_flow_rate": flow,
                "reconcile_residual_vnd": 0.0,
            }
        )
    return pd.DataFrame(rows)


def test_transition_streaks_are_exact_after_inception_filter() -> None:
    result = regime_transition_table(_transition_panel()).set_index("regime")
    assert result.loc["nonnegative", "n_transitions"] == 1
    assert result.loc["first_negative", "n_transitions"] == 1
    assert result.loc["2plus_negative", "n_transitions"] == 1
    assert result.loc["nonnegative", "next_negative_probability"] == 1.0
    assert result.loc["first_negative", "next_negative_probability"] == 1.0
    assert result.loc["2plus_negative", "next_negative_probability"] == 1.0


def test_transition_gap_breaks_streak_and_transition() -> None:
    panel = _transition_panel()
    panel.loc[panel.month == "2024-07", "month"] = "2024-08"
    panel.loc[panel.month == "2024-08", "month"] = "2024-09"
    result = regime_transition_table(panel).set_index("regime")
    assert result.n_transitions.sum() == 1  # May -> June only.
    assert result.loc["first_negative", "n_transitions"] == 0


def test_sibling_offset_is_upper_bound_after_filters() -> None:
    panel = _monthly_panel(n_months=5, n_funds=2)
    month = "2023-05"
    panel.loc[(panel.fund_code == "F0") & (panel.month == month), ["net_flow", "net_flow_rate"]] = [
        100.0,
        0.1,
    ]
    panel.loc[(panel.fund_code == "F1") & (panel.month == month), ["net_flow", "net_flow_rate"]] = [
        -60.0,
        -0.05,
    ]
    result = manager_rotation_upper_bound(panel)
    assert len(result) == 1
    row = result.iloc[0]
    assert row.sibling_offset_upper_bound_vnd == pytest.approx(60.0)
    assert row.upper_bound_share_of_outflow == pytest.approx(1.0)
    assert row.metric_scope == "upper_bound_not_observed_customer_transfers"


# HAC and macro ------------------------------------------------------------


def test_hac_ols_recovers_deterministic_slope() -> None:
    x = np.arange(30, dtype=float)
    noise = np.sin(x) * 0.01
    y = 1.5 + 2.0 * x + noise
    beta, standard_errors, r_squared = hac_ols(y, np.column_stack([np.ones(len(x)), x]))
    assert beta[1] == pytest.approx(2.0, abs=0.001)
    assert standard_errors[1] > 0
    assert r_squared > 0.999


def _monthly_demand_for_macro() -> pd.DataFrame:
    rows = []
    for index, month in enumerate(pd.period_range("2023-01", periods=14, freq="M")):
        local = 0.001 * index
        rows.append(
            {
                "fund_code": "F",
                "manager_id": "vinacapital",
                "asset_class": "equity",
                "month": str(month),
                "fund_month_order": index + 5,
                "opening_total_aum_vnd": 1_000.0,
                "opening_foreign_aum_vnd": 300.0,
                "opening_domestic_aum_vnd": 700.0,
                "foreign_unit_demand_vnd": 300.0 * (0.01 + local),
                "domestic_unit_demand_vnd": 700.0 * (0.02 - local),
                "market_return": local,
            }
        )
    return pd.DataFrame(rows)


def test_macro_observed_only_and_lag_samples() -> None:
    demand = _monthly_demand_for_macro()
    macro = pd.DataFrame(
        {
            "month": demand.month,
            "deposit_rate_pct": np.linspace(4.0, 6.0, len(demand)),
            "deposit_observed": [True, False] * 7,
        }
    )
    result = investor_macro_sensitivity(
        demand,
        macro,
        predictor_scales={"deposit_rate_pct": 1.0},
        sample="vinacapital",
        observed_only={"deposit_rate_pct": "deposit_observed"},
        controls=(),
        winsor=None,
    )
    assert len(result) == 4  # two segments x current/lag1
    current = result[result.timing == "current"]
    lagged = result[result.timing == "lag1"]
    assert (current.n_months == 7).all()
    # Current months whose immediately previous macro month was observed:
    # February, April, ..., December, and the following February = seven.
    assert (lagged.n_months == 7).all()
    assert lagged.sample_start.eq("2023-02").all()
    assert result.interpretation.eq("exploratory_association_not_causal").all()


def test_empty_inputs_are_safe() -> None:
    empty = pd.DataFrame()
    assert infer_investor_demand(empty).empty
    assert aggregate_investor_demand_monthly(empty).empty
    assert performance_horizon_response(empty) == {}
    assert book_flow_conversion(empty).empty
    assert flow_persistence(empty) == {}
    assert regime_transition_table(empty).empty
    assert manager_rotation_upper_bound(empty).empty


# Publication synchronization ---------------------------------------------


def test_growth_publication_artifacts_match_live_panel_and_documentation() -> None:
    """Generated evidence, manifests, and current-build prose must move together."""
    root = Path(__file__).resolve().parents[1]
    output = root / "data" / "output"
    research = output / "growth_research"
    period = pd.read_csv(output / "vngross_fund_period.csv", low_memory=False)
    monthly = pd.read_csv(output / "vngross_fund_month.csv", low_memory=False)
    manifest = json.loads((research / "manifest.json").read_text(encoding="utf-8"))
    core_manifest = json.loads(
        (output / "build_manifest.json").read_text(encoding="utf-8")
    )

    def sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    assert manifest["period_rows"] == len(period) == 3_989
    assert manifest["monthly_rows"] == len(monthly) == 950
    assert manifest["economic_funds"] == period["fund_code"].nunique() == 18
    assert manifest["investor_split_valid_periods"] == 3_871
    assert manifest["investor_split_invalid_periods"] == 118
    assert manifest["period_panel_sha256"] == sha256(
        output / "vngross_fund_period.csv"
    )
    assert manifest["monthly_panel_sha256"] == sha256(
        output / "vngross_fund_month.csv"
    )
    for name, expected_hash in manifest["output_hashes"].items():
        assert sha256(research / name) == expected_hash

    assert core_manifest["period_rows"] == len(period)
    assert core_manifest["month_rows"] == len(monthly)
    assert core_manifest["economic_fund_count"] == 18
    assert core_manifest["continuity_count"] == 79
    assert core_manifest["hashes"]["vngross_fund_period.csv"] == sha256(
        output / "vngross_fund_period.csv"
    )
    assert core_manifest["hashes"]["vngross_fund_month.csv"] == sha256(
        output / "vngross_fund_month.csv"
    )

    summary = pd.read_csv(research / "investor_net_demand_exclusion_summary.csv")
    exclusions = pd.read_csv(research / "investor_net_demand_exclusions.csv", low_memory=False)
    assert summary["n_periods"].sum() == len(exclusions) == 118
    assert set(summary["check_category"]) >= {
        "non_contiguous_filing_pair",
        "critical_fields_missing",
        "foreign_units_exceed_total_units",
        "unit_flow_proxy_error",
    }

    churn = pd.read_csv(research / "hidden_gross_churn.csv")
    vinacapital = churn.set_index("sample").loc["vinacapital"]
    assert vinacapital["max_quiet_fund_code"] == "VINACAPITAL-VIBF"
    assert vinacapital["max_quiet_period_end"] == "2025-09-15"
    assert vinacapital["max_quiet_source"].startswith("https://")
    assert vinacapital["max_quiet_churn_rate"] == pytest.approx(0.0973590662)

    for document in ("README.md", "METHOD.md", "DATA_DICTIONARY.md"):
        text = (root / document).read_text(encoding="utf-8")
        assert "3,989" in text
        assert "18 economic" in text
        assert "3,860" not in text


def test_hidden_churn_correlation_is_not_decided_by_one_row() -> None:
    """`corr_net_churn` and the volatility ratio are non-robust statistics.

    VLGF subscribed 1,380% of its own NAV in the week to 2022-04-06. Left
    unwinsorised that single row drives the correlation to 0.99 and the
    subscription/redemption volatility ratio to 13.2, which would assert that
    net flow explains essentially all gross activity and contradict the very
    finding this table reports. The inception filter cannot remove it, because
    VLGF had been filing for three months before it took the mandate.
    """
    table = pd.read_csv(
        Path("data/output/growth_research/hidden_gross_churn.csv")
    )
    assert not table.empty
    # A correlation this high would mean the netting-loss finding is not real.
    assert (table["corr_net_churn"] < 0.6).all()
    # Gross legs are of comparable scale once one launch week stops dominating.
    assert (table["sd_ratio_subs_over_reds"].between(0.5, 3.0)).all()
    # The evidence the table exists to carry is unaffected by the clipping.
    row = table[table["sample"] == "all_comparable"].iloc[0]
    assert row["share_quiet"] > 0.10
    assert row["max_churn_when_quiet"] > 0.05
    assert str(row["max_quiet_source"]).startswith("http")
