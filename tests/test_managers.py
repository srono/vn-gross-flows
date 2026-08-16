"""Per-manager fixtures, one real filing each, as spec section 10 step 7 requires.

The point of these is that a single parser reads the Ministry of Finance
template across managers. Each fixture also pins the one thing that manager does
differently:

  dcds_20250116.xlsx  DCVFM, standard weekly XLSX with full gross legs
  dcds_daily_20260813.xlsx
                         DCVFM's point-in-time look-alike, which must be rejected
  vesaf_20260713.xlsx    VinaCapital, weekly XLSX with full gross legs
  ssibf_20231211.pdf     SSIAM, reduced Appendix XXIV with no gross legs at all,
                         and a period written numerically in Vietnamese
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from vngross.appendix_xxiv import ParseError, parse_filing
from vngross.reconcile import check_net_flow_consistency, reconcile

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture(scope="module")
def vesaf():
    return parse_filing(FIXTURES / "vesaf_20260713.xlsx", fund_code="VINACAPITAL-VESAF")


@pytest.fixture(scope="module")
def ssibf():
    return parse_filing(FIXTURES / "ssibf_20231211.pdf", fund_code="SSIBF")


@pytest.fixture(scope="module")
def dcds():
    return parse_filing(FIXTURES / "dcds_20250116.xlsx", fund_code="DCDS")


# --- DCVFM ----------------------------------------------------------------


def test_dcvfm_identity_closes(dcds) -> None:
    check = reconcile(dcds)
    assert check.passed
    assert check.residual_vnd == 0.0


def test_dcvfm_gross_legs_keep_their_signs(dcds) -> None:
    assert dcds.values["subscriptions"] > 0
    assert dcds.values["redemptions"] < 0
    assert check_net_flow_consistency(dcds).passed


def test_dcvfm_daily_nav_report_is_rejected() -> None:
    with pytest.raises(ParseError, match="no section-3 .* point-in-time NAV report"):
        parse_filing(FIXTURES / "dcds_daily_20260813.xlsx", fund_code="DCDS")


# --- VinaCapital ----------------------------------------------------------


def test_vinacapital_identity_closes(vesaf) -> None:
    check = reconcile(vesaf)
    assert check.passed
    assert abs(check.residual_vnd) < 1.0


def test_vinacapital_discloses_gross_legs(vesaf) -> None:
    """VinaCapital files the full template, so the gross split survives."""
    assert vesaf.values["subscriptions"] > 0
    assert vesaf.values["redemptions"] < 0
    assert check_net_flow_consistency(vesaf).passed


def test_vinacapital_dates(vesaf) -> None:
    assert vesaf.period_start == date(2026, 7, 7)
    assert vesaf.period_end == date(2026, 7, 13)
    assert vesaf.period_days == 6


# --- SSIAM ----------------------------------------------------------------


def test_ssiam_identity_closes_on_the_net_line(ssibf) -> None:
    """SSIAM omits 3.2.1 and 3.2.2, so the identity must close off line 3.2.

    Requiring the gross legs here would quarantine a perfectly valid filing over
    a disclosure choice its manager made.
    """
    check = reconcile(ssibf)
    assert check.passed
    assert abs(check.residual_vnd) < 1.0
    assert "net line 3.2" in check.detail


def test_ssiam_has_no_gross_legs(ssibf) -> None:
    assert "chg_flows_net" in ssibf.values
    assert "subscriptions" not in ssibf.values
    assert "redemptions" not in ssibf.values


def test_ssiam_net_flow_falls_back_to_the_disclosed_line(ssibf) -> None:
    assert ssibf.net_flow == pytest.approx(ssibf.values["chg_flows_net"])


def test_ssiam_numeric_vietnamese_period(ssibf) -> None:
    """Period reads "tuan tu 05/12/2023 den 11/12/2023"."""
    assert ssibf.period_start == date(2023, 12, 5)
    assert ssibf.period_end == date(2023, 12, 11)


def test_undisclosed_gross_legs_are_missing_not_zero(vesaf, ssibf) -> None:
    """A manager that does not disclose the split must not read as zero flow."""
    import pandas as pd

    from vngross.panel import build_fund_period_panel

    panel = build_fund_period_panel([vesaf, ssibf]).panel
    rows = panel.set_index("fund_code")

    assert bool(rows.loc["VINACAPITAL-VESAF", "gross_legs_disclosed"])
    assert not bool(rows.loc["SSIBF", "gross_legs_disclosed"])

    # SSIAM had a real, non-zero net flow that period, so a zero gross rate
    # would be a fabricated number rather than an absent one.
    assert rows.loc["SSIBF", "net_flow_rate"] != 0
    assert pd.isna(rows.loc["SSIBF", "gross_subscription_rate"])
    assert pd.isna(rows.loc["SSIBF", "gross_redemption_rate"])
    assert pd.isna(rows.loc["SSIBF", "flow_asymmetry"])
    assert rows.loc["VINACAPITAL-VESAF", "gross_subscription_rate"] > 0
