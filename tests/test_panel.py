"""Acceptance tests for panel assembly, per spec section 8."""

from __future__ import annotations

import copy
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from vngross.appendix_xxiv import Filing, parse_text
from vngross.panel import (
    attach_deposit_rate,
    attach_market_return,
    build_fund_period_panel,
    deduplicate,
    to_monthly,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "vcbbcf_20221121.txt"


def _filing(
    fund_code: str,
    start: date,
    end: date,
    nav_begin: float,
    subs: float,
    reds: float,
    invest: float,
    source: str = "s",
) -> Filing:
    """A synthetic filing that closes the identity by construction."""
    nav_end = nav_begin + invest + subs + reds
    return Filing(
        fund_code=fund_code,
        period_start=start,
        period_end=end,
        values={
            "nav_begin": nav_begin,
            "nav_end": nav_end,
            "nav_per_unit_begin": 10_000.0,
            "nav_per_unit_end": 10_000.0 * (1 + invest / nav_begin),
            "chg_investment": invest,
            "subscriptions": subs,
            "redemptions": reds,
            "chg_flows_net": subs + reds,
        },
        source=source,
    )


@pytest.fixture()
def real_filing() -> Filing:
    return parse_text(FIXTURE.read_text(encoding="utf-8"))


# --- quarantine, spec section 4.5 -----------------------------------------


def test_three_filings_one_corrupted_yields_two_rows_and_a_reason(
    real_filing: Filing,
) -> None:
    good_a = _filing("AAA", date(2023, 1, 2), date(2023, 1, 6), 1e11, 5e8, -2e8, 1e9)
    good_b = _filing("BBB", date(2023, 1, 2), date(2023, 1, 6), 2e11, 1e9, -5e8, 2e9)
    broken = copy.deepcopy(real_filing)
    broken.values["subscriptions"] = 8_062_772_600.0  # single-digit slip

    result = build_fund_period_panel([good_a, good_b, broken])
    assert len(result.panel) == 2
    assert len(result.quarantine) == 1
    reason = result.quarantine.iloc[0]["quarantine_reason"]
    assert reason
    assert "identity" in reason


def test_quarantine_carries_the_residual_for_audit(real_filing: Filing) -> None:
    broken = copy.deepcopy(real_filing)
    broken.values["chg_investment"] = 0.0
    result = build_fund_period_panel([broken])
    assert result.panel.empty
    assert abs(result.quarantine.iloc[0]["reconcile_residual_vnd"]) > 1e9


def test_a_clean_filing_is_never_quarantined(real_filing: Filing) -> None:
    result = build_fund_period_panel([real_filing])
    assert len(result.panel) == 1
    assert result.quarantine.empty


def test_implausible_period_length_is_quarantined() -> None:
    """A misread header produces a year-long "week"; it must not enter the panel."""
    odd = _filing("AAA", date(2023, 1, 2), date(2023, 12, 6), 1e11, 5e8, -2e8, 1e9)
    result = build_fund_period_panel([odd])
    assert result.panel.empty
    assert "period_days" in result.quarantine.iloc[0]["quarantine_reason"]


# --- deduplication --------------------------------------------------------


def test_identical_republication_collapses_to_one_row() -> None:
    a = _filing("AAA", date(2023, 1, 2), date(2023, 1, 6), 1e11, 5e8, -2e8, 1e9, "f_1")
    b = _filing("AAA", date(2023, 1, 2), date(2023, 1, 6), 1e11, 5e8, -2e8, 1e9, "f_3")
    kept, superseded = deduplicate([a, b])
    assert len(kept) == 1
    assert len(superseded) == 1
    assert "identical" in superseded[0]["reason"]


def test_restatement_keeps_the_later_file_and_flags_it() -> None:
    a = _filing("AAA", date(2023, 1, 2), date(2023, 1, 6), 1e11, 5e8, -2e8, 1e9, "f_1")
    b = _filing("AAA", date(2023, 1, 2), date(2023, 1, 6), 1e11, 6e8, -2e8, 1e9, "f_3")
    kept, superseded = deduplicate([a, b])
    assert len(kept) == 1
    assert kept[0].source == "f_3"
    assert "restatement" in superseded[0]["reason"]


def test_distinct_periods_are_not_collapsed() -> None:
    a = _filing("AAA", date(2023, 1, 2), date(2023, 1, 6), 1e11, 5e8, -2e8, 1e9)
    b = _filing("AAA", date(2023, 1, 9), date(2023, 1, 13), 1e11, 5e8, -2e8, 1e9)
    kept, superseded = deduplicate([a, b])
    assert len(kept) == 2
    assert not superseded


def test_duplicates_do_not_double_count_flows() -> None:
    a = _filing("AAA", date(2023, 1, 2), date(2023, 1, 6), 1e11, 5e8, -2e8, 1e9, "f_1")
    b = _filing("AAA", date(2023, 1, 2), date(2023, 1, 6), 1e11, 5e8, -2e8, 1e9, "f_3")
    panel = build_fund_period_panel([a, b]).panel
    assert len(panel) == 1
    assert panel["subscriptions"].sum() == pytest.approx(5e8)


# --- derived flow measures, spec sections 4.2 and 4.3 ---------------------




def test_longer_period_supersedes_same_opening_boundary() -> None:
    short = _filing(
        "AAA", date(2025, 1, 17), date(2025, 1, 21), 1e11, 5e8, -2e8, 1e9,
        "weekly_20250121",
    )
    long = _filing(
        "AAA", date(2025, 1, 17), date(2025, 1, 23), 1e11, 7e8, -3e8, 2e9,
        "weekly_20250123",
    )

    kept, superseded = deduplicate([long, short])

    assert kept == [long]
    assert len(superseded) == 1
    assert superseded[0]["source"] == "weekly_20250121"
    assert superseded[0]["superseded_by"] == "weekly_20250123"
    assert "longer period" in superseded[0]["reason"]
def _chain(fund_code: str, specs: list[tuple]) -> list[Filing]:
    """Build a contiguous run of filings where each opens at the prior close.

    The monthly identity can only survive aggregation if the weekly rows actually
    chain, so test data that does not chain would be testing nothing.
    """
    filings: list[Filing] = []
    nav_begin = 1e11
    for start, end, subs, reds, invest in specs:
        filing = _filing(fund_code, start, end, nav_begin, subs, reds, invest)
        filings.append(filing)
        nav_begin = filing.values["nav_end"]
    return filings


@pytest.fixture()
def panel() -> pd.DataFrame:
    rows = _chain(
        "AAA",
        [
            (date(2023, 1, 2), date(2023, 1, 6), 5e8, -2e8, 1e9),
            (date(2023, 1, 9), date(2023, 1, 13), 1e8, -9e8, -5e8),
            (date(2023, 2, 6), date(2023, 2, 10), 0.0, 0.0, 1e8),
        ],
    )
    return build_fund_period_panel(rows).panel


def test_flow_rates_scale_by_beginning_nav(panel: pd.DataFrame) -> None:
    first = panel.iloc[0]
    assert first["gross_subscription_rate"] == pytest.approx(5e8 / 1e11)
    assert first["gross_redemption_rate"] == pytest.approx(2e8 / 1e11)
    assert first["net_flow_rate"] == pytest.approx(3e8 / 1e11)
    assert first["churn_rate"] == pytest.approx(7e8 / 1e11)


def test_redemption_rate_is_positive_magnitude(panel: pd.DataFrame) -> None:
    """Redemptions are negative in the filing; the rate is a magnitude."""
    assert (panel["gross_redemption_rate"].dropna() >= 0).all()


def test_flow_asymmetry_is_bounded(panel: pd.DataFrame) -> None:
    values = panel["flow_asymmetry"].dropna()
    assert not values.empty
    assert values.between(-1.0, 1.0).all()


def test_flow_asymmetry_is_undefined_when_there_is_no_flow(
    panel: pd.DataFrame,
) -> None:
    """Zero gross flow gives 0/0; that is missing, not zero."""
    assert pd.isna(panel.iloc[2]["flow_asymmetry"])


def test_gross_legs_survive_into_the_panel(panel: pd.DataFrame) -> None:
    for column in ("subscriptions", "redemptions"):
        assert column in panel.columns
    assert (panel["subscriptions"] >= 0).all()
    assert (panel["redemptions"] <= 0).all()


def test_fund_metadata_is_merged() -> None:
    rows = [_filing("AAA", date(2023, 1, 2), date(2023, 1, 6), 1e11, 5e8, -2e8, 1e9)]
    result = build_fund_period_panel(
        rows, fund_meta={"AAA": {"asset_class": "equity", "fund_name": "A Fund"}}
    )
    assert result.panel.iloc[0]["asset_class"] == "equity"


# --- market return, spec section 4.4 --------------------------------------


@pytest.fixture()
def index() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "time": pd.to_datetime(
                ["2022-12-30", "2023-01-03", "2023-01-06", "2023-01-13", "2023-02-10"]
            ),
            "close": [1000.0, 1010.0, 1020.0, 1040.0, 1100.0],
        }
    )


