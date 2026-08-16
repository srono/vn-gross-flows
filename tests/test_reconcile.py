"""Acceptance tests for the reconciliation gates, per spec section 8."""

from __future__ import annotations

import copy
from datetime import date
from pathlib import Path

import pytest

from vngross.appendix_xxiv import Filing, parse_text
from vngross.reconcile import (
    chain_continuity,
    check_net_flow_consistency,
    proxy_divergence,
    reconcile,
    summarise,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "vcbbcf_20221121.txt"


@pytest.fixture(scope="module")
def fixture_text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture()
def filing(fixture_text: str) -> Filing:
    return parse_text(fixture_text)


# --- the identity ---------------------------------------------------------


def test_identity_closes_to_zero_dong(filing: Filing) -> None:
    check = reconcile(filing)
    assert check.passed
    assert abs(check.residual_vnd) < 1.0


def test_net_flow_consistency_passes(filing: Filing) -> None:
    assert check_net_flow_consistency(filing).passed


def test_single_digit_slip_fails_the_gate(filing: Filing) -> None:
    """80,627,726 mistyped as 8,062,772,600 must not survive."""
    broken = copy.deepcopy(filing)
    broken.values["subscriptions"] = 8_062_772_600.0
    assert not reconcile(broken).passed
    assert not check_net_flow_consistency(broken).passed


def test_missing_nav_begin_fails_rather_than_assumes(filing: Filing) -> None:
    broken = copy.deepcopy(filing)
    del broken.values["nav_begin"]
    check = reconcile(broken)
    assert not check.passed
    assert "nav_begin" in check.detail


def test_absent_distribution_is_zero_only_inside_the_arithmetic(
    filing: Filing,
) -> None:
    check = reconcile(filing)
    assert check.passed
    assert "chg_distribution" in check.detail
    assert "chg_distribution" not in filing.values


# --- measurement error, the evidence for rule 4.1 -------------------------


@pytest.fixture()
def proxy(filing: Filing) -> dict:
    return proxy_divergence(filing)


def test_proxy_overstates_outflow_in_a_rising_week(
    filing: Filing, proxy: dict
) -> None:
    assert filing.gross_return > 0
    assert proxy["proxy_end"] < proxy["reported_net_flow"]


def test_proxy_error_exceeds_three_percent_in_one_week(proxy: dict) -> None:
    assert abs(proxy["proxy_end_error_pct"]) > 3.0


def test_midpoint_nav_reduces_but_does_not_remove_the_bias(proxy: dict) -> None:
    assert abs(proxy["proxy_mid_error_vnd"]) < abs(proxy["proxy_end_error_vnd"])
    assert proxy["proxy_mid_error_vnd"] != pytest.approx(0.0, abs=1.0)


def test_proxy_reports_gross_return(proxy: dict, filing: Filing) -> None:
    assert proxy["gross_return"] == pytest.approx(filing.gross_return)


# --- chain continuity -----------------------------------------------------


def _stub(
    fund_code: str, start: date, end: date, nav_begin: float, nav_end: float
) -> Filing:
    return Filing(
        fund_code=fund_code,
        period_start=start,
        period_end=end,
        values={
            "nav_begin": nav_begin,
            "nav_end": nav_end,
            "nav_per_unit_begin": 10_000.0,
            "nav_per_unit_end": 10_000.0,
            "chg_investment": nav_end - nav_begin,
        },
    )


def test_contiguous_filings_all_pass() -> None:
    first = _stub("AAA", date(2022, 11, 14), date(2022, 11, 18), 100.0, 110.0)
    second = _stub("AAA", date(2022, 11, 21), date(2022, 11, 25), 110.0, 120.0)
    checks = chain_continuity([first, second])
    assert len(checks) == 1
    assert all(c.passed for c in checks)


def test_mismatched_opening_flags_a_missed_filing() -> None:
    first = _stub("AAA", date(2022, 11, 14), date(2022, 11, 18), 100.0, 110.0)
    third = _stub("AAA", date(2022, 11, 28), date(2022, 12, 2), 130.0, 140.0)
    checks = chain_continuity([first, third])
    failed = [c for c in checks if not c.passed]
    assert len(failed) == 1
    assert "missed filing" in failed[0].detail


def test_continuity_is_per_fund() -> None:
    a1 = _stub("AAA", date(2022, 11, 14), date(2022, 11, 18), 100.0, 110.0)
    b1 = _stub("BBB", date(2022, 11, 14), date(2022, 11, 18), 500.0, 520.0)
    a2 = _stub("AAA", date(2022, 11, 21), date(2022, 11, 25), 110.0, 120.0)
    b2 = _stub("BBB", date(2022, 11, 21), date(2022, 11, 25), 520.0, 530.0)
    checks = chain_continuity([a1, b1, a2, b2])
    assert len(checks) == 2
    assert all(c.passed for c in checks)


def test_unordered_input_is_sorted_before_chaining() -> None:
    first = _stub("AAA", date(2022, 11, 14), date(2022, 11, 18), 100.0, 110.0)
    second = _stub("AAA", date(2022, 11, 21), date(2022, 11, 25), 110.0, 120.0)
    assert all(c.passed for c in chain_continuity([second, first]))


# --- summary --------------------------------------------------------------


def test_summarise_reports_failures(filing: Filing) -> None:
    broken = copy.deepcopy(filing)
    broken.values["subscriptions"] = 8_062_772_600.0
    text = summarise([reconcile(filing), reconcile(broken)])
    assert "1/2 passed" in text
    assert "FAIL" in text


def test_summarise_handles_empty() -> None:
    assert summarise([]) == "no checks run"
