"""Parse Appendix XXIV of Circular 98/2020/TT-BTC into typed observations.

Appendix XXIV, "Bao cao ve thay doi gia tri tai san rong" / "Report on change of
Net Asset Value", is a fixed Ministry of Finance template filed by every licensed
Vietnamese open-ended fund on each dealing period. The line codes are stable
across managers and across container formats, which is what makes one parser
viable for the whole market.

Three container readers feed one mapping layer:

    parse_text  text layer, trailing-numeric-run heuristic
    parse_pdf   PDF, column assignment from word x-coordinates
    parse_xlsx  XLSX, column assignment from the cell grid

All three produce `cells`, a mapping of line code to (this period, last period),
and hand it to `_filing_from_cells`. Only that function knows FIELD_MAP, so the
mapping rules cannot drift between formats.

Parse on the line code, never on the bilingual label. Label text is reordered by
PDF extraction and is unreliable as an anchor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

__all__ = [
    "FIELD_MAP",
    "SECTION_CODES",
    "PERCENT_FIELDS",
    "FIELD_MAP_ALT",
    "CHANGE_FIELDS",
    "ParseError",
    "Filing",
    "parse_number",
    "parse_text",
    "parse_pdf",
    "parse_xlsx",
    "parse_filing",
    "parse_many",
    "extract_pdf_text",
]


# Line code to field name. Verified against VCBBCF filing of 2022-11-21.
FIELD_MAP: dict[str, str] = {
    "1.1": "nav_begin",
    "1.3": "nav_per_unit_begin",
    "2.1": "nav_end",
    "2.3": "nav_per_unit_end",
    "3.1": "chg_investment",
    "3.2": "chg_flows_net",
    "3.2.1": "subscriptions",
    "3.2.2": "redemptions",
    "3.3": "chg_distribution",
    "4": "chg_nav_per_unit",
    "5.1": "nav_52w_high",
    "5.2": "nav_52w_low",
    "6.1": "foreign_units",
    "6.2": "foreign_value",
    "6.3": "foreign_ownership_pct",
}

# A second Appendix XXIV layout, used by VinaCapital for VLBF and other funds
# filing before roughly mid-2022. It is the same regulation and the same
# identity, but four codes mean different things, which makes it the most
# dangerous document in the corpus: read with FIELD_MAP it yields a filing that
# looks complete and is wrong.
#
#   1.2 / 2.2   NAV per certificate          (standard puts these at 1.3 / 2.3)
#   3.2.1       change from distribution     (standard: gross subscriptions)
#   3.2.2       change from subscription and
#               redemption, NET              (standard: gross redemptions)
#
# So a naive read would file a distribution as a subscription and a net flow as
# a redemption, with the sign of the "redemption" wrong whenever the fund had
# net inflow. This variant does not disclose the gross legs at all; 3.2.2 is
# already netted. Detection is structural, see _select_field_map.
FIELD_MAP_ALT: dict[str, str] = {
    "1.1": "nav_begin",
    "1.2": "nav_per_unit_begin",
    "2.1": "nav_end",
    "2.2": "nav_per_unit_end",
    "3.1": "chg_investment",
    "3.2.1": "chg_distribution",
    "3.2.2": "chg_flows_net",
    "4": "chg_nav_per_unit",
    "6.1": "foreign_units",
    "6.2": "foreign_value",
    "6.3": "foreign_ownership_pct",
}

# Section headers. These never carry values and must never be mapped to fields.
SECTION_CODES: set[str] = {"1", "2", "3", "5", "6", "I", "II"}

# Fields carried in percent units, not as a fraction. The PDF template prints
# "20.54%", which parse_number reads as 20.54. XLSX stores the same cell as the
# fraction 0.2054 under a percent number format, so the XLSX reader scales it to
# match. Both containers must land on the same units.
PERCENT_FIELDS: frozenset[str] = frozenset({"foreign_ownership_pct"})

# The section-3 lines. At least one must be present for a document to be the
# change report rather than a point-in-time NAV snapshot sharing its title.
CHANGE_FIELDS: frozenset[str] = frozenset(
    {
        "chg_investment",
        "chg_flows_net",
        "subscriptions",
        "redemptions",
        "chg_distribution",
    }
)

_CODE_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){0,2}$")
_NUMERIC_TOKEN_RE = re.compile(r"^\(?-?[0-9][0-9,]*(?:\.[0-9]+)?\)?%?$")
_ROW_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+){0,2})\s+(\S.*?)\s*$")

# Header-block lines that open with a bare digit which collides with a real line
# code: "4 Ky bao cao: ..." would otherwise be read as line 4 because it ends in
# a year. Filter them structurally rather than trusting token shape.
_METADATA_LINE_RE = re.compile(
    r"(Kỳ báo cáo|Ngày lập báo cáo|Reporting period|Reporting Date|"
    r"Tên Quỹ|Fund name|Tên Công ty|Fund Management Company|"
    r"Tên Ngân hàng|Supervisory bank|Đơn vị tính|Currency)",
    re.IGNORECASE,
)

_PERIOD_RE = re.compile(
    r"From\s+(\d{1,2}\s+\w{3,}\s+\d{4})\s+to\s+(\d{1,2}\s+\w{3,}\s+\d{4})",
    re.IGNORECASE,
)
_PERIOD_VN_RE = re.compile(
    r"Từ\s+ngày\s+(\d{1,2})\s+tháng\s+(\d{1,2})\s+năm\s+(\d{4})\s+"
    r"đến\s+ngày\s+(\d{1,2})\s+tháng\s+(\d{1,2})\s+năm\s+(\d{4})",
    re.IGNORECASE,
)
# Compressed variant used for VCBF-MGF throughout, and occasionally elsewhere:
# the year is stated once, at the end. "Tu ngay 02 thang 01 den ngay 08 thang 01
# nam 2025" / "From 02 Jan to 08 Jan 2025".
_PERIOD_VN_SHORT_RE = re.compile(
    r"Từ\s+ngày\s+(\d{1,2})\s+tháng\s+(\d{1,2})\s+"
    r"đến\s+ngày\s+(\d{1,2})\s+tháng\s+(\d{1,2})\s+năm\s+(\d{4})",
    re.IGNORECASE,
)
# SSIAM writes the period numerically: "tuan tu 21/11/2023 den 27/11/2023".
# Vietnamese convention is unambiguously day/month/year. Its English half here
# reads "from Nov 21th 2023", which is both month-first and malformed, so the
# Vietnamese line is the only reliable one on these filings.
_PERIOD_VN_SLASH_RE = re.compile(
    r"từ\s*:?\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*"
    r"đến\s*:?\s*(\d{1,2})/(\d{1,2})/(\d{4})",
    re.IGNORECASE,
)
_PERIOD_SHORT_RE = re.compile(
    r"From\s+(\d{1,2}\s+\w{3,})\s+to\s+(\d{1,2}\s+\w{3,}\s+\d{4})",
    re.IGNORECASE,
)
_REPORT_DATE_RE = re.compile(
    r"Reporting\s+Date\s*:?\s*(\d{1,2}\s+\w{3,}\s+\d{4})", re.IGNORECASE
)
_REPORT_DATE_VN_RE = re.compile(
    r"Ngày lập báo cáo\s*:?\s*Ngày\s+(\d{1,2})\s+tháng\s+(\d{1,2})\s+năm\s+(\d{4})",
    re.IGNORECASE,
)
# "VCBF Blue Chip Fund (VCBBCF)" in 2022, "(VCBF-BCF)" after the 2024 rename.
# The leading capital requirement stops accounting parentheses such as
# "(96,819,279)" and mixed-case asides such as "(Vietnam)" from matching.
_FUND_CODE_RE = re.compile(r"\(([A-Z][A-Z0-9]{1,6}(?:-[A-Z0-9]{2,6})?)\)")

_DATE_FORMATS = ("%d %b %Y", "%d %B %Y")

# Cells = line code -> (this period, last period). None means the cell is blank.
Cells = dict[str, "tuple[float | None, float | None]"]


class ParseError(ValueError):
    """The document is not a parseable Appendix XXIV filing."""


def parse_number(tok: str) -> float:
    """Parse one template cell into a float.

    Handles comma thousands separators, accounting parentheses for negatives,
    and a trailing percent sign. Raises ParseError on anything else, including
    the empty string, so a blank cell never silently becomes a number.
    """
    if tok is None:
        raise ParseError("cannot parse number from None")
    s = str(tok).strip().replace(" ", " ").strip()
    if not s:
        raise ParseError("cannot parse number from empty cell")

    if s.endswith("%"):
        s = s[:-1].strip()

    # Accounting parentheses mark a negative. PDF text extraction sometimes
    # splits the pair across tokens, leaving "(2,078,454,593" or
    # "2,078,454,593)". A lone bracket on either side still means the same
    # thing: nothing else in this template puts a bracket against a number.
    negative = s.startswith("(") or s.endswith(")")
    if negative:
        s = s.lstrip("(").rstrip(")").strip()

    s = s.replace(",", "").replace(" ", "")
    if not s:
        raise ParseError("cannot parse number from empty cell")

    try:
        value = float(s)
    except ValueError as exc:
        raise ParseError(f"cannot parse number from {tok!r}") from exc

    return -value if negative else value


def _parse_date(s: str) -> date:
    s = " ".join(str(s).split())
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ParseError(f"cannot parse date from {s!r}")


def _normalise_code(raw) -> str | None:
    """Render a code cell as a string code.

    XLSX stores some codes as numbers: `1` as int, `6.1` as float. Everything
    with two dots, such as `3.2.1`, can only ever be a string.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return str(raw)
    if isinstance(raw, float):
        if raw != raw:  # NaN
            return None
        text = f"{raw:.10f}".rstrip("0").rstrip(".")
        return text or None
    text = str(raw).strip()
    return text or None


