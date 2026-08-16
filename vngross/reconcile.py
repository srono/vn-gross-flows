"""Quality gates for parsed filings.

The reconciliation identity is the core quality control of the entire build:

    nav_end = nav_begin + chg_investment + subscriptions + redemptions
              + chg_distribution

It closes to zero dong on a correctly parsed filing. A non-zero residual is a
defect, never noise.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from .appendix_xxiv import Filing, ParseError

__all__ = [
    "ABS_TOL_VND",
    "REL_TOL",
    "Check",
    "reconcile",
    "check_net_flow_consistency",
    "proxy_divergence",
    "chain_continuity",
    "summarise",
]

# Tolerance covers float representation of ~1e14 dong magnitudes, nothing more.
ABS_TOL_VND = 5.0
REL_TOL = 1e-9

_IDENTITY_TERMS = (
    "chg_investment",
    "subscriptions",
    "redemptions",
    "chg_distribution",
)

# The flow term of the identity, in order of preference. The gross legs are the
# primary form, but not every manager discloses them: SSIAM files a reduced
# Appendix XXIV carrying only the combined line 3.2, with no 3.2.1 or 3.2.2. The
# identity must close off whichever form the filing actually provides, or a
# perfectly valid filing is quarantined for a disclosure choice its manager made.
_GROSS_LEGS = ("subscriptions", "redemptions")


@dataclass
class Check:
    fund_code: str | None
    period_end: date | None
    passed: bool
    residual_vnd: float
    detail: str
    check: str = "reconcile"

    def as_row(self) -> dict:
        return {
            "fund_code": self.fund_code,
            "period_end": self.period_end,
            "check": self.check,
            "passed": self.passed,
            "residual_vnd": self.residual_vnd,
            "detail": self.detail,
        }


def _within_tolerance(residual: float, scale: float) -> bool:
    return abs(residual) <= max(ABS_TOL_VND, REL_TOL * abs(scale))


def reconcile(filing: Filing) -> Check:
    """Gate a filing on the NAV reconciliation identity."""
    values = filing.values
    missing = [k for k in ("nav_begin", "nav_end") if k not in values]
    if missing:
        return Check(
            fund_code=filing.fund_code,
            period_end=filing.period_end,
            passed=False,
            residual_vnd=float("nan"),
            detail=f"missing required line(s): {', '.join(missing)}",
        )

    nav_begin = values["nav_begin"]
    nav_end = values["nav_end"]

    # A blank cell is absent, not zero. Substitute zero only here, inside the
    # arithmetic, never at parse time.
    has_gross = any(leg in values for leg in _GROSS_LEGS)
    if has_gross:
        flow = sum(values.get(leg, 0.0) for leg in _GROSS_LEGS)
        flow_form = "gross legs 3.2.1+3.2.2"
    else:
        flow = values.get("chg_flows_net", 0.0)
        flow_form = "net line 3.2"

    total = (
        nav_begin
        + values.get("chg_investment", 0.0)
        + flow
        + values.get("chg_distribution", 0.0)
    )
    residual = nav_end - total
    passed = _within_tolerance(residual, nav_end)

    terms = ("chg_investment", "chg_distribution") + (
        _GROSS_LEGS if has_gross else ("chg_flows_net",)
    )
    absent = [term for term in terms if term not in values]
    note = f" (absent, treated as 0: {', '.join(absent)})" if absent else ""
    note += f" [flow from {flow_form}]"
    detail = (
        f"nav_end {nav_end:,.0f} vs identity {total:,.0f}, "
        f"residual {residual:,.2f} VND{note}"
    )
    return Check(
        fund_code=filing.fund_code,
        period_end=filing.period_end,
        passed=passed,
        residual_vnd=residual,
        detail=detail,
        check="reconcile",
    )


def check_net_flow_consistency(filing: Filing) -> Check:
    """Check the disclosed net flow line 3.2 against its gross legs."""
    values = filing.values
    net = values.get("chg_flows_net")
    subs = values.get("subscriptions")
    reds = values.get("redemptions")

    if net is None or (subs is None and reds is None):
        return Check(
            fund_code=filing.fund_code,
            period_end=filing.period_end,
            passed=True,
            residual_vnd=0.0,
            detail="net or both gross legs absent, nothing to cross-check",
            check="net_flow_consistency",
        )

    gross_sum = (subs or 0.0) + (reds or 0.0)
    residual = net - gross_sum
    passed = _within_tolerance(residual, net)
    return Check(
        fund_code=filing.fund_code,
        period_end=filing.period_end,
        passed=passed,
        residual_vnd=residual,
        detail=(
            f"3.2 {net:,.0f} vs 3.2.1+3.2.2 {gross_sum:,.0f}, "
            f"residual {residual:,.2f} VND"
        ),
        check="net_flow_consistency",
    )


def proxy_divergence(filing: Filing) -> dict:
    """Quantify the error in estimating flows from changes in units outstanding.

    Diagnostic only. The proxy `change in units x NAV per unit` is never a panel
    value: flows transact at intra-period dealing NAVs, so the proxy's error
    tracks the period return and would make measurement error in the dependent
    variable correlated with the main regressor.
    """
    out: dict = {
        "fund_code": filing.fund_code,
        "period_end": filing.period_end,
        "gross_return": filing.gross_return,
        "units_begin": filing.units_begin,
        "units_end": filing.units_end,
        "reported_net_flow": None,
        "proxy_end": None,
        "proxy_mid": None,
        "proxy_end_error_vnd": None,
        "proxy_mid_error_vnd": None,
        "proxy_end_error_pct": None,
        "proxy_mid_error_pct": None,
    }

    try:
        reported = filing.net_flow
    except ParseError:
        return out
    out["reported_net_flow"] = reported

    units_begin = filing.units_begin
    units_end = filing.units_end
    nav_pu_begin = filing.values.get("nav_per_unit_begin")
    nav_pu_end = filing.values.get("nav_per_unit_end")
    if units_begin is None or units_end is None or nav_pu_begin is None:
        return out

    delta_units = units_end - units_begin
    proxy_end = delta_units * nav_pu_end
    proxy_mid = delta_units * (nav_pu_begin + nav_pu_end) / 2.0

    out["delta_units"] = delta_units
    out["proxy_end"] = proxy_end
    out["proxy_mid"] = proxy_mid
    out["proxy_end_error_vnd"] = proxy_end - reported
    out["proxy_mid_error_vnd"] = proxy_mid - reported
    if reported:
        scale = abs(reported)
        out["proxy_end_error_pct"] = (proxy_end - reported) / scale * 100.0
        out["proxy_mid_error_pct"] = (proxy_mid - reported) / scale * 100.0
    return out


def chain_continuity(filings: list[Filing]) -> list[Check]:
    """Verify each fund's opening NAV equals the prior filing's closing NAV.

    A break means a filing is missing from the sample. That matters because an
    unnoticed gap turns a missing week into a fabricated flow: the next filing's
    opening NAV silently absorbs the unobserved period.
    """
    by_fund: dict[str | None, list[Filing]] = defaultdict(list)
    for filing in filings:
        by_fund[filing.fund_code].append(filing)

    checks: list[Check] = []
    for fund_code, group in by_fund.items():
        ordered = sorted(group, key=lambda f: (f.period_end or date.min))
        for prev, curr in zip(ordered, ordered[1:]):
            prev_end = prev.values.get("nav_end")
            curr_begin = curr.values.get("nav_begin")
            if prev_end is None or curr_begin is None:
                checks.append(
                    Check(
                        fund_code=fund_code,
                        period_end=curr.period_end,
                        passed=False,
                        residual_vnd=float("nan"),
                        detail="cannot chain: nav_end or nav_begin absent",
                        check="chain_continuity",
                    )
                )
                continue

            residual = curr_begin - prev_end
            passed = _within_tolerance(residual, prev_end)
            if passed:
                detail = (
                    f"opening {curr_begin:,.0f} matches prior closing "
                    f"{prev_end:,.0f}"
                )
            else:
                detail = (
                    f"opening {curr_begin:,.0f} does not match prior closing "
                    f"{prev_end:,.0f} (gap {residual:,.0f} VND); likely a missed "
                    f"filing between {prev.period_end} and {curr.period_start}"
                )
            checks.append(
                Check(
                    fund_code=fund_code,
                    period_end=curr.period_end,
                    passed=passed,
                    residual_vnd=residual,
                    detail=detail,
                    check="chain_continuity",
                )
            )
    return checks


def summarise(checks: list[Check]) -> str:
    """Human-readable roll-up, grouped by check name."""
    if not checks:
        return "no checks run"

    by_check: dict[str, list[Check]] = defaultdict(list)
    for check in checks:
        by_check[check.check].append(check)

    lines: list[str] = []
    for name, group in sorted(by_check.items()):
        failed = [c for c in group if not c.passed]
        worst = max(
            (abs(c.residual_vnd) for c in group if c.residual_vnd == c.residual_vnd),
            default=0.0,
        )
        lines.append(
            f"{name}: {len(group) - len(failed)}/{len(group)} passed, "
            f"max abs residual {worst:,.2f} VND"
        )
        for check in failed[:10]:
            lines.append(f"  FAIL {check.fund_code} {check.period_end}: {check.detail}")
        if len(failed) > 10:
            lines.append(f"  ... and {len(failed) - 10} more failures")
    return "\n".join(lines)
