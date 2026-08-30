"""Acceptance tests for the Appendix XXIV parser, per spec section 8."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from vngross.appendix_xxiv import (
    FIELD_MAP,
    SECTION_CODES,
    Filing,
    ParseError,
    parse_number,
    parse_filing,
    detect_convention,
    parse_text,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "vcbbcf_20221121.txt"


@pytest.fixture(scope="module")
def fixture_text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture()
def filing(fixture_text: str) -> Filing:
    return parse_text(fixture_text, source="fixtures/vcbbcf_20221121.txt")


# --- number parsing -------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("302,234,351,773", 302234351773.0),
        ("(96,819,279)", -96819279.0),
        ("21,439.31", 21439.31),
        ("(78.44)", -78.44),
        ("20.54%", 20.54),
    ],
)
def test_parse_number(token: str, expected: float) -> None:
    assert parse_number(token) == pytest.approx(expected)


@pytest.mark.parametrize("token", ["n/a", "", "   ", "-", "N/A", None])
def test_parse_number_rejects_non_numeric(token) -> None:
    with pytest.raises(ParseError):
        parse_number(token)


# --- field mapping --------------------------------------------------------


def test_dates(filing: Filing) -> None:
    assert filing.period_start == date(2022, 11, 17)
    assert filing.period_end == date(2022, 11, 21)
    assert filing.report_date == date(2022, 11, 22)


def test_fund_code_extracted_from_bilingual_name(filing: Filing) -> None:
    assert filing.fund_code == "VCBBCF"


def test_nav_levels(filing: Filing) -> None:
    assert filing.values["nav_begin"] == pytest.approx(302_234_351_773)
    assert filing.values["nav_end"] == pytest.approx(312_389_199_705)
    assert filing.values["nav_per_unit_begin"] == pytest.approx(21_439.31)
    assert filing.values["nav_per_unit_end"] == pytest.approx(22_160.84)


def test_flow_lines(filing: Filing) -> None:
    assert filing.values["chg_investment"] == pytest.approx(10_171_039_485)
    assert filing.values["subscriptions"] == pytest.approx(80_627_726)
    assert filing.values["redemptions"] == pytest.approx(-96_819_279)
    assert filing.values["chg_flows_net"] == pytest.approx(-16_191_553)


def test_gross_legs_carry_their_signs(filing: Filing) -> None:
    assert filing.values["subscriptions"] > 0
    assert filing.values["redemptions"] < 0


def test_prior_period_column_is_captured(filing: Filing) -> None:
    assert filing.prior_values["nav_end"] == pytest.approx(302_234_351_773)
    assert filing.prior_values["subscriptions"] == pytest.approx(146_183_850)
    assert filing.prior_values["chg_investment"] == pytest.approx(-1_105_852_259)


def test_prior_closing_equals_this_opening(filing: Filing) -> None:
    """The template's own free consistency check."""
    assert filing.prior_values["nav_end"] == pytest.approx(filing.values["nav_begin"])


def test_section_codes_are_not_mapped(filing: Filing) -> None:
    for code in SECTION_CODES:
        assert code not in FIELD_MAP
    # No value in the filing originates from a section header row.
    assert filing.values["chg_investment"] != filing.values["nav_begin"]


def test_blank_cell_is_absent_not_zero(filing: Filing) -> None:
    """Line 3.3 is blank on the fixture. Absent means absent."""
    assert "chg_distribution" not in filing.values
    assert filing.values.get("chg_distribution") is None


def test_remaining_template_lines(filing: Filing) -> None:
    assert filing.values["chg_nav_per_unit"] == pytest.approx(721.53)
    assert filing.values["nav_52w_high"] == pytest.approx(31_723.28)
    assert filing.values["nav_52w_low"] == pytest.approx(21_439.31)
    assert filing.values["foreign_units"] == pytest.approx(2_885_796.11)
    assert filing.values["foreign_value"] == pytest.approx(63_951_665_866)
    assert filing.values["foreign_ownership_pct"] == pytest.approx(20.54)


def test_bilingual_header_row_4_does_not_shadow_line_4(fixture_text: str) -> None:
    """'4 Ky bao cao: ...' must not be read as line code 4."""
    filing = parse_text(fixture_text)
    assert filing.values["chg_nav_per_unit"] == pytest.approx(721.53)


def test_missing_line_2_1_raises(fixture_text: str) -> None:
    stripped = "\n".join(
        line for line in fixture_text.splitlines() if not line.startswith("2.1 ")
    )
    with pytest.raises(ParseError):
        parse_text(stripped)


def test_empty_text_raises() -> None:
    with pytest.raises(ParseError):
        parse_text("   \n\n")


# --- derived properties ---------------------------------------------------


def test_net_flow_prefers_disclosed_net(filing: Filing) -> None:
    assert filing.net_flow == pytest.approx(-16_191_553)


def test_net_flow_falls_back_to_gross_legs(filing: Filing) -> None:
    del filing.values["chg_flows_net"]
    assert filing.net_flow == pytest.approx(80_627_726 - 96_819_279)


def test_units_outstanding(filing: Filing) -> None:
    assert filing.units_begin == pytest.approx(14_097_205.17, abs=0.05)
    assert filing.units_end == pytest.approx(14_096_451.2, abs=0.05)


def test_gross_return_positive_on_fixture(filing: Filing) -> None:
    assert filing.gross_return is not None
    assert filing.gross_return > 0