@dataclass
class Filing:
    """One fund, one dealing period, as disclosed."""

    fund_code: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    report_date: date | None = None
    values: dict[str, float] = field(default_factory=dict)
    prior_values: dict[str, float] = field(default_factory=dict)
    source: str | None = None
    # Set when the bilingual header's two halves disagree on the period window.
    # Carried into the panel rather than resolved silently.
    date_conflict: str | None = None
    # Which code mapping was applied: "standard" or "alt". Carried so a reader
    # can tell whether a row's gross legs are absent because the fund had no
    # flow or because its template never disclosed them.
    template_variant: str = "standard"

    @property
    def net_flow(self) -> float:
        """Net subscription flow in VND.

        Prefers the disclosed net line 3.2; falls back to the sum of the gross
        legs 3.2.1 and 3.2.2 when 3.2 is blank.
        """
        if "chg_flows_net" in self.values:
            return self.values["chg_flows_net"]
        subs = self.values.get("subscriptions")
        reds = self.values.get("redemptions")
        if subs is None and reds is None:
            raise ParseError("filing discloses neither net nor gross flows")
        return (subs or 0.0) + (reds or 0.0)

    def _units(self, nav_key: str, per_unit_key: str) -> float | None:
        nav = self.values.get(nav_key)
        per_unit = self.values.get(per_unit_key)
        if nav is None or per_unit in (None, 0.0):
            return None
        return nav / per_unit

    @property
    def units_begin(self) -> float | None:
        return self._units("nav_begin", "nav_per_unit_begin")

    @property
    def units_end(self) -> float | None:
        return self._units("nav_end", "nav_per_unit_end")

    @property
    def gross_return(self) -> float | None:
        """Period return on NAV per certificate. Not distribution adjusted."""
        begin = self.values.get("nav_per_unit_begin")
        end = self.values.get("nav_per_unit_end")
        if begin in (None, 0.0) or end is None:
            return None
        return end / begin - 1.0

    @property
    def period_days(self) -> int | None:
        if self.period_start is None or self.period_end is None:
            return None
        return (self.period_end - self.period_start).days

    @property
    def foreign_share_of_nav(self) -> float | None:
        """Foreign value over closing NAV, in percent.

        A derived cross-check on line 6.3 rather than a substitute for it. The
        printed ratio does not always equal this quotient, so both are carried.
        """
        value = self.values.get("foreign_value")
        nav_end = self.values.get("nav_end")
        if value is None or nav_end in (None, 0.0):
            return None
        return value / nav_end * 100.0

    def as_row(self) -> dict:
        """Flatten to one panel row. Prior-period columns get a `prior_` prefix."""
        row: dict = {
            "fund_code": self.fund_code,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "report_date": self.report_date,
            "period_days": self.period_days,
            "source": self.source,
            "date_conflict": self.date_conflict,
            "template_variant": self.template_variant,
        }
        for name in FIELD_MAP.values():
            row[name] = self.values.get(name)
        for name in FIELD_MAP.values():
            row[f"prior_{name}"] = self.prior_values.get(name)
        try:
            row["net_flow"] = self.net_flow
        except ParseError:
            row["net_flow"] = None
        row["units_begin"] = self.units_begin
        row["units_end"] = self.units_end
        row["gross_return"] = self.gross_return
        row["foreign_share_of_nav"] = self.foreign_share_of_nav
        return row