def test_market_return_uses_last_close_at_or_before_each_boundary(
    panel: pd.DataFrame, index: pd.DataFrame
) -> None:
    """2023-01-02 is a holiday; the boundary takes the 2022-12-30 close."""
    joined = attach_market_return(panel, index)
    first = joined.iloc[0]
    assert first["index_begin"] == pytest.approx(1000.0)
    assert first["index_end"] == pytest.approx(1020.0)
    assert first["market_return"] == pytest.approx(1020.0 / 1000.0 - 1.0)


def test_non_trading_boundaries_do_not_create_gaps(
    panel: pd.DataFrame, index: pd.DataFrame
) -> None:
    joined = attach_market_return(panel, index)
    assert joined["market_return"].notna().all()


def test_excess_return_is_fund_less_market(
    panel: pd.DataFrame, index: pd.DataFrame
) -> None:
    joined = attach_market_return(panel, index)
    assert joined["excess_return"].iloc[0] == pytest.approx(
        joined["gross_return"].iloc[0] - joined["market_return"].iloc[0]
    )


def test_empty_index_is_an_error(panel: pd.DataFrame) -> None:
    with pytest.raises(ValueError):
        attach_market_return(panel, pd.DataFrame(columns=["time", "close"]))


# --- deposit rate ---------------------------------------------------------


