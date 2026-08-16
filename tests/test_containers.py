"""Tests for the XLSX reader and the bilingual header, against real filings.

Each fixture is a real filing that broke an assumption in the build spec:

  vcbbcf_20260729.xlsx                  the XLSX container VCBF moved to in
                                        December 2022, and line 6.3 stored as a
                                        fraction rather than printed as a percent
  vcbmgf_20250108.xlsx                  the compressed header that states the
                                        year once, which left every MGF filing
                                        undated until it was handled
  vcbbcf_20240103_en_header_error.xlsx  a filing whose English header is
                                        mistyped, where trusting English over
                                        Vietnamese dates the row eleven months
                                        late
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from vngross.appendix_xxiv import ParseError, parse_filing, parse_xlsx
from vngross.reconcile import check_net_flow_consistency, reconcile

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture(scope="module")
def xlsx():
    return parse_xlsx(FIXTURES / "vcbbcf_20260729.xlsx", fund_code="VCBF-BCF")


@pytest.fixture(scope="module")
def compressed_header():
    return parse_xlsx(FIXTURES / "vcbmgf_20250108.xlsx", fund_code="VCBF-MGF")


@pytest.fixture(scope="module")
def bad_english_header():
    return parse_xlsx(
        FIXTURES / "vcbbcf_20240103_en_header_error.xlsx", fund_code="VCBF-BCF"
    )


# --- the XLSX container ----------------------------------------------------


def test_xlsx_identity_closes(xlsx) -> None:
    """The same identity that gates the PDFs gates the XLSX filings."""
    check = reconcile(xlsx)
    assert check.passed
    assert abs(check.residual_vnd) < 1.0


def test_xlsx_net_flow_consistent(xlsx) -> None:
    assert check_net_flow_consistency(xlsx).passed


def test_xlsx_dates(xlsx) -> None:
    assert xlsx.period_start == date(2026, 7, 23)
    assert xlsx.period_end == date(2026, 7, 29)
    assert xlsx.report_date == date(2026, 7, 30)
    assert xlsx.period_days == 6


def test_xlsx_gross_legs_keep_their_signs(xlsx) -> None:
    assert xlsx.values["subscriptions"] > 0
    assert xlsx.values["redemptions"] < 0
    assert xlsx.net_flow == pytest.approx(
        xlsx.values["subscriptions"] + xlsx.values["redemptions"]
    )


def test_xlsx_blank_cell_is_absent_not_zero(xlsx) -> None:
    """Line 3.3 is blank in this filing, as it is in the PDF fixture."""
    assert "chg_distribution" not in xlsx.values


def test_xlsx_section_code_rows_are_skipped(xlsx) -> None:
    """Codes 1, 2, 3, 6 sit in the code column as numbers, not strings."""
    for name in ("nav_begin", "nav_end", "chg_investment"):
        assert name in xlsx.values


def test_xlsx_percent_field_is_scaled_to_percent_units(xlsx) -> None:
    """XLSX stores 6.3 as a fraction; the PDF prints it as 20.54%.

    Without normalisation the panel would carry foreign ownership 100x too small
    for the 90% of the archive that is XLSX.
    """
    printed = xlsx.values["foreign_ownership_pct"]
    assert 1.0 < printed < 100.0
    # Cross-check against the same quantity derived from two other lines.
    assert printed == pytest.approx(xlsx.foreign_share_of_nav, abs=0.05)


def test_dispatch_reads_container_from_magic_bytes(xlsx) -> None:
    """The cache names files by content hash, so extensions cannot be trusted."""
    assert parse_filing(FIXTURES / "vcbbcf_20260729.xlsx").values == xlsx.values


def test_non_appendix_xxiv_workbook_raises(tmp_path: Path) -> None:
    import openpyxl

    book = openpyxl.Workbook()
    book.active["A1"] = "not a filing"
    path = tmp_path / "other.xlsx"
    book.save(path)
    with pytest.raises(ParseError):
        parse_xlsx(path)


# --- the bilingual header --------------------------------------------------


def test_compressed_header_is_dated(compressed_header) -> None:
    """"From 02 Jan to 08 Jan 2025" states the year once."""
    assert compressed_header.period_start == date(2025, 1, 2)
    assert compressed_header.period_end == date(2025, 1, 8)
    assert compressed_header.period_days == 6


def test_compressed_header_still_reconciles(compressed_header) -> None:
    assert reconcile(compressed_header).passed


def test_vietnamese_header_wins_over_mistyped_english(bad_english_header) -> None:
    """The filing reads "From 28 Dec 2023 to 03 Dec 2024"; Vietnamese says 03/01/2024.

    The spec directs parsing from the English half. Doing so here would date the
    row eleven months late and misalign its return window.
    """
    assert bad_english_header.period_start == date(2023, 12, 28)
    assert bad_english_header.period_end == date(2024, 1, 3)
    assert bad_english_header.period_days == 6


def test_header_disagreement_is_recorded_not_hidden(bad_english_header) -> None:
    assert bad_english_header.date_conflict is not None
    assert "2024-01-03" in bad_english_header.date_conflict
    assert "2024-12-03" in bad_english_header.date_conflict


def test_clean_filings_carry_no_date_conflict(xlsx, compressed_header) -> None:
    assert xlsx.date_conflict is None
    assert compressed_header.date_conflict is None


# --- parse_many collects failures rather than raising ---------------------


def test_parse_many_collects_failures_without_aborting(tmp_path: Path) -> None:
    """A malformed filing must not abort a multi-year run.

    The two truncated VCBF workbooks of 2025-11-26 are exactly this case: the
    server serves a short file with a matching Content-Length, so it can only be
    detected at parse time.
    """
    from vngross.appendix_xxiv import parse_many

    truncated = tmp_path / "truncated.xlsx"
    truncated.write_bytes(b"PK\x03\x04" + b"\x00" * 200)
    scanned = tmp_path / "no_text.pdf"
    scanned.write_bytes(b"not a pdf at all")

    filings, failures = parse_many(
        [
            (str(FIXTURES / "vcbbcf_20260729.xlsx"), "VCBF-BCF"),
            (str(truncated), "VCBF-AIF"),
            (str(scanned), "VCBF-FIF"),
            (str(FIXTURES / "vcbmgf_20250108.xlsx"), "VCBF-MGF"),
        ]
    )

    assert len(filings) == 2
    assert {f.fund_code for f in filings} == {"VCBF-BCF", "VCBF-MGF"}
    assert len(failures) == 2
    for path, reason in failures:
        assert reason  # every failure carries a diagnosable reason
        assert ":" in reason


def test_parse_many_honours_the_supplied_fund_code() -> None:
    """The registry's code wins over the code printed in the document.

    The 2026 filing prints "VCBF-BCF" while the 2022 filings print "VCBBCF";
    the caller supplies the stable identity.
    """
    from vngross.appendix_xxiv import parse_many

    filings, failures = parse_many(
        [(str(FIXTURES / "vcbbcf_20260729.xlsx"), "vcbbcf-canonical")]
    )
    assert not failures
    assert filings[0].fund_code == "vcbbcf-canonical"