# --------------------------------------------------------------------------
# the single mapping layer
# --------------------------------------------------------------------------


def _select_field_map(cells: Cells) -> tuple[dict[str, str], str]:
    """Choose the code mapping by the shape of the document.

    The two layouts are told apart by where NAV per certificate sits. The
    standard template always carries 1.3 and leaves 1.2 blank, because 1.2 is
    the per-lot line that applies only to ETFs. The alternate template has no
    1.3 at all and puts the per-certificate value in 1.2. Testing for a valued
    1.2 with no 1.3 therefore separates them without relying on labels.
    """
    has_13 = "1.3" in cells and cells["1.3"][0] is not None
    has_12 = "1.2" in cells and cells["1.2"][0] is not None
    if has_12 and not has_13:
        return FIELD_MAP_ALT, "alt"
    return FIELD_MAP, "standard"


def _filing_from_cells(
    cells: Cells,
    *,
    fund_code: str | None,
    period_start: date | None,
    period_end: date | None,
    report_date: date | None,
    source: str | None,
    date_conflict: str | None = None,
) -> Filing:
    """Apply FIELD_MAP to raw cells. The only place mapping rules live."""
    values: dict[str, float] = {}
    prior_values: dict[str, float] = {}
    field_map, variant = _select_field_map(cells)

    for code, (this_period, last_period) in cells.items():
        if code in SECTION_CODES:
            continue
        name = field_map.get(code)
        if name is None:
            continue
        if this_period is not None:
            values[name] = this_period
        if last_period is not None:
            prior_values[name] = last_period

    if "nav_end" not in values:
        raise ParseError(
            "line 2.1 (nav_end) absent; layout is not Appendix XXIV"
            + (f" [{source}]" if source else "")
        )

    # Reject the point-in-time variant. VinaCapital publishes a daily file that
    # is also headed "Phu luc XXIV" and also carries a line 2.1, but it reports
    # NAV as at a date rather than its change over a period, and its codes mean
    # different things: 2.1 there is the foreign certificate count, not closing
    # NAV. Read as a change report it yields a plausible-looking, wrong nav_end.
    # The change section is what makes a filing a change report, so require it.
    if not (CHANGE_FIELDS & set(values)):
        raise ParseError(
            "no section-3 (change in NAV) line present; this is a point-in-time "
            "NAV report, not the Appendix XXIV change report"
            + (f" [{source}]" if source else "")
        )

    return Filing(
        fund_code=fund_code,
        period_start=period_start,
        period_end=period_end,
        report_date=report_date,
        values=values,
        prior_values=prior_values,
        source=source,
        date_conflict=date_conflict,
        template_variant=variant,
    )


