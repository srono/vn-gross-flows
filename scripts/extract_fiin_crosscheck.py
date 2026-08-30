"""Cross-check panel NAV against FiinGroup's independently published figures.

The Fmarket check already validates NAV *per certificate*, which catches a
misparsed price. It cannot catch a misparsed unit count, because price times
wrong units is a wrong total that no per-unit comparison sees. FiinGroup
publishes total NAV per fund from its own pipeline, so comparing it closes that
gap and is the first independent check on the level of the panel rather than
its unit price.

Their tables print one line per fund: a name, then NAV in billion VND, then a
run of percentages and flow figures whose meaning changes between the monthly
and year-end layouts. Only the first numeric token is reliably NAV across every
layout, so only that is read. The flow columns are deliberately left alone: in
the June 2026 report they mean month, quarter and half-year, in the December
one they mean two calendar years, and a parser that guessed would produce a
comparison that looked fine and meant nothing.

Fund names are not stable across reports. The same fund appears as
"VinaCapital Enhanced Fixed Income Fund", "VinaCapital Enhanced Fund" and
"VINACAPITAL-VFF" in different months, so matching is by explicit alias and
anything unmatched is reported rather than dropped silently.
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import pdfplumber

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "data" / "interim" / "fiin_reports"
OUT = ROOT / "data" / "output" / "growth_research"

# Alias patterns per panel fund, case-insensitive, matched against the whole
# name. Built from the variants actually observed across the fourteen reports.
ALIASES = {
    "VINACAPITAL-VEOF": r"vinacapital[- ]veof|vinacapital leading enterprise",
    "VINACAPITAL-VESAF": r"vinacapital[- ]vesaf|vietnam equity special access|vinacapital strategic growth",
    "VINACAPITAL-VFF": r"vinacapital enhanced (fixed income )?fund|vinacapital[- ]vff",
    "VINACAPITAL-VIBF": r"vinacapital (insights?|integrated) balanced|vinacapital[- ]vibf",
    "VINACAPITAL-VLBF": r"vinacapital liquidity bond|vinacapital[- ]vlbf",
    "DCDS": r"^dc dynamic securities",
    "DCBF": r"^dc bond fund",
    "DCIP": r"^dc income plus",
    "DCDE": r"^dc dividend focus(ed)? equity",
    "SSIBF": r"^ssi bond fund",
    "SSI-SCA": r"^ssi-sca|ssi sustainable competitive",
    "SSI-EF": r"^ssi enhanced fund",
    "VLGF": r"vietnam long[- ]?term growth",
    "VCBF-BCF": r"^vcbf blue chip",
    "VCBF-MGF": r"^vcbf midcap growth",
    "VCBF-TBF": r"^vcbf tactical balanced",
    "VCBF-FIF": r"^vcbf fixed income",
    "VCBF-AIF": r"^vcbf active income",
}
COMPILED = {code: re.compile(p, re.I) for code, p in ALIASES.items()}

# A name, then a numeric tail. The tail must start with a digit or an opening
# bracket so a sentence ending in a year is not mistaken for a table row.
ROW = re.compile(r"^([A-Za-z][A-Za-z0-9 .,&'/\-]{3,48}?)\s+([\d(][\d(),.%\- ]{6,})$")
NUMBER = re.compile(r"^\(?-?[\d,]+(?:\.\d+)?\)?$")


def _to_float(token: str) -> float | None:
    """Accounting negatives are bracketed; thousands are comma-separated."""
    negative = token.startswith("(") and token.endswith(")")
    cleaned = token.strip("()").replace(",", "")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return -value if negative else value


def _match(name: str) -> str | None:
    for code, pattern in COMPILED.items():
        if pattern.search(name.strip()):
            return code
    return None


def extract(path: Path) -> tuple[list[dict], set[str]]:
    period = path.stem
    rows, unmatched = [], set()
    with pdfplumber.open(path) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            for line in (page.extract_text() or "").split("\n"):
                match = ROW.match(line.strip())
                if not match:
                    continue
                name, tail = match.group(1).strip(), match.group(2)
                tokens = tail.split()
                # NAV is the leading token and carries no percent sign.
                if not tokens or "%" in tokens[0] or not NUMBER.match(tokens[0]):
                    continue
                code = _match(name)
                if code is None:
                    unmatched.add(name)
                    continue
                nav = _to_float(tokens[0])
                if nav is None or nav <= 0:
                    continue
                rows.append(
                    {
                        "report_period": period,
                        "fund_code": code,
                        "reported_name": name,
                        "fiin_nav_bn_vnd": nav,
                        "source_page": page_number,
                    }
                )
    return rows, unmatched


def main() -> None:
    rows, unmatched = [], set()
    for path in sorted(REPORTS.glob("*.pdf")):
        page_rows, page_unmatched = extract(path)
        rows.extend(page_rows)
        unmatched |= page_unmatched

    frame = pd.DataFrame(rows)
    if frame.empty:
        print("no rows extracted")
        return

    # A fund can appear in several tables of one report; keep the largest, which
    # is the full-NAV table rather than an equity-sleeve subtotal.
    frame = (
        frame.sort_values("fiin_nav_bn_vnd", ascending=False)
        .drop_duplicates(subset=["report_period", "fund_code"])
        .sort_values(["report_period", "fund_code"])
    )

    panel = pd.read_csv(ROOT / "data" / "output" / "vngross_fund_month.csv")
    months = pd.PeriodIndex(pd.to_datetime(panel["month"]), freq="M")
    panel["report_period"] = months.strftime("%Y.%m")
    panel["panel_nav_bn_vnd"] = panel["nav_end"] / 1e9
    # These funds deal weekly, so the last dealing date of a month is usually
    # not the last calendar day of it. FiinGroup reports the calendar month end.
    # The two sources are therefore measuring different dates, and the gap is
    # the first thing to rule out before calling a difference an error.
    panel["gap_days"] = (
        months.to_timestamp("M") - pd.to_datetime(panel["period_end"])
    ).dt.days

    merged = frame.merge(
        panel[["report_period", "fund_code", "panel_nav_bn_vnd", "gap_days"]],
        on=["report_period", "fund_code"],
        how="left",
    )
    matched = merged.dropna(subset=["panel_nav_bn_vnd"]).copy()
    matched["difference_pct"] = (
        (matched["fiin_nav_bn_vnd"] - matched["panel_nav_bn_vnd"])
        / matched["panel_nav_bn_vnd"]
        * 100
    )
    merged.to_csv(OUT / "fiin_nav_crosscheck.csv", index=False)

    print(f"extracted {len(frame)} fund-report NAV observations")
    print(f"overlapping the panel: {len(matched)}")
    print()
    if len(matched):
        absolute = matched["difference_pct"].abs()
        for threshold in (0.5, 1.0, 5.0):
            share = (absolute <= threshold).mean() * 100
            print(f"  within {threshold:>4}% : {(absolute <= threshold).sum():>3} "
                  f"({share:.0f}%)")
        print(f"  median absolute difference: {absolute.median():.2f}%")
        print()
        matched["gap_bucket"] = pd.cut(
            matched["gap_days"], [-1, 0, 2, 4, 100], labels=["0", "1-2", "3-4", "5+"]
        )
        print("median absolute difference by dealing-date gap (days):")
        print(
            matched.groupby("gap_bucket", observed=True)
            .agg(n=("difference_pct", "size"),
                 median_abs_pct=("difference_pct", lambda s: s.abs().median()))
            .round(3)
            .to_string()
        )
        print()

        worst = matched.reindex(absolute.sort_values(ascending=False).index).head(8)
        print("largest disagreements:")
        print(
            worst[
                [
                    "report_period",
                    "fund_code",
                    "fiin_nav_bn_vnd",
                    "panel_nav_bn_vnd",
                    "difference_pct",
                ]
            ]
            .round(2)
            .to_string(index=False)
        )
    print(f"\nunmatched names seen: {len(unmatched)} (other managers' funds, ETFs)")


if __name__ == "__main__":
    main()