def test_deposit_rate_provenance_reaches_the_panel(panel: pd.DataFrame) -> None:
    rates = pd.DataFrame(
        {
            "month": ["2023-01", "2023-02"],
            "rate_pct": [7.4, 7.2],
            "provenance": ["Agribank board rate, snapshot X", "carried forward"],
        }
    )
    joined = attach_deposit_rate(panel, rates)
    assert joined["deposit_rate_pct"].iloc[0] == pytest.approx(7.4)
    assert joined["deposit_rate_pct"].iloc[2] == pytest.approx(7.2)
    assert "Agribank" in joined["deposit_rate_provenance"].iloc[0]


def test_missing_month_leaves_rate_null_not_filled(panel: pd.DataFrame) -> None:
    rates = pd.DataFrame(
        {"month": ["2023-01"], "rate_pct": [7.4], "provenance": ["observed"]}
    )
    joined = attach_deposit_rate(panel, rates)
    assert pd.isna(joined["deposit_rate_pct"].iloc[2])


# --- monthly rollup -------------------------------------------------------


def test_to_monthly_sums_flows(panel: pd.DataFrame) -> None:
    monthly = to_monthly(panel)
    january = monthly[monthly["month"] == "2023-01"].iloc[0]
    assert january["subscriptions"] == pytest.approx(6e8)
    assert january["redemptions"] == pytest.approx(-11e8)
    assert january["n_periods"] == 2


def test_to_monthly_takes_first_opening_and_last_closing(
    panel: pd.DataFrame,
) -> None:
    """NAV must not be averaged, or the identity stops closing after rollup."""
    monthly = to_monthly(panel)
    january = monthly[monthly["month"] == "2023-01"].iloc[0]
    weeks = panel[panel["period_end"].astype(str).str.startswith("2023-01")]
    assert january["nav_begin"] == pytest.approx(weeks.iloc[0]["nav_begin"])
    assert january["nav_end"] == pytest.approx(weeks.iloc[-1]["nav_end"])