def _start_year(month_start: int, end: date) -> int:
    """Infer the omitted start year in a compressed header.

    The year is stated once, on the closing date. A period whose start month is
    later than its end month straddles New Year, so the start belongs to the
    preceding year: "Tu ngay 29 thang 12 den ngay 04 thang 01 nam 2025".
    """
    return end.year - 1 if month_start > end.month else end.year


def _period_from_vietnamese(text: str) -> tuple[date | None, date | None]:
    match = _PERIOD_VN_RE.search(text)
    if match:
        d1, m1, y1, d2, m2, y2 = (int(g) for g in match.groups())
        try:
            return date(y1, m1, d1), date(y2, m2, d2)
        except ValueError:
            return None, None

    match = _PERIOD_VN_SHORT_RE.search(text)
    if match:
        d1, m1, d2, m2, y2 = (int(g) for g in match.groups())
        try:
            end = date(y2, m2, d2)
            return date(_start_year(m1, end), m1, d1), end
        except ValueError:
            return None, None

    match = _PERIOD_VN_SLASH_RE.search(text)
    if match:
        d1, m1, y1, d2, m2, y2 = (int(g) for g in match.groups())
        try:
            return date(y1, m1, d1), date(y2, m2, d2)
        except ValueError:
            return None, None

    return None, None


def _period_from_english(text: str) -> tuple[date | None, date | None]:
    match = _PERIOD_RE.search(text)
    if match:
        try:
            return _parse_date(match.group(1)), _parse_date(match.group(2))
        except ParseError:
            return None, None

    match = _PERIOD_SHORT_RE.search(text)
    if match:
        try:
            end = _parse_date(match.group(2))
            start_partial = _parse_date(f"{match.group(1)} {end.year}")
            return (
                start_partial.replace(year=_start_year(start_partial.month, end)),
                end,
            )
        except (ParseError, ValueError):
            return None, None

    return None, None


def _dates_from_text(
    text: str,
) -> tuple[date | None, date | None, date | None, str | None]:
    """Resolve the period window and report date, preferring the Vietnamese half.

    The spec directs parsing from the English half of the bilingual header, but
    the English half is hand-translated and demonstrably wrong in real filings:
    vcbbcf_bc_tuan_20240103_1.xlsx reads "From 28 Dec 2023 to 03 Dec 2024" where
    the Vietnamese reads "den ngay 03 thang 01 nam 2024". Trusting English there
    dates the filing eleven months late, which silently misaligns the return
    window and violates rule 4.4. The Vietnamese half is generated from the
    numeric fields and is internally consistent, so it wins; English is kept as
    a fallback and as a cross-check whose disagreement is reported, not hidden.
    """
    vn_start, vn_end = _period_from_vietnamese(text)
    en_start, en_end = _period_from_english(text)

    period_start = vn_start or en_start
    period_end = vn_end or en_end

    conflicts: list[str] = []
    if vn_start and en_start and vn_start != en_start:
        conflicts.append(f"period_start VN {vn_start} vs EN {en_start}")
    if vn_end and en_end and vn_end != en_end:
        conflicts.append(f"period_end VN {vn_end} vs EN {en_end}")

    report_date = None
    match = _REPORT_DATE_VN_RE.search(text)
    if match:
        day, month, year = (int(g) for g in match.groups())
        try:
            report_date = date(year, month, day)
        except ValueError:
            report_date = None
    if report_date is None:
        match = _REPORT_DATE_RE.search(text)
        if match:
            try:
                report_date = _parse_date(match.group(1))
            except ParseError:
                report_date = None

    return (
        period_start,
        period_end,
        report_date,
        "; ".join(conflicts) if conflicts else None,
    )


