"""Panel assembly: flow measures, macro joins, monthly rollup.

Design rules enforced here, from spec section 4:

  4.2  Gross legs stay separate all the way through. Net flow is derived.
  4.3  Flows are scaled by beginning-of-period NAV, never closing or average.
  4.4  Returns align to each filing's own period window, not a calendar week.
  4.5  Rows failing the identity are quarantined with a written reason, never
       dropped silently.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import unquote

import pandas as pd

from .appendix_xxiv import Filing
from .reconcile import (
    chain_continuity,
    check_net_flow_consistency,
    check_prior_column_consistency,
    proxy_divergence,
    reconcile,
)

__all__ = [
    "PanelResult",
    "deduplicate",
    "filename_report_date",
    "filename_date_conflict",
    "ADJUDICATED_FILENAME_DATE_ERRORS",
    "build_fund_period_panel",
    "attach_market_return",
    "attach_deposit_rate",
    "to_monthly",
]

log = logging.getLogger(__name__)

# A dealing period is a week for most funds and a day for some. Anything outside
# this range means the header dates were misread, not that a fund filed yearly.
MIN_PERIOD_DAYS = 0
MAX_PERIOD_DAYS = 35

FLOW_COLUMNS = [
    "gross_subscription_rate",
    "gross_redemption_rate",
    "net_flow_rate",
    "churn_rate",
    "flow_asymmetry",
]


def _normalize_fund_meta(fund_meta: dict[str, dict]) -> dict[str, dict]:
    """Normalize fund metadata codes to canonical forms.

    Handles DCBC/DCDE mapping: if input metadata maps both DCBC and DCDE to DCDE,
    ensure consistent canonical handling.
    """
    normalized = {}

    # Detect if both DCBC and DCDE exist and map to the same canonical code
    dcbc_meta = fund_meta.get("DCBC")
    dcde_meta = fund_meta.get("DCDE")

    for code, meta in fund_meta.items():
        # If DCBC and DCDE both exist and DCBC should map to DCDE
        if code == "DCBC" and dcde_meta is not None:
            # Check if metadata indicates they should be treated as same fund
            if dcbc_meta and dcde_meta:
                # Use DCDE as canonical, mark DCBC as alias
                meta_copy = meta.copy()
                meta_copy["canonical_code"] = "DCDE"
                meta_copy["is_alias"] = True
                normalized[code] = meta_copy
                continue

        meta_copy = meta.copy()
        meta_copy.setdefault("canonical_code", code)
        meta_copy.setdefault("is_alias", False)
        normalized[code] = meta_copy

    return normalized


@dataclass
class PanelResult:
    panel: pd.DataFrame
    superseded: pd.DataFrame
    quarantine: pd.DataFrame
    continuity_breaks: pd.DataFrame
    diagnostics: pd.DataFrame
    period_corrections: pd.DataFrame


def _derive_flow_measures(frame: pd.DataFrame) -> pd.DataFrame:
    """Add the scaled flow measures.

    Every rate uses `nav_begin` as the denominator. Using closing or average NAV
    would put the flow inside its own denominator and manufacture part of the
    flow-performance relationship the panel exists to measure.
    """
    frame = frame.copy()
    nav_begin = frame["nav_begin"].where(frame["nav_begin"] > 0)

    # A blank gross leg means one of two different things, and conflating them
    # would be a fabrication. When the disclosed net flow is zero, the fund
    # simply had no flow that period and zero is the right rate. When the net
    # flow is non-zero but the legs are blank, the manager did not disclose the
    # decomposition at all: SSIAM files a reduced Appendix XXIV with only line
    # 3.2. That is unknown, not zero, and must stay missing.
    legs_absent = frame["subscriptions"].isna() & frame["redemptions"].isna()
    no_flow = legs_absent & frame["net_flow"].fillna(0.0).eq(0.0)
    undisclosed = legs_absent & ~no_flow

    # ``gross_legs_disclosed`` is literal source semantics: both printed lines
    # must be present. A net-only zero is useful but remains an inference.
    both_present = frame["subscriptions"].notna() & frame["redemptions"].notna()
    subs = pd.to_numeric(frame["subscriptions"], errors="coerce").where(both_present)
    reds = pd.to_numeric(frame["redemptions"], errors="coerce").abs().where(both_present)

    frame["gross_subscription_rate"] = subs / nav_begin
    frame["gross_redemption_rate"] = reds / nav_begin
    frame["net_flow_rate"] = frame["net_flow"] / nav_begin
    frame["churn_rate"] = (subs + reds) / nav_begin
    frame["gross_legs_disclosed"] = both_present
    frame["gross_legs_inferred_zero"] = no_flow

    gross_total = subs + reds
    # Prevent division by zero in flow_asymmetry - compute safely
    asymmetry = pd.Series(index=frame.index, dtype="float64")
    nonzero_mask = gross_total > 0
    asymmetry[nonzero_mask] = (subs[nonzero_mask] - reds[nonzero_mask]) / gross_total[nonzero_mask]
    frame["flow_asymmetry"] = asymmetry
    return frame


_FILENAME_DATE_RE = re.compile(r"(20[12][0-9])([01][0-9])([0-3][0-9])")


def filename_report_date(source: str | None) -> date | None:
    """The dealing date a filing's own filename claims, if it carries one.

    Managers name these files after the period they close, so the filename is a
    second, independent statement of the date the printed header also makes.
    Where the two agree, which is 3,300 of the 3,528 rows that carry a parseable
    filename date, neither adds anything. Where they disagree, one of them is a
    typing mistake, and which one is not decidable from the filename alone: the
    2023 SSIBF files transpose day and month in the *filename* while the header
    is right, and two VCBF files carry the previous year in the filename.

    So this function only reports what the filename says. Deciding whose mistake
    it is belongs to `_reanchor_stale_periods`, which uses corroborating
    evidence rather than a preference for one source over the other.
    """
    if not source:
        return None
    text = unquote(str(source))
    best: date | None = None
    for match in _FILENAME_DATE_RE.finditer(text):
        year, month, day = (int(g) for g in match.groups())
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if date(2013, 1, 1) <= candidate <= date(2027, 12, 31):
            best = candidate
    return best


# Filings whose *filename* carries the wrong date while the printed reporting
# period is right, confirmed one by one against the documents on 2026-08-17.
# The panel keeps the printed period for every one of them; this list exists so
# that a conflict already looked at stays quiet while a new one does not.
#
#   DCBC_BC_TUAN_20221206      month typed as 12 for 01; period ends 2022-01-06
#   ...-TT98-2023030{7}/10/17/24  day and month transposed across four weeks
#   vcbbcf_bc_tuan_2024010{2,8}   year not rolled over at the new year
#   20210831-0906-*, 20240215-VLBF-*  filename names the whole span or the
#                                     publication date, so only the extractor
#                                     disagreed, never the document
ADJUDICATED_FILENAME_DATE_ERRORS = frozenset(
    {
        "DCBC_BC_TUAN_20221206.xlsx",
        "SSIBF_Bao-cao-ve-thay-doi-GTTSR-quy-mo-PLXXIV-TT98-20230307.pdf",
        "SSIBF_Bao-cao-ve-thay-doi-GTTSR-quy-mo-PLXXIV-TT98-20230310.pdf",
        "Copy of SSIBF_Bao-cao-ve-thay-doi-GTTSR-quy-mo-PLXXIV-TT98-20230317.pdf",
        "SSIBF_Bao-cao-ve-thay-doi-GTTSR-quy-mo-PLXXIV-TT98-20230324.pdf",
        "vcbbcf_bc_tuan_20240102.xlsx",
        "vcbbcf_bc_tuan_20240108.xlsx",
        "20210831-0906-veof-changes-of-nav-weekly-report.xlsx",
        "20210831-0906-vesaf-changes-of-nav-weekly-report.xlsx",
        "20210909-15-vlbf-changes-of-nav-weekly-report.xlsx",
        "20240215-VLBF-NAV-TUAN-TU-01.02.2024-den-07.02.2024.xlsx",
    }
)

# A filename and a header can disagree by a day or two for honest reasons: some
# managers name the file after the publication date rather than the dealing
# date. Beyond this the two are telling different stories.
FILENAME_DATE_TOLERANCE_DAYS = 4


def filename_date_conflict(source: str | None, period_end) -> bool:
    """Does the filename's date contradict the printed period it closes?

    Reported, never acted on. The two defects behind a conflict point opposite
    ways and only the document settles which: SSIAM has published a stale
    header over a correct filename, and separately a transposed filename over a
    correct header. `_reanchor_stale_periods` repairs the first because a
    collision corroborates it; this flag exists so the second is visible rather
    than silent.
    """
    stamped = filename_report_date(source)
    if stamped is None or period_end is None:
        return False
    return abs((stamped - period_end).days) > FILENAME_DATE_TOLERANCE_DAYS


def _reanchor_stale_periods(filings: list[Filing]) -> list[dict]:
    """Repair filings whose printed period is a stale copy of an earlier one.

    SSIAM's weekly template is edited from the previous week's file, and four
    times the figures were updated while the reporting-period line was not. The
    2025-11-10 filing prints "tuần từ 28/10/2025 đến 03/11/2025" and then gives
    the 4-10 November NAVs underneath it.

    Left alone this is worse than a missing week. Deduplication keys on the
    declared period, so the stale filing collides with the correctly labelled
    one for that week, supersedes it, and publishes the wrong figures under the
    right dates while its own week disappears entirely.

    The repair only fires on self-evident evidence: two or more filings claiming
    one period, where at least one filename agrees with the period it claims and
    another does not. That corroboration is what separates this from the
    opposite defect, a mistyped filename over a correct header, which never
    produces a collision and is therefore never touched here. The period is
    re-anchored to the filename's date, keeping the printed span, and every
    change is returned for the audit trail rather than applied quietly.
    """
    by_period: dict[tuple, list[Filing]] = {}
    for filing in filings:
        if filing.period_start is None or filing.period_end is None:
            continue
        by_period.setdefault(
            (filing.fund_code, filing.period_start, filing.period_end), []
        ).append(filing)

    corrections: list[dict] = []
    for (fund_code, start, end), group in by_period.items():
        if len({f.source for f in group}) < 2:
            continue
        dated = {id(f): filename_report_date(f.source) for f in group}
        if not any(d == end for d in dated.values()):
            continue
        span = end - start
        for filing in group:
            claimed = dated[id(filing)]
            if claimed is None or claimed == end:
                continue
            corrections.append(
                {
                    "fund_code": fund_code,
                    "source": filing.source,
                    "printed_period_start": start,
                    "printed_period_end": end,
                    "corrected_period_start": claimed - span,
                    "corrected_period_end": claimed,
                    "reason": (
                        "printed reporting period duplicates a filing whose own "
                        "filename matches it; re-anchored to this filing's "
                        "filename date, printed span preserved"
                    ),
                }
            )
            filing.period_start = claimed - span
            filing.period_end = claimed
    if corrections:
        log.warning(
            "re-anchored %d filing(s) whose printed period was stale; see "
            "period_corrections.csv",
            len(corrections),
        )
    return corrections


def deduplicate(
    filings: list[Filing],
) -> tuple[list[Filing], list[dict], list[dict], list[dict]]:
    """Collapse filings that describe the same fund-period.

    VCBF republishes a filing under a new sequence suffix without withdrawing
    the old one: vcbaif_bc_tuan_20250528_1.xlsx and _3.xlsx are byte-distinct
    files carrying identical figures. Two rows for one dealing period would
    double-count the flow and break chain continuity, so they collapse to one.

    When republished figures differ, the later suffix is treated as the
    restatement and kept. A later report can also extend the same opening
    boundary: DCBF published both 2025-01-17/21 and 2025-01-17/23 as weekly
    reports. The longer window supersedes the shorter cumulative snapshot;
    retaining both would count the shared days twice. Every displaced row is
    returned for the audit trail rather than discarded.

    Returns: (kept, superseded_rows, overlap_quarantine_rows, period_corrections)
    """
    period_corrections = _reanchor_stale_periods(filings)

    ordered = sorted(
        filings,
        key=lambda f: (
            f.fund_code or "",
            f.period_end or pd.Timestamp.min.date(),
            f.source or "",
        ),
    )

    chosen: dict[tuple, Filing] = {}
    superseded: list[dict] = []
    for filing in ordered:
        key = (filing.fund_code, filing.period_start, filing.period_end)
        previous = chosen.get(key)
        if previous is None:
            chosen[key] = filing
            continue
        identical = previous.values == filing.values
        chosen[key] = filing  # later source wins
        superseded.append(
            {
                **previous.as_row(),
                "superseded_by": filing.source,
                "reason": (
                    "duplicate republication, identical figures"
                    if identical
                    else "superseded by a restatement with different figures"
                ),
            }
        )

    by_opening: dict[tuple, list[Filing]] = {}
    undated: list[Filing] = []
    for filing in chosen.values():
        if filing.period_start is None or filing.period_end is None:
            undated.append(filing)
            continue
        by_opening.setdefault((filing.fund_code, filing.period_start), []).append(
            filing
        )

    kept = list(undated)
    for group in by_opening.values():
        winner = max(group, key=lambda f: (f.period_end, f.source or ""))
        kept.append(winner)
        for filing in group:
            if filing is winner:
                continue
            superseded.append(
                {
                    **filing.as_row(),
                    "superseded_by": winner.source,
                    "reason": (
                        "superseded by a later filing with the same opening "
                        "boundary and a longer period"
                    ),
                }
            )

    # Check for overlapping periods (not exact duplicates) - do this on final kept list
    overlap_quarantine: list[dict] = []
    by_fund: dict[str, list[Filing]] = {}
    for filing in kept:
        code = filing.fund_code or "_unknown"
        by_fund.setdefault(code, []).append(filing)

    for fund_code, fund_filings in by_fund.items():
        sorted_filings = sorted(
            fund_filings,
            key=lambda f: (f.period_start or pd.Timestamp.min.date(), f.period_end or pd.Timestamp.min.date())
        )

        to_remove_indices = set()
        for i, curr in enumerate(sorted_filings):
            if i in to_remove_indices or curr.period_start is None or curr.period_end is None:
                continue

            # Check for overlaps with subsequent filings
            for j, next_filing in enumerate(sorted_filings[i+1:], start=i+1):
                if j in to_remove_indices or next_filing.period_start is None or next_filing.period_end is None:
                    continue

                # Filing windows use inclusive dates, and many managers repeat
                # the prior closing boundary as the next opening boundary. That
                # shared boundary is supported and not an overlap; only a start
                # strictly before the preceding end is quarantined.
                if curr.period_end > next_filing.period_start and curr.period_start < next_filing.period_start:
                    overlap_quarantine.append({
                        **curr.as_row(),
                        "quarantine_reason": (
                            f"period overlap: {curr.period_start} to {curr.period_end} "
                            f"overlaps with {next_filing.period_start} to {next_filing.period_end}"
                        ),
                        "overlaps_with": next_filing.source,
                    })
                    to_remove_indices.add(i)
                    break

        # Remove quarantined filings from kept
        if to_remove_indices:
            kept = [f for f in kept if f not in [sorted_filings[idx] for idx in to_remove_indices]]

    return kept, superseded, overlap_quarantine, period_corrections


def build_fund_period_panel(
    filings: list[Filing], fund_meta: dict[str, dict] | None = None
) -> PanelResult:
    """Assemble the fund-period panel and its audit frames.

    A filing enters the panel only if it passes the reconciliation identity.
    Failures go to `quarantine` carrying the residual and a written reason, so
    the exclusion list is itself auditable.
    """
    # Normalize fund codes: canonical DCBC/DCDE handling
    if fund_meta:
        fund_meta = _normalize_fund_meta(fund_meta)
        canonical = {
            code: meta.get("canonical_code", code) for code, meta in fund_meta.items()
        }
        for filing in filings:
            filing.fund_code = canonical.get(filing.fund_code, filing.fund_code)

    filings, superseded, overlap_quarantine, period_corrections = deduplicate(filings)
    filings = sorted(
        filings,
        key=lambda filing: (
            filing.fund_code or "",
            filing.period_start or pd.Timestamp.min.date(),
            filing.period_end or pd.Timestamp.min.date(),
        ),
    )

    kept: list[dict] = []
    kept_filings: list[Filing] = []
    quarantined: list[dict] = []
    diagnostics: list[dict] = []
    last_accepted: dict[str | None, Filing] = {}

    for filing in filings:
        row = filing.as_row()
        identity = reconcile(filing)
        net_check = check_net_flow_consistency(filing)
        prior_check = check_prior_column_consistency(filing)

        row["reconcile_residual_vnd"] = identity.residual_vnd
        row["net_flow_residual_vnd"] = net_check.residual_vnd
        row["prior_column_warnings"] = prior_check.detail if "consistent" not in prior_check.detail else None
        row["filename_date_conflict"] = filename_date_conflict(
            filing.source, filing.period_end
        )

        diagnostic = proxy_divergence(filing)
        diagnostic["source"] = filing.source
        diagnostics.append(diagnostic)

        reasons = []
        if not identity.passed:
            reasons.append(f"identity: {identity.detail}")
        if not net_check.passed:
            reasons.append(f"net flow: {net_check.detail}")
        if row.get("nav_begin") in (None, 0) or pd.isna(row.get("nav_begin")):
            reasons.append("nav_begin missing or zero; flow rates undefined")
        if row.get("period_end") is None:
            reasons.append("period_end missing; cannot place row in time")
        if row.get("period_days") is not None and not (
            MIN_PERIOD_DAYS <= row["period_days"] <= MAX_PERIOD_DAYS
        ):
            reasons.append(
                f"period_days {row['period_days']} outside plausible range "
                f"[{MIN_PERIOD_DAYS}, {MAX_PERIOD_DAYS}]; header dates suspect"
            )

        # An internally consistent 10x/100x/1000x parse can satisfy the NAV
        # identity. For an adjacent filing, independently require its opening
        # NAV and NAV/unit to chain to the last accepted close. Calendar gaps
        # remain continuity warnings; extreme mismatches on a contiguous
        # boundary are quarantined.
        previous = last_accepted.get(filing.fund_code)
        if not reasons and previous and filing.period_start and previous.period_end:
            gap_days = (filing.period_start - previous.period_end).days
            if 0 <= gap_days <= 3:
                checks = (
                    ("nav", filing.values.get("nav_begin"), previous.values.get("nav_end")),
                    (
                        "nav_per_unit",
                        filing.values.get("nav_per_unit_begin"),
                        previous.values.get("nav_per_unit_end"),
                    ),
                )
                for check_name, current, prior in checks:
                    if current not in (None, 0) and prior not in (None, 0):
                        ratio = current / prior
                        if ratio < 0.2 or ratio > 5.0:
                            reasons.append(
                                f"contiguous {check_name} chain mismatch: opening/prior "
                                f"closing ratio {ratio:.6g} after {previous.period_end}"
                            )

        if reasons:
            quarantined.append({**row, "quarantine_reason": "; ".join(reasons)})
            continue
        kept.append(row)
        kept_filings.append(filing)
        last_accepted[filing.fund_code] = filing

    # Merge overlap quarantine with other quarantined rows
    quarantined.extend(overlap_quarantine)

    panel = pd.DataFrame(kept)
    if not panel.empty:
        panel = _derive_flow_measures(panel)
        if fund_meta:
            # Metadata may contain a legacy and canonical entry for one economic
            # code. Keep the canonical (non-alias) row for the many-to-one join.
            meta_rows = []
            for source_code, values in fund_meta.items():
                canonical_code = values.get("canonical_code", source_code)
                meta_rows.append({"fund_code": canonical_code, **values})
            meta = pd.DataFrame(meta_rows).sort_values("is_alias").drop_duplicates(
                "fund_code", keep="first"
            )
            panel = panel.merge(meta, on="fund_code", how="left")
        panel = panel.sort_values(["fund_code", "period_end"]).reset_index(drop=True)

    # Continuity is checked on the exact accepted objects. A quarantined filing
    # cannot bridge two panel rows: doing so would hide the gap created by its
    # exclusion and make continuity_breaks disagree with the published panel.
    # A filename that contradicts its own header is a source typo in one place
    # or the other, and only reading the document settles which. Those already
    # read are listed above and stay quiet; anything else is surfaced here so it
    # is adjudicated rather than inherited.
    unreviewed = sorted(
        {
            str(f.source).rsplit("/", 1)[-1]
            for f in kept_filings
            if filename_date_conflict(f.source, f.period_end)
            and unquote(str(f.source).rsplit("/", 1)[-1])
            not in ADJUDICATED_FILENAME_DATE_ERRORS
        }
    )
    if unreviewed:
        log.warning(
            "%d filing(s) whose filename date contradicts the printed period have "
            "not been adjudicated; the printed period is used. Review and add to "
            "ADJUDICATED_FILENAME_DATE_ERRORS: %s",
            len(unreviewed),
            ", ".join(unreviewed[:5]),
        )

    breaks = [c.as_row() for c in chain_continuity(kept_filings) if not c.passed]

    return PanelResult(
        panel=panel,
        superseded=pd.DataFrame(superseded),
        period_corrections=pd.DataFrame(period_corrections),
        quarantine=pd.DataFrame(quarantined),
        continuity_breaks=pd.DataFrame(breaks),
        diagnostics=pd.DataFrame(diagnostics),
    )


def attach_market_return(
    panel: pd.DataFrame, vnindex_daily: pd.DataFrame
) -> pd.DataFrame:
    """Join VN-Index return over each filing's own period window.

    Uses the last available close at or before each boundary date, so a boundary
    falling on a weekend or a Tet holiday does not create a gap. Aligning to a
    fixed calendar week instead would misalign flow and return by a day or more
    whenever a dealing period shifts around a public holiday.

    For contiguous periods within the same fund, uses the previous period's index_end
    as the current period's index_begin to eliminate compounding errors. Gaps or
    first periods fall back to asof lookup and are flagged.
    """
    if panel.empty:
        return panel.copy()
    if vnindex_daily is None or vnindex_daily.empty:
        raise ValueError("vnindex_daily is empty; cannot attach market return")

    index = vnindex_daily.rename(columns=str.lower)[["time", "close"]].copy()
    # Pin both sides to the same datetime resolution. A panel read back from CSV
    # parses to second resolution while the index series arrives in nanoseconds,
    # and pandas 3 refuses to merge_asof across differing units where pandas 2
    # silently coerced. Normalising here keeps the join working on both.
    index["time"] = pd.to_datetime(index["time"]).astype("datetime64[ns]")
    index = index.dropna(subset=["close"]).sort_values("time").reset_index(drop=True)

    out = panel.copy()

    # First pass: get index_end for all rows using asof logic
    keys = pd.to_datetime(out["period_end"]).astype("datetime64[ns]")
    frame = pd.DataFrame({"time": keys, "_row": range(len(out))}).sort_values("time")
    merged = pd.merge_asof(
        frame, index, on="time", direction="backward", allow_exact_matches=True
    )
    out["index_end"] = merged.sort_values("_row")["close"].to_numpy()

    # Second pass: set index_begin using contiguous logic
    out = out.sort_values(["fund_code", "period_end"]).reset_index(drop=True)
    out["index_begin"] = None
    out["market_boundary_source"] = None

    for fund_code in out["fund_code"].dropna().unique():
        fund_mask = out["fund_code"] == fund_code
        fund_rows = out[fund_mask].copy()

        for idx in fund_rows.index:
            if idx == 0 or out.loc[idx-1, "fund_code"] != fund_code:
                # First period for this fund: use asof lookup
                start_time = pd.to_datetime(out.loc[idx, "period_start"]).asm8
                asof_val = index[index["time"] <= start_time]
                if not asof_val.empty:
                    out.loc[idx, "index_begin"] = asof_val.iloc[-1]["close"]
                    out.loc[idx, "market_boundary_source"] = "first_period_asof"
            else:
                prev_idx = idx - 1
                # Check if contiguous (current start <= previous end + tolerance)
                curr_start = out.loc[idx, "period_start"]
                prev_end = out.loc[prev_idx, "period_end"]

                if pd.notna(curr_start) and pd.notna(prev_end):
                    gap_days = (curr_start - prev_end).days

                    if 0 <= gap_days <= 7:  # Contiguous or small gap
                        # Use previous period's index_end
                        out.loc[idx, "index_begin"] = out.loc[prev_idx, "index_end"]
                        out.loc[idx, "market_boundary_source"] = "contiguous"
                    else:
                        # Gap too large: use asof lookup and flag
                        start_time = pd.to_datetime(curr_start).asm8
                        asof_val = index[index["time"] <= start_time]
                        if not asof_val.empty:
                            out.loc[idx, "index_begin"] = asof_val.iloc[-1]["close"]
                            out.loc[idx, "market_boundary_source"] = f"gap_{gap_days}d_asof"
                else:
                    # Missing dates: fall back to asof
                    start_time = pd.to_datetime(out.loc[idx, "period_start"]).asm8
                    asof_val = index[index["time"] <= start_time]
                    if not asof_val.empty:
                        out.loc[idx, "index_begin"] = asof_val.iloc[-1]["close"]
                        out.loc[idx, "market_boundary_source"] = "missing_date_asof"

    # The index level at period_start is the last close at or before the first
    # dealing day, which is the level flows and NAV are struck against.
    out["market_return"] = out["index_end"] / out["index_begin"] - 1.0

    # Preserve gross_return compatibility but use total_return for excess when available
    if "total_return" in out.columns:
        out["excess_return"] = out["total_return"] - out["market_return"]
        out["excess_return_gross"] = out["gross_return"] - out["market_return"]
    else:
        out["excess_return"] = out["gross_return"] - out["market_return"]

    return out


def attach_deposit_rate(
    panel: pd.DataFrame, deposit_monthly: pd.DataFrame
) -> pd.DataFrame:
    """Join the monthly deposit rate on each row's period-end month.

    The `provenance` column is carried into the panel deliberately. The series is
    hand-curated, not scraped, and the panel should make that visible at the row
    level rather than burying it in documentation.
    """
    if panel.empty:
        return panel.copy()
    if deposit_monthly is None or deposit_monthly.empty:
        raise ValueError("deposit_monthly is empty; cannot attach deposit rate")

    rates = deposit_monthly.copy()
    rates["month"] = pd.PeriodIndex(pd.to_datetime(rates["month"]), freq="M")
    keep = ["month", "rate_pct", "provenance"]
    for optional in ("rate_low_pct", "rate_high_pct", "tenor", "source", "bank"):
        if optional in rates.columns:
            keep.append(optional)
    rates = rates[keep].rename(
        columns={
            "rate_pct": "deposit_rate_pct",
            "provenance": "deposit_rate_provenance",
            "rate_low_pct": "deposit_rate_low_pct",
            "rate_high_pct": "deposit_rate_high_pct",
            "tenor": "deposit_rate_tenor",
            "source": "deposit_rate_source",
            "bank": "deposit_rate_bank",
        }
    )

    out = panel.copy()
    out["month"] = pd.PeriodIndex(pd.to_datetime(out["period_end"]), freq="M")
    out = out.merge(rates, on="month", how="left")
    out["month"] = out["month"].astype(str)
    return out


def to_monthly(panel: pd.DataFrame) -> pd.DataFrame:
    """Roll the fund-period panel up to fund-month.

    Flows sum, returns compound, and NAV is taken from the month's first opening
    and last closing. `reconcile_residual_vnd` therefore exposes any missing or
    excluded period inside a month instead of implying that every rollup closes.
    A gap crossing a month boundary can still have zero monthly residual, so
    continuity_breaks.csv remains the authoritative completeness audit.
    """
    if panel.empty:
        return panel.copy()

    frame = panel.copy()
    frame["period_end"] = pd.to_datetime(frame["period_end"])
    frame["period_start"] = pd.to_datetime(frame["period_start"])
    frame["month"] = frame["period_end"].dt.to_period("M")
    frame = frame.sort_values(["fund_code", "period_end"])

    def _compound(series: pd.Series) -> float:
        clean = series.dropna()
        if clean.empty:
            return float("nan")
        return float((1.0 + clean).prod() - 1.0)

    grouped = frame.groupby(["fund_code", "month"], sort=True)
    monthly = grouped.agg(
        period_start=("period_start", "min"),
        period_end=("period_end", "max"),
        n_periods=("period_end", "size"),
        nav_begin=("nav_begin", "first"),
        nav_end=("nav_end", "last"),
        nav_per_unit_begin=("nav_per_unit_begin", "first"),
        nav_per_unit_end=("nav_per_unit_end", "last"),
        subscriptions=("subscriptions", "sum"),
        redemptions=("redemptions", "sum"),
        net_flow=("net_flow", "sum"),
        chg_investment=("chg_investment", "sum"),
        chg_distribution=("chg_distribution", "sum"),
        period_days=("period_days", "sum"),
    ).reset_index()

    # `sum` returns 0.0 for an all-missing group, which would turn a manager's
    # undisclosed gross legs into a disclosed zero and defeat the guard in
    # `_derive_flow_measures`. A month inherits the weaker of its periods: if
    # any period inside it withheld the decomposition, the month's legs are
    # unknown, and the same rule downstream decides whether unknown-with-no-net
    # is simply a quiet month.
    if "gross_legs_disclosed" in frame.columns:
        undisclosed = (
            grouped["gross_legs_disclosed"]
            .apply(lambda s: bool((~s.fillna(False)).any()))
            .to_numpy()
        )
        monthly.loc[undisclosed, ["subscriptions", "redemptions"]] = float("nan")

    monthly["gross_return"] = grouped["gross_return"].apply(_compound).to_numpy()

    # Compound total_return if available
    if "total_return" in frame.columns:
        monthly["total_return"] = grouped["total_return"].apply(_compound).to_numpy()

    monthly["reconcile_residual_vnd"] = monthly["nav_end"] - (
        monthly["nav_begin"]
        + monthly["chg_investment"].fillna(0.0)
        + monthly["net_flow"].fillna(0.0)
        + pd.to_numeric(monthly["chg_distribution"], errors="coerce").fillna(0.0)
    )
    if "market_return" in frame.columns:
        monthly["market_return"] = grouped["market_return"].apply(_compound).to_numpy()
        # Use total_return for excess if available, otherwise gross_return
        if "total_return" in monthly.columns:
            monthly["excess_return"] = monthly["total_return"] - monthly["market_return"]
            monthly["excess_return_gross"] = monthly["gross_return"] - monthly["market_return"]
        else:
            monthly["excess_return"] = monthly["gross_return"] - monthly["market_return"]
    for column in ("deposit_rate_pct", "deposit_rate_provenance", "asset_class",
                   "fund_name", "manager_id"):
        if column in frame.columns:
            monthly[column] = grouped[column].last().to_numpy()

    monthly = _derive_flow_measures(monthly)
    monthly["month"] = monthly["month"].astype(str)
    return monthly