def test_identity_survives_monthly_aggregation(panel: pd.DataFrame) -> None:
    monthly = to_monthly(panel)
    for _, row in monthly.iterrows():
        rebuilt = (
            row["nav_begin"]
            + row["chg_investment"]
            + row["subscriptions"]
            + row["redemptions"]
        )
        assert rebuilt == pytest.approx(row["nav_end"], abs=5.0)
        assert row["reconcile_residual_vnd"] == pytest.approx(0.0, abs=5.0)


def test_monthly_residual_exposes_an_internal_gap() -> None:
    a = _filing("AAA", date(2023, 1, 2), date(2023, 1, 6), 1e11, 5e8, -2e8, 1e9)
    c = _filing("AAA", date(2023, 1, 16), date(2023, 1, 20), 5e11, 5e8, -2e8, 1e9)
    period = build_fund_period_panel([a, c]).panel

    monthly = to_monthly(period)

    assert len(monthly) == 1
    assert abs(monthly.iloc[0]["reconcile_residual_vnd"]) > 1e9


def test_to_monthly_compounds_returns(panel: pd.DataFrame) -> None:
    monthly = to_monthly(panel)
    january = monthly[monthly["month"] == "2023-01"].iloc[0]
    weeks = panel[panel["period_end"].astype(str).str.startswith("2023-01")]
    expected = (1 + weeks["gross_return"]).prod() - 1
    assert january["gross_return"] == pytest.approx(expected)


def test_to_monthly_flow_rates_are_rederived(panel: pd.DataFrame) -> None:
    monthly = to_monthly(panel)
    january = monthly[monthly["month"] == "2023-01"].iloc[0]
    assert january["gross_subscription_rate"] == pytest.approx(
        january["subscriptions"] / january["nav_begin"]
    )
    assert monthly["flow_asymmetry"].dropna().between(-1, 1).all()


# --- continuity in the panel result --------------------------------------


def test_continuity_break_is_reported_in_the_result() -> None:
    a = _filing("AAA", date(2023, 1, 2), date(2023, 1, 6), 1e11, 5e8, -2e8, 1e9)
    # Opening NAV deliberately unequal to a's closing: a week is missing.
    c = _filing("AAA", date(2023, 1, 16), date(2023, 1, 20), 5e11, 5e8, -2e8, 1e9)
    result = build_fund_period_panel([a, c])
    assert len(result.continuity_breaks) == 1
    assert "missed filing" in result.continuity_breaks.iloc[0]["detail"]


def test_quarantined_filing_does_not_bridge_retained_rows() -> None:
    a = _filing("AAA", date(2023, 1, 2), date(2023, 1, 6), 1e11, 5e8, -2e8, 1e9)
    bad = _filing(
        "AAA",
        date(2023, 1, 7),
        date(2023, 2, 20),
        a.values["nav_end"],
        5e8,
        -2e8,
        1e9,
    )
    c = _filing(
        "AAA",
        date(2023, 2, 21),
        date(2023, 2, 24),
        bad.values["nav_end"],
        5e8,
        -2e8,
        1e9,
    )

    result = build_fund_period_panel([a, bad, c])

    assert len(result.panel) == 2
    assert len(result.quarantine) == 1
    assert "period_days" in result.quarantine.iloc[0]["quarantine_reason"]
    assert len(result.continuity_breaks) == 1
    assert "missed filing" in result.continuity_breaks.iloc[0]["detail"]


def test_diagnostics_are_emitted_for_every_filing(real_filing: Filing) -> None:
    result = build_fund_period_panel([real_filing])
    assert len(result.diagnostics) == 1
    assert result.diagnostics.iloc[0]["proxy_end_error_pct"] is not None


def test_empty_input_produces_empty_frames() -> None:
    result = build_fund_period_panel([])
    assert result.panel.empty
    assert result.quarantine.empty