def _fund_code_from_text(text: str) -> str | None:
    match = _FUND_CODE_RE.search(text)
    return match.group(1) if match else None


# --------------------------------------------------------------------------
# container reader: text layer
# --------------------------------------------------------------------------


def parse_text(
    text: str, fund_code: str | None = None, source: str | None = None
) -> Filing:
    """Parse an extracted text layer into a Filing.

    Kept free of PDF handling so the mapping layer is testable against text
    fixtures without pdfplumber or a network.

    Values are taken as the run of numeric tokens at the end of the line, so
    inline bilingual labels are tolerated. When the real filing puts a label
    between the code and its numbers, the trailing run still lands correctly.
    A single trailing number is read as this-period, which is the one case this
    reader cannot distinguish from a blank this-period cell; `parse_pdf` uses
    word coordinates instead and does not share that limitation.
    """
    if not text or not text.strip():
        raise ParseError("empty text")

    cells: Cells = {}
    for raw_line in text.splitlines():
        line = raw_line.replace(" ", " ").rstrip()
        match = _ROW_RE.match(line)
        if not match:
            continue
        code, remainder = match.group(1), match.group(2)
        if code in SECTION_CODES or code in cells:
            continue
        if _METADATA_LINE_RE.search(remainder):
            continue

        tokens = remainder.split()
        trailing: list[str] = []
        for token in reversed(tokens):
            if len(trailing) == 2 or not _NUMERIC_TOKEN_RE.match(token):
                break
            trailing.append(token)
        if not trailing:
            continue
        trailing.reverse()

        this_period = parse_number(trailing[0])
        last_period = parse_number(trailing[1]) if len(trailing) > 1 else None
        cells[code] = (this_period, last_period)

    period_start, period_end, report_date, date_conflict = _dates_from_text(text)
    return _filing_from_cells(
        cells,
        fund_code=fund_code or _fund_code_from_text(text),
        period_start=period_start,
        period_end=period_end,
        report_date=report_date,
        source=source,
        date_conflict=date_conflict,
    )


# --------------------------------------------------------------------------
# container reader: PDF
# --------------------------------------------------------------------------


def extract_pdf_text(path: str | Path) -> str:
    """Extract the text layer of a filing PDF."""
    import pdfplumber  # imported lazily so text parsing needs no PDF stack

    parts: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    return "\n".join(parts)


# A value must right-align within this many points of a column edge. The two
# columns sit ~90pt apart, so this comfortably separates them while rejecting
# numbers embedded in label text.
_COLUMN_TOLERANCE_PT = 20.0

# How far below its code line a detached value line may sit. Template rows are
# about 18pt apart and the observed offset is about 5pt, so 12 separates a row's
# own values from the next row's code.
_DETACHED_VALUE_MAX_PT = 12.0


def _column_right_edges(numeric_words: list[dict]) -> list[float]:
    """Infer the right edges of the two value columns.

    The template right-aligns both numeric columns, so their right edges are
    near-constant down the page. Cluster the observed edges and keep the two
    strongest, which become the anchors every value is assigned against.
    """
    edges = sorted(w["x1"] for w in numeric_words)
    if not edges:
        return []

    clusters: list[list[float]] = [[edges[0]]]
    for edge in edges[1:]:
        if edge - clusters[-1][-1] <= 6.0:
            clusters[-1].append(edge)
        else:
            clusters.append([edge])

    ranked = sorted(clusters, key=len, reverse=True)[:2]
    return sorted(sum(c) / len(c) for c in ranked)


_NUMERIC_GLYPHS = set("0123456789,.()%-")