def test_period_days(filing: Filing) -> None:
    assert filing.period_days == 4


def test_as_row_flattens_both_columns(filing: Filing) -> None:
    row = filing.as_row()
    assert row["fund_code"] == "VCBBCF"
    assert row["nav_end"] == pytest.approx(312_389_199_705)
    assert row["prior_nav_end"] == pytest.approx(302_234_351_773)
    assert row["chg_distribution"] is None
    assert row["net_flow"] == pytest.approx(-16_191_553)
    assert row["source"] == "fixtures/vcbbcf_20221121.txt"


# --- number conventions ---------------------------------------------------
#
# The corpus is not written in one convention, and a single filing is not
# either. The November 2025 SSIBF template prints NAV as "103,228,319,952" and
# the change in NAV, on the same page, as "(6.487.457.656)". Both readings check
# out against the NAV identity, so these tests fix the rule that a token is read
# as written and only the genuinely undecidable shape consults the document.


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        # comma groups thousands, dot marks the decimal
        ("455,032,095,816", 455_032_095_816.0),
        ("13,913.83", 13_913.83),
        # dot groups thousands, comma marks the decimal
        ("6.487.457.656", 6_487_457_656.0),
        ("118.787,06", 118_787.06),
        ("29,98", 29.98),
        # a lone separator with a non-three-digit tail can only be a decimal
        ("78.44", 78.44),
        ("1.2345", 1.2345),
    ],
)
def test_parse_number_reads_either_convention(token: str, expected: float) -> None:
    assert parse_number(token) == pytest.approx(expected)


@pytest.mark.parametrize("token", ["10.029", "1,234"])
def test_ambiguous_number_raises_rather_than_guessing(token: str) -> None:
    """A single separator with exactly three digits behind it is undecidable.

    Guessing costs a silent factor of 1,000, which is precisely the error class
    the six-line NAV identity cannot see, so the parser refuses instead.
    """
    with pytest.raises(ParseError):
        parse_number(token)


@pytest.mark.parametrize(
    ("token", "convention", "expected"),
    [
        ("10.029", "en", 10.029),
        ("10.029", "vi", 10_029.0),
        ("1,234", "en", 1_234.0),
        ("1,234", "vi", 1.234),
    ],
)
def test_convention_resolves_only_the_ambiguous_shape(
    token: str, convention: str, expected: float
) -> None:
    assert parse_number(token, convention) == pytest.approx(expected)


def test_an_unambiguous_token_outranks_the_declared_convention() -> None:
    """The token is evidence; the convention is an inference about the document."""
    assert parse_number("13,913.83", "vi") == pytest.approx(13_913.83)
    assert parse_number("118.787,06", "en") == pytest.approx(118_787.06)


@pytest.mark.parametrize("token", ["1,23,456", "12.34.567,8"])
def test_inconsistent_grouping_is_rejected(token: str) -> None:
    with pytest.raises(ParseError):
        parse_number(token)


def test_detect_convention_reads_self_evident_tokens() -> None:
    assert detect_convention("NAV 103,228,319,952 per unit 16,368.68") == "en"
    assert detect_convention("NAV 103.228.319.952 per unit 16.368,68") == "vi"


def test_detect_convention_abstains_on_a_mixed_document() -> None:
    """Mixed is a real state in this corpus, not a detector failure.

    Returning None makes ambiguous tokens in such a document raise, which is the
    only safe answer when the page itself uses both rules.
    """
    assert detect_convention("NAV 103,228,319,952 change (6.487.457.656)") is None
    assert detect_convention("no numbers here") is None


# --- multi-page column geometry -------------------------------------------

MULTIPAGE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "ssibf_20230605_multipage_columns.pdf"
)


def test_multipage_filing_measures_columns_per_page() -> None:
    """Column geometry belongs to a page, not to a document.

    This filing runs five pages and page 3 carries a second copy of the table
    whose value columns sit ~17pt left of page 0's. Pooling every page's numeric
    words into one pair of anchors put both anchors inside a single column and
    cut each figure in half, filing "455,032,095,816" as 455,032,095 this period
    and 5,816 last period. The six-line identity still closed on the fragments,
    because the discarded remainders happened to balance too, so nothing
    downstream could catch it.
    """
    filing = parse_filing(MULTIPAGE, fund_code="SSIBF")
    assert filing.values["nav_begin"] == pytest.approx(455_032_095_816)
    assert filing.values["nav_end"] == pytest.approx(455_474_226_909)
    assert filing.values["nav_per_unit_begin"] == pytest.approx(13_913.83)
    assert filing.values["nav_per_unit_end"] == pytest.approx(13_943.81)


def test_multipage_filing_keeps_the_prior_column_separate() -> None:
    """The halves used to land in the prior-period slot; check it holds truth."""
    filing = parse_filing(MULTIPAGE, fund_code="SSIBF")
    row = filing.as_row()
    assert row["prior_nav_end"] == pytest.approx(455_032_095_816)
    assert row["prior_nav_begin"] == pytest.approx(453_463_763_649)


def test_multipage_filing_reconciles_on_recovered_values() -> None:
    filing = parse_filing(MULTIPAGE, fund_code="SSIBF")
    identity = (
        filing.values["nav_begin"]
        + filing.values["chg_investment"]
        + filing.net_flow
    )
    assert identity == pytest.approx(filing.values["nav_end"], abs=1.0)
