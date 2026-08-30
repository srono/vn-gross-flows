"""Cache FiinGroup's monthly fund reports as an external cross-check.

These are an independent read on the same market from a commercial vendor, and
they are useful precisely because they are not built from the filings this
project parses. Where they agree, a result has survived a different data
pipeline and a different universe. Where they disagree, one of us is wrong and
it is worth knowing which.

The universes are not the same and headline numbers are not comparable. FiinPro
covers 76 equity and 25 bond funds worth around VND275tn including foreign
ETFs, offshore closed-end funds and UCITS wrappers; this panel covers domestic
open-ended funds filing Appendix XXIV. Cite them for direction, never levels.

Treated the same way as the source filings under LICENSE-DATA: cached locally,
excluded from version control, never redistributed. Only derived comparisons
and citations belong in the repository. Fetched at one request per second with
an identifying user agent.
"""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "interim" / "fiin_reports"
MANIFEST = ROOT / "data" / "fiin_report_index.csv"

# The file is served from two hosts with different path casing. Try both, but
# note that the lowercase host answers 200 with a 3KB HTML page for months it
# does not have, so a status code proves nothing here and the response has to
# be checked for the PDF magic bytes. A HEAD sweep against that host reports
# every period as available and every one of them is a lie.
HOSTS = (
    "https://web.fiintrade.vn/Upload/FUND",
    "https://fiintrade.vn/upload/FUND",
)
FILENAME = "FiinGroup_Fund_Monthly_Report_{period}_EN.pdf"
HEADERS = {"User-Agent": "vngross/0.1 (academic research; mai@10thirtylabs.com)"}
REQUEST_INTERVAL_SECONDS = 1.0


def periods(start: tuple[int, int], end: tuple[int, int]) -> list[str]:
    stamps = pd.period_range(
        pd.Period(f"{start[0]}-{start[1]:02d}", freq="M"),
        pd.Period(f"{end[0]}-{end[1]:02d}", freq="M"),
    )
    return [f"{p.year}.{p.month:02d}" for p in stamps]


def fetch(period: str) -> dict | None:
    for host in HOSTS:
        url = f"{host}/{FILENAME.format(period=period)}"
        try:
            with urlopen(  # noqa: S310 - fixed trusted hosts
                Request(url, headers=HEADERS), timeout=90
            ) as response:
                if response.status != 200:
                    continue
                payload = response.read()
            if not payload.startswith(b"%PDF"):
                # Soft 404: a landing page dressed as a success.
                continue
        except (HTTPError, URLError, TimeoutError):
            continue
        finally:
            time.sleep(REQUEST_INTERVAL_SECONDS)

        path = CACHE / f"{period}.pdf"
        path.write_bytes(payload)
        return {
            "period": period,
            "url": url,
            "bytes": len(payload),
            "retrieved": date.today().isoformat(),
        }
    return None


def main() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    rows, missing = [], []
    for period in periods((2025, 1), (2026, 8)):
        record = fetch(period)
        if record is None:
            missing.append(period)
            continue
        rows.append(record)
        print(f"{period}  {record['bytes'] / 1048576:5.1f} MB")

    index = pd.DataFrame(rows)
    index.to_csv(MANIFEST, index=False)
    print(f"\n{len(rows)} reports cached in {CACHE.relative_to(ROOT)}")
    print(f"index written to {MANIFEST.relative_to(ROOT)}")
    if missing:
        print("unavailable: " + ", ".join(missing))


if __name__ == "__main__":
    main()