def _recover_from_chars(
    chars: list[dict], window: tuple[float, float]
) -> float | None:
    """Rebuild a value from raw glyphs when word grouping mangled it.

    Some filings wrap a long label onto the line holding its value, so the label
    and the number occupy overlapping x-positions and pdfplumber interleaves
    them: SSIAM's line 3.1 extracts as "5ro1n,8g1 8k,y490" where the value is
    51,818,490 with the letters of "rong ky" woven through it.

    Interleaving preserves x-order, so keeping only numeric glyphs inside the
    column's own x-window and reading left to right reconstructs the figure
    exactly. This runs only when the word-based path found nothing for that
    slot, and whatever it returns still has to satisfy the reconciliation
    identity, so a bad recovery is caught rather than absorbed.
    """
    low, high = window
    glyphs = [
        c
        for c in chars
        if low <= c["x0"] < high and c["text"] in _NUMERIC_GLYPHS
    ]
    if not glyphs:
        return None
    text = "".join(c["text"] for c in sorted(glyphs, key=lambda c: c["x0"]))
    if not any(ch.isdigit() for ch in text):
        return None
    try:
        return parse_number(text)
    except ParseError:
        return None


def _cells_from_pdf(path: str | Path) -> tuple[Cells, str]:
    """Read code rows out of a PDF using word geometry.

    Assigning each numeric word to the nearest column right edge is what makes
    blank cells genuinely blank: a lone number sitting under "Last period" is
    recorded as a prior value with no current value, rather than being promoted
    into the current column.
    """
    import pdfplumber

    rows: dict[tuple[int, int], list[dict]] = {}
    page_chars: dict[int, list[dict]] = {}
    text_parts: list[str] = []

    with pdfplumber.open(str(path)) as pdf:
        for page_no, page in enumerate(pdf.pages):
            text_parts.append(page.extract_text() or "")
            page_chars[page_no] = list(page.chars)
            for word in page.extract_words():
                # Bucket by vertical position; template rows are ~17pt apart.
                key = (page_no, int(round(word["top"] / 3.0)))
                word["_page"] = page_no
                rows.setdefault(key, []).append(word)

    # Merge buckets that belong to the same visual row.
    merged: dict[tuple[int, int], list[dict]] = {}
    for (page_no, band), words in sorted(rows.items()):
        target = None
        for offset in (-1, 0, 1):
            if (page_no, band + offset) in merged:
                target = (page_no, band + offset)
                break
        merged.setdefault(target or (page_no, band), []).extend(words)

    # Drop the bilingual header block before anything measures or reads it.
    # "4 Ky bao cao: Tu ngay 17 thang 11 nam 2022" opens with a real line code
    # and ends in digits, so only its label text distinguishes it from line 4.
    body: list[list[dict]] = []
    for words in merged.values():
        ordered = sorted(words, key=lambda w: w["x0"])
        if not ordered:
            continue
        if _METADATA_LINE_RE.search(" ".join(w["text"] for w in ordered)):
            continue
        body.append(ordered)

    numeric_words = [
        w for ordered in body for w in ordered[1:] if _NUMERIC_TOKEN_RE.match(w["text"])
    ]
    edges = _column_right_edges(numeric_words)

    # Per-column x-window, anchored on the column's right edge and made as wide
    # as the widest figure that column actually holds. Anchoring on the right is
    # what makes this safe: the template right-aligns both value columns, so a
    # window sized to the widest observed number cannot reach into the column
    # to its left, while still being wide enough to catch a leading digit that
    # word grouping split off.
    widths: dict[int, float] = {}
    for word in numeric_words:
        if len(edges) < 2:
            break
        index = min(range(len(edges)), key=lambda i: abs(word["x1"] - edges[i]))
        if abs(word["x1"] - edges[index]) > _COLUMN_TOLERANCE_PT:
            continue
        widths[index] = max(widths.get(index, 0.0), word["x1"] - word["x0"])

    windows: dict[int, tuple[float, float]] = {}
    for index, edge in enumerate(edges):
        width = widths.get(index)
        if width is None:
            continue
        left = edge - width - _COLUMN_TOLERANCE_PT
        if index > 0:
            # Never reach past the previous column's right edge.
            left = max(left, edges[index - 1] + 2.0)
        windows[index] = (left, edge + 3.0)

    cells: Cells = {}
    for ordered in body:
        code = ordered[0]["text"].strip()
        if not _CODE_RE.match(code) or code in SECTION_CODES or code in cells:
            continue

        numbers = [w for w in ordered[1:] if _NUMERIC_TOKEN_RE.match(w["text"])]
        if not numbers:
            continue

        if len(edges) < 2:
            # Single-column layout: everything is this-period, in reading order.
            this_period = parse_number(numbers[0]["text"])
            last_period = parse_number(numbers[1]["text"]) if len(numbers) > 1 else None
        else:
            # Read the glyphs, not pdfplumber's grouping of them. Word grouping
            # is a heuristic over these same characters and it fails two ways in
            # this corpus: a wrapped label woven through the digits, and a
            # leading digit split off as its own token, which silently truncated
            # 828,087,665 to 28,087,665. Reading every numeric glyph inside the
            # column window in x-order is immune to both, and the reconciliation
            # identity independently checks the result on every row.
            page_no = ordered[0].get("_page", 0)
            tops = [w["top"] for w in ordered]
            band = [
                c
                for c in page_chars.get(page_no, [])
                if min(tops) - 1.5 <= c["top"] <= max(tops) + 1.5
            ]
            this_period = (
                _recover_from_chars(band, windows[0]) if 0 in windows else None
            )
            last_period = (
                _recover_from_chars(band, windows[1]) if 1 in windows else None
            )

            # Word grouping remains the fallback for anything the glyph pass
            # could not resolve.
            if this_period is None or last_period is None:
                slot: dict[int, float] = {}
                for word in numbers:
                    index = min(
                        range(len(edges)), key=lambda i: abs(word["x1"] - edges[i])
                    )
                    if abs(word["x1"] - edges[index]) > _COLUMN_TOLERANCE_PT:
                        continue  # a number inside label text, not a value cell
                    slot.setdefault(index, parse_number(word["text"]))
                if this_period is None:
                    this_period = slot.get(0)
                if last_period is None:
                    last_period = slot.get(1)

        if this_period is None and last_period is None:
            continue
        cells[code] = (this_period, last_period)

    # Second pass: some filings render a row as a four-line block, with the code
    # on one line, the Vietnamese label on the next, the values about 5pt below
    # the code, and the English label under those. The value line carries no
    # code of its own, so the banding above never reaches it and the line is
    # simply lost, which is what left SSIAM's 3.1 and 3.2 blank on 27 filings.
    #
    # A detached, purely numeric line belongs to the nearest code line above it.
    # That is the document's own visual grammar rather than a guess, and it only
    # ever fills a slot the first pass left empty, so it cannot overwrite a value
    # that was read correctly.
    if len(edges) >= 2:
        code_lines: list[tuple[int, float, str]] = []
        numeric_lines: list[tuple[int, float, list[dict]]] = []
        for ordered in body:
            page_no = ordered[0].get("_page", 0)
            top = min(w["top"] for w in ordered)
            head = ordered[0]["text"].strip()
            if _CODE_RE.match(head) and head not in SECTION_CODES:
                code_lines.append((page_no, top, head))
                continue
            # A detached value line often gets banded together with the English
            # half of the label, so it is not purely numeric. What identifies it
            # is that it opens with no line code yet carries figures sitting in
            # the value columns.
            values = [
                w
                for w in ordered
                if _NUMERIC_TOKEN_RE.match(w["text"])
                and min(abs(w["x1"] - e) for e in edges) <= _COLUMN_TOLERANCE_PT
            ]
            if values:
                numeric_lines.append((page_no, top, values))

        for page_no, top, words in numeric_lines:
            above = [
                (t, c) for (pg, t, c) in code_lines
                if pg == page_no and 0 < top - t <= _DETACHED_VALUE_MAX_PT
            ]
            if not above:
                continue
            _, code = max(above)
            name = FIELD_MAP.get(code)
            if name is None:
                continue
            existing = cells.get(code, (None, None))
            if existing[0] is not None and existing[1] is not None:
                continue
            slot: dict[int, float] = {}
            for word in words:
                index = min(range(len(edges)), key=lambda i: abs(word["x1"] - edges[i]))
                if abs(word["x1"] - edges[index]) > _COLUMN_TOLERANCE_PT:
                    continue
                slot.setdefault(index, parse_number(word["text"]))
            merged = (
                existing[0] if existing[0] is not None else slot.get(0),
                existing[1] if existing[1] is not None else slot.get(1),
            )
            if merged != (None, None):
                cells[code] = merged

    return cells, "\n".join(text_parts)


def parse_pdf(path: str | Path, fund_code: str | None = None) -> Filing:
    """Parse a filing PDF. Raises ParseError when there is no usable text layer."""
    cells, text = _cells_from_pdf(path)
    if not text.strip():
        raise ParseError(f"no text layer in {path}; needs OCR")

    period_start, period_end, report_date, date_conflict = _dates_from_text(text)
    return _filing_from_cells(
        cells,
        fund_code=fund_code or _fund_code_from_text(text),
        period_start=period_start,
        period_end=period_end,
        report_date=report_date,
        source=str(path),
        date_conflict=date_conflict,
    )


# --------------------------------------------------------------------------
# container reader: XLSX
# --------------------------------------------------------------------------


def _cells_from_xlsx(path: str | Path) -> tuple[Cells, str, date | None]:
    """Read code rows out of an XLSX filing.

    VCBF moved from PDF to XLSX in December 2022. The template, its line codes
    and the identity are unchanged; only the container differs. Value columns
    are located from the row carrying code 2.1 rather than hardcoded, because
    the sheet name and header row both move between vintages.
    """
    import io

    import openpyxl

    # Hand openpyxl the bytes, not the path: it validates the container by file
    # extension, and cached filings are named by content hash.
    with open(path, "rb") as handle:
        workbook = openpyxl.load_workbook(io.BytesIO(handle.read()), data_only=True)

    for sheet in workbook.worksheets:
        grid = [list(row) for row in sheet.iter_rows()]

        # The code column is not always column A. VCBF puts codes in A, but
        # VinaCapital's earlier layout indents the whole table and puts them in
        # C. Pick the column that yields the most recognisable line codes and
        # actually contains 2.1, rather than assuming a position.
        best: tuple[int, dict[str, list]] | None = None
        for column in range(min(6, max((len(r) for r in grid), default=0))):
            candidate: dict[str, list] = {}
            for row in grid:
                if column >= len(row):
                    continue
                code = _normalise_code(row[column].value)
                if code and _CODE_RE.match(code) and code not in candidate:
                    candidate[code] = row
            if "2.1" in candidate and (best is None or len(candidate) > len(best[1])):
                best = (column, candidate)
        if best is None:
            continue
        code_rows = best[1]

        # Calibrate the value columns off line 2.1, skipping the code column.
        anchor = [
            cell
            for cell in code_rows["2.1"][1:]
            if isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool)
        ]
        if not anchor:
            continue
        value_columns = [cell.column for cell in anchor[:2]]

        cells: Cells = {}
        for code, row in code_rows.items():
            if code in SECTION_CODES:
                continue
            slots: list[float | None] = []
            for column in value_columns:
                cell = next((c for c in row if c.column == column), None)
                raw = cell.value if cell is not None else None
                if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    slots.append(None)
                    continue
                value = float(raw)
                name = FIELD_MAP.get(code)
                # XLSX stores 6.3 as a fraction under a percent format; the PDF
                # prints it as "20.54%". Scale so both containers agree.
                if (
                    name in PERCENT_FIELDS
                    and cell is not None
                    and "%" in (cell.number_format or "")
                ):
                    value *= 100.0
                slots.append(value)
            while len(slots) < 2:
                slots.append(None)
            if slots[0] is None and slots[1] is None:
                continue
            cells[code] = (slots[0], slots[1])

        # Text blob for the bilingual header, plus any real date cell.
        text_parts: list[str] = []
        report_date: date | None = None
        for row in grid:
            for index, cell in enumerate(row):
                if isinstance(cell.value, str):
                    text_parts.append(cell.value)
                    if "reporting date" in cell.value.lower():
                        for following in row[index + 1 :]:
                            if isinstance(following.value, datetime):
                                report_date = following.value.date()
                                break
                            if isinstance(following.value, date):
                                report_date = following.value
                                break
        return cells, "\n".join(text_parts), report_date

    raise ParseError(f"no sheet in {path} carries line 2.1; not Appendix XXIV")


def parse_xlsx(path: str | Path, fund_code: str | None = None) -> Filing:
    """Parse an XLSX filing."""
    cells, text, report_date_cell = _cells_from_xlsx(path)
    period_start, period_end, report_date, date_conflict = _dates_from_text(text)
    return _filing_from_cells(
        cells,
        fund_code=fund_code or _fund_code_from_text(text),
        period_start=period_start,
        period_end=period_end,
        report_date=report_date or report_date_cell,
        source=str(path),
        date_conflict=date_conflict,
    )


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------


def _sniff(path: Path) -> str:
    """Identify the container by magic bytes, not by file name.

    The cache names files by content hash, and mirrors serve XLSX under a .pdf
    path often enough that the extension cannot be trusted.
    """
    with open(path, "rb") as handle:
        head = handle.read(8)
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "xlsx"
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix in (".xlsx", ".xlsm"):
        return "xlsx"
    raise ParseError(f"unrecognised container for {path}")


def parse_filing(path: str | Path, fund_code: str | None = None) -> Filing:
    """Parse a filing, dispatching on the container's actual bytes."""
    path = Path(path)
    kind = _sniff(path)
    if kind == "pdf":
        return parse_pdf(path, fund_code=fund_code)
    return parse_xlsx(path, fund_code=fund_code)


def parse_many(
    paths: Iterable[tuple[str, str]],
) -> tuple[list[Filing], list[tuple[str, str]]]:
    """Parse many (path, fund_code) pairs.

    Collects failures as (path, reason) instead of raising. A multi-year,
    multi-manager run will always hit malformed or scanned filings, and the run
    should surface them for review rather than abort.
    """
    filings: list[Filing] = []
    failures: list[tuple[str, str]] = []
    for path, fund_code in paths:
        try:
            filings.append(parse_filing(path, fund_code=fund_code))
        except Exception as exc:  # noqa: BLE001 - a bad filing must not abort a run
            failures.append((str(path), f"{type(exc).__name__}: {exc}"))
    return filings, failures
