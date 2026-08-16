"""Enumerate filing URLs from each manager's disclosure listing.

Never construct filing URLs by pattern. VCBF filenames carry an unpredictable
trailing sequence suffix (`vcbbcf_bc_ky_20221121_2.pdf`) and the year directory
in the path does not always match the filing's year, so enumeration from the
listing page is the only reliable route.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlencode, urljoin, urlsplit

import httpx
import yaml

from .fetch import USER_AGENT, MIN_INTERVAL_S

__all__ = [
    "FilingRef",
    "DeadRef",
    "load_sources",
    "manager_config",
    "discover_filings",
    "discover_all",
]

log = logging.getLogger(__name__)

SOURCES_PATH = Path(__file__).with_name("sources.yaml")


@dataclass(frozen=True)
class FilingRef:
    """One enumerated filing, before fetching."""

    manager_id: str
    fund_key: str
    fund_code: str
    url: str
    published: date | None = None
    title: str | None = None
    label: str | None = None
    filename_date: date | None = None

    def as_row(self) -> dict:
        return {
            "manager_id": self.manager_id,
            "fund_key": self.fund_key,
            "fund_code": self.fund_code,
            "url": self.url,
            "published": self.published,
            "title": self.title,
            "label": self.label,
            "filename_date": self.filename_date,
        }


@dataclass(frozen=True)
class DeadRef:
    """A listing entry that advertises a filing but links to nothing.

    Recorded rather than dropped: an unlinked filing is a hole in the archive,
    and a hole that nobody wrote down becomes a fabricated flow once the next
    filing's opening NAV absorbs the unobserved period.
    """

    manager_id: str
    fund_key: str | None
    title: str
    published: date | None
    reason: str

    def as_row(self) -> dict:
        return {
            "manager_id": self.manager_id,
            "fund_key": self.fund_key,
            "title": self.title,
            "published": self.published,
            "reason": self.reason,
        }


def load_sources(path: Path | None = None) -> dict:
    """Load the manager and fund registry."""
    with open(path or SOURCES_PATH, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def manager_config(manager_id: str, sources: dict | None = None) -> dict:
    sources = sources or load_sources()
    managers = sources.get("managers", {})
    if manager_id not in managers:
        raise KeyError(f"unknown manager {manager_id!r}; known: {sorted(managers)}")
    return managers[manager_id]


# --------------------------------------------------------------------------
# VCBF
# --------------------------------------------------------------------------

# Each listing entry is a .list-download block: title, published date, link.
_VCBF_ITEM_RE = re.compile(
    r'<div class="list-download">.*?<p>(?P<title>.*?)</p>.*?'
    r'<span class="date-time">(?P<date>.*?)</span>.*?'
    r'href="(?P<href>[^"]*)"',
    re.S,
)
_VCBF_PAGE_RE = re.compile(r"[?&]p=(\d+)")
_VCBF_DATE_RE = re.compile(
    r"Ngày\s+(\d{1,2})\s+tháng\s+(\d{1,2})\s+năm\s+(\d{4})", re.IGNORECASE
)
_FILENAME_DATE_RE = re.compile(r"(\d{4})(\d{2})(\d{2})")
_TAG_RE = re.compile(r"<[^>]+>")
_LABEL_RE = re.compile(r"(Tuần|Ky|Kỳ)\s*\d+\s*/\s*\d{4}", re.IGNORECASE)


def _clean(text: str) -> str:
    return " ".join(html.unescape(_TAG_RE.sub(" ", text)).split())


def _vcbf_published(text: str) -> date | None:
    match = _VCBF_DATE_RE.search(text)
    if not match:
        return None
    day, month, year = (int(g) for g in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _filename_date(url: str) -> date | None:
    match = _FILENAME_DATE_RE.search(url.rsplit("/", 1)[-1])
    if not match:
        return None
    year, month, day = (int(g) for g in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _fund_key_from_url(url: str, funds: dict) -> str | None:
    name = url.rsplit("/", 1)[-1].lower()
    for key in funds:
        if name.startswith(key.lower()):
            return key
    return None


def _fund_key_from_title(title: str, funds: dict) -> str | None:
    """Fall back to the short fund alias in the listing title.

    Titles read "Bao cao thay doi gia tri tai san rong Quy FIF - Tuan 47/2024",
    which is the only fund identifier available on a dead entry.
    """
    upper = title.upper()
    for key, meta in funds.items():
        for alias in (meta or {}).get("aliases", []):
            if re.search(rf"\bQU[YỸ]\s+{re.escape(alias)}\b", upper):
                return key
    return None


def _discover_vcbf(
    manager_id: str,
    cfg: dict,
    *,
    client: httpx.Client,
    max_pages: int,
) -> tuple[list[FilingRef], list[DeadRef]]:
    listing_url = cfg["listing_url"]
    base_url = cfg.get("base_url") or listing_url
    page_param = cfg.get("page_param", "p")
    funds: dict = cfg.get("funds") or {}

    refs: list[FilingRef] = []
    dead: list[DeadRef] = []
    seen_urls: set[str] = set()

    page = 1
    last_page = 1
    while page <= max_pages:
        url = listing_url if page == 1 else f"{listing_url}?{page_param}={page}"
        response = client.get(url)
        if response.status_code != 200:
            log.warning("listing page %s returned %s", page, response.status_code)
            break

        body = response.text
        items = list(_VCBF_ITEM_RE.finditer(body))
        if not items:
            log.info("listing page %s has no entries; stopping", page)
            break

        for item in items:
            title = _clean(item.group("title"))
            published = _vcbf_published(_clean(item.group("date")))
            href = html.unescape(item.group("href").strip())
            label_match = _LABEL_RE.search(title)
            label = label_match.group(0) if label_match else None

            if not href or href.lower().startswith("javascript"):
                dead.append(
                    DeadRef(
                        manager_id=manager_id,
                        fund_key=_fund_key_from_title(title, funds),
                        title=title,
                        published=published,
                        reason=f'listing entry links to {href!r}; no file published',
                    )
                )
                continue

            absolute = urljoin(base_url + "/", href.lstrip("/"))
            if absolute in seen_urls:
                continue
            seen_urls.add(absolute)

            fund_key = _fund_key_from_url(absolute, funds) or _fund_key_from_title(
                title, funds
            )
            if fund_key is None:
                dead.append(
                    DeadRef(
                        manager_id=manager_id,
                        fund_key=None,
                        title=title,
                        published=published,
                        reason=f"cannot map to a registered fund: {absolute}",
                    )
                )
                continue

            refs.append(
                FilingRef(
                    manager_id=manager_id,
                    fund_key=fund_key,
                    fund_code=(funds[fund_key] or {}).get("code", fund_key.upper()),
                    url=absolute,
                    published=published,
                    title=title,
                    label=label,
                    filename_date=_filename_date(absolute),
                )
            )

        last_page = max(
            [int(n) for n in _VCBF_PAGE_RE.findall(body)] or [last_page], default=page
        )
        if page >= last_page:
            log.info("reached last listing page %s", page)
            break
        page += 1

    return refs, dead


# --------------------------------------------------------------------------
# VinaCapital
# --------------------------------------------------------------------------

# The disclosure list is served by a WordPress admin-ajax action. The form is
# posted as a urlencoded blob under a `data` key, carrying a per-session CSRF
# token that has to be read off the page first.
_VC_CSRF_RE = re.compile(r'id="field_csrf_ft"[^>]*value="([^"]+)"')
_VC_PAGES_RE = re.compile(r'data-total="(\d+)"')
_VC_FILE_RE = re.compile(r'href="([^"]+\.(?:xlsx|xls|pdf))"', re.IGNORECASE)
# VinaCapital publishes two files per fund per day. Only the weekly one is the
# Appendix XXIV change report; the daily one is a point-in-time NAV snapshot
# that shares the title but not the line codes. Three naming conventions are in
# use across the funds, so match on the period word rather than the whole stem:
#   VEOF   20260811_VEOF_BC_Tuan_Ky-so_20260810.xlsx      / BC_Ngay
#   VESAF  20260714_VESAF_BC_Weekly_20260713.xlsx         / BC_Daily
#   VLBF   20260814-VLBF-NAV-TUAN-TU-11.08.2026-den-...   / NAV-NGAY
_VC_WEEKLY_RE = re.compile(r"[-_](?:tuan|tuần|weekly)[-_]", re.IGNORECASE)
_VC_DAILY_RE = re.compile(r"[-_](?:ngay|ngày|daily)[-_]", re.IGNORECASE)


def _discover_vinacapital(
    manager_id: str,
    cfg: dict,
    *,
    client: httpx.Client,
    max_pages: int,
) -> tuple[list[FilingRef], list[DeadRef]]:
    listing_url = cfg["listing_url"]
    ajax_url = cfg.get("ajax_url") or urljoin(listing_url, "/wp-admin/admin-ajax.php")
    funds: dict = cfg.get("funds") or {}

    page = client.get(listing_url)
    page.raise_for_status()
    token_match = _VC_CSRF_RE.search(page.text)
    if token_match is None:
        raise RuntimeError(
            f"no CSRF token on {listing_url}; the disclosure form has changed"
        )
    token = token_match.group(1)

    refs: list[FilingRef] = []
    dead: list[DeadRef] = []
    seen: set[str] = set()

    for fund_key, meta in funds.items():
        slug = (meta or {}).get("site_slug", fund_key)
        total = None
        page_no = 1
        while page_no <= max_pages:
            payload = urlencode(
                {
                    "txtfund": slug,
                    "txtcat": "nav",
                    "txtnextp": str(page_no),
                    "txtkeywords": "",
                    "field_csrf_ft": token,
                    "_wp_http_referer": urlsplit(listing_url).path,
                }
            )
            # The AJAX endpoint is slower and flakier than a static page, so
            # it gets its own retry loop; the shared client only paces GETs.
            response = None
            for attempt in range(4):
                try:
                    response = client.post(
                        ajax_url,
                        data={
                            "action": "filterdiscl" if page_no == 1 else "loaddiscl",
                            "data": payload,
                        },
                        headers={"X-Requested-With": "XMLHttpRequest"},
                        timeout=120.0,
                    )
                except httpx.HTTPError as exc:
                    log.warning(
                        "%s/%s page %s: %s, retrying",
                        manager_id, slug, page_no, type(exc).__name__,
                    )
                    time.sleep(3 * (attempt + 1))
                    continue
                if response.status_code == 200:
                    break
                log.warning(
                    "%s/%s page %s: HTTP %s", manager_id, slug, page_no,
                    response.status_code,
                )
                time.sleep(3 * (attempt + 1))
                response = None
            if response is None:
                log.warning("%s/%s page %s: giving up", manager_id, slug, page_no)
                break
            time.sleep(MIN_INTERVAL_S)

            body = response.text
            if total is None:
                totals = [int(n) for n in _VC_PAGES_RE.findall(body)]
                total = max(totals) if totals else 1
                log.info("%s/%s: %d disclosure pages", manager_id, slug, total)

            found_on_page = 0
            for href in _VC_FILE_RE.findall(body):
                absolute = urljoin(listing_url, html.unescape(href))
                if absolute in seen:
                    continue
                seen.add(absolute)
                name = absolute.rsplit("/", 1)[-1]

                if _VC_DAILY_RE.search(name):
                    continue  # point-in-time snapshot, not the change report
                if not _VC_WEEKLY_RE.search(name):
                    dead.append(
                        DeadRef(
                            manager_id=manager_id,
                            fund_key=fund_key,
                            title=name,
                            published=_filename_date(name),
                            reason="filename matches neither the weekly nor the daily pattern",
                        )
                    )
                    continue

                found_on_page += 1
                refs.append(
                    FilingRef(
                        manager_id=manager_id,
                        fund_key=fund_key,
                        fund_code=(meta or {}).get("code", fund_key.upper()),
                        url=absolute,
                        published=_filename_date(name),
                        title=name,
                        label=None,
                        # The trailing date is the period end; the leading one is
                        # the publication date.
                        filename_date=_vc_period_date(name),
                    )
                )

            if page_no >= (total or 1):
                break
            page_no += 1

    return refs, dead


_VC_TRAILING_DATE_RE = re.compile(r"(\d{8})(?:-\d+)?\.(?:xlsx|xls|pdf)$", re.IGNORECASE)
# VLBF spells the period out: "...-TU-11.08.2026-den-14.08.2026.xlsx".
_VC_DOTTED_END_RE = re.compile(
    r"den-(\d{2})\.(\d{2})\.(\d{4})", re.IGNORECASE
)


def _vc_period_date(name: str) -> date | None:
    """The period-end date, which is the trailing date in the filename.

    "20260811_VEOF_BC_Tuan_Ky-so_20260810.xlsx" is published on the 11th for the
    period ending the 10th.
    """
    dotted = _VC_DOTTED_END_RE.search(name)
    if dotted:
        day, month, year = (int(g) for g in dotted.groups())
        try:
            return date(year, month, day)
        except ValueError:
            return None

    match = _VC_TRAILING_DATE_RE.search(name)
    if not match:
        return _filename_date(name)
    year, month, day = (
        int(match.group(1)[0:4]),
        int(match.group(1)[4:6]),
        int(match.group(1)[6:8]),
    )
    try:
        return date(year, month, day)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# DCVFM
# --------------------------------------------------------------------------

# The current Salesforce site does not expose a replayable document endpoint.
# Its predecessor remains live, with every report page enumerated in static
# WordPress sitemaps. Cache that 50-sitemap walk because it changes rarely and
# costs one paced request per sitemap even before any report pages are visited.
_DCVFM_SITEMAP_RE = re.compile(r"/report-sitemap(?:\d+)?\.xml$", re.IGNORECASE)
_DCVFM_FILE_RE = re.compile(
    r'''href=["']([^"']+\.(?:pdf|xlsx|xls)(?:\?[^"']*)?)["']''', re.IGNORECASE
)
_DCVFM_WEEKLY_RE = re.compile(r"_BC_TUAN_", re.IGNORECASE)
_DCVFM_PERIOD_RE = re.compile(r"_BC_KY_", re.IGNORECASE)
_DCVFM_DAILY_RE = re.compile(r"_BC_NGAY_", re.IGNORECASE)
_DCVFM_OTHER_BC_RE = re.compile(r"_BC_", re.IGNORECASE)
# Current Vietnamese weekly slugs use "tuan"; one legacy English DCBC page
# uses "week". The filename remains authoritative after the page is opened.
_DCVFM_WEEKLY_PAGE_RE = re.compile(
    r"(?:^|[-_])(?:tuan|week)(?:[-_]|$)", re.IGNORECASE
)
_DCVFM_PAGES_CACHE = Path(__file__).resolve().parents[1] / "data/interim/dcvfm_pages.json"
_DCVFM_CACHE_MAX_AGE = timedelta(hours=24)


def _dcvfm_locations(body: str) -> list[str]:
    """Read <loc> values without depending on a sitemap namespace version."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise RuntimeError(f"invalid DCVFM sitemap XML: {exc}") from exc
    return [
        html.unescape((element.text or "").strip())
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1] == "loc" and (element.text or "").strip()
    ]


def _dcvfm_get(client: httpx.Client, url: str) -> httpx.Response:
    """Retry transient archive failures; an incomplete sitemap is not a result."""
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            response = client.get(url, timeout=120.0)
        except httpx.HTTPError as exc:
            last_error = exc
        else:
            if response.status_code == 200:
                return response
            last_error = RuntimeError(f"HTTP {response.status_code}")
            if response.status_code < 500:
                return response
        if attempt < 3:
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def _dcvfm_fund_key(page_url: str, funds: dict) -> str | None:
    slug = unquote(urlsplit(page_url).path.rstrip("/").rsplit("/", 1)[-1]).lower()
    for fund_key in funds:
        if re.search(
            rf"(?:^|[^a-z0-9]){re.escape(fund_key.lower())}(?:[^a-z0-9]|$)",
            slug,
        ):
            return fund_key
    return None


def _dcvfm_page_urls(
    sitemap_url: str,
    *,
    client: httpx.Client,
    max_pages: int,
) -> list[str]:
    if _DCVFM_PAGES_CACHE.exists():
        try:
            cached = json.loads(_DCVFM_PAGES_CACHE.read_text(encoding="utf-8"))
            created_at = datetime.fromisoformat(cached["created_at"])
            pages = cached["pages"]
            cache_age = datetime.now(timezone.utc) - created_at
            cache_is_current = (
                cached.get("sitemap_url") == sitemap_url
                and created_at.tzinfo is not None
                and timedelta(0) <= cache_age <= _DCVFM_CACHE_MAX_AGE
                and isinstance(pages, list)
                and bool(pages)
                and all(isinstance(url, str) for url in pages)
                and cached.get("page_count") == len(pages)
                and isinstance(cached.get("sitemap_count"), int)
                and 0 < cached["sitemap_count"] <= max_pages
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            log.warning("cannot use %s: %s; rebuilding", _DCVFM_PAGES_CACHE, exc)
        else:
            if cache_is_current:
                log.info("DCVFM: loaded %d report pages from cache", len(pages))
                return pages
            log.info("%s is stale or invalid; rebuilding", _DCVFM_PAGES_CACHE)

    index = _dcvfm_get(client, sitemap_url)
    if index.status_code != 200:
        raise RuntimeError(f"DCVFM sitemap index returned HTTP {index.status_code}")
    sitemap_urls = [
        url
        for url in _dcvfm_locations(index.text)
        if _DCVFM_SITEMAP_RE.search(urlsplit(url).path)
    ]
    if not sitemap_urls:
        raise RuntimeError("DCVFM sitemap index contains no report sitemaps")

    selected_sitemaps = sitemap_urls[:max_pages]
    page_urls: set[str] = set()
    for child_url in selected_sitemaps:
        child = _dcvfm_get(client, child_url)
        if child.status_code != 200:
            raise RuntimeError(
                f"DCVFM report sitemap returned HTTP {child.status_code}: {child_url}"
            )
        child_pages = _dcvfm_locations(child.text)
        if not child_pages:
            raise RuntimeError(f"DCVFM report sitemap contains no pages: {child_url}")
        page_urls.update(child_pages)

    pages = sorted(page_urls)
    if len(selected_sitemaps) == len(sitemap_urls):
        _DCVFM_PAGES_CACHE.parent.mkdir(parents=True, exist_ok=True)
        temporary = _DCVFM_PAGES_CACHE.with_suffix(".tmp")
        cache_record = {
            "sitemap_url": sitemap_url,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "sitemap_count": len(selected_sitemaps),
            "page_count": len(pages),
            "pages": pages,
        }
        temporary.write_text(
            json.dumps(cache_record, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        temporary.replace(_DCVFM_PAGES_CACHE)
        log.info(
            "DCVFM: cached %d report pages from %d sitemaps",
            len(pages),
            len(selected_sitemaps),
        )
    else:
        log.info(
            "DCVFM: enumerated %d report pages from a %d/%d sitemap subset; "
            "not caching a partial walk",
            len(pages),
            len(selected_sitemaps),
            len(sitemap_urls),
        )
    return pages


def _discover_dcvfm(
    manager_id: str,
    cfg: dict,
    *,
    client: httpx.Client,
    max_pages: int,
) -> tuple[list[FilingRef], list[DeadRef]]:
    sitemap_url = cfg["sitemap_url"]
    funds: dict = cfg.get("funds") or {}
    pages = _dcvfm_page_urls(sitemap_url, client=client, max_pages=max_pages)

    candidates = []
    for page_url in pages:
        fund_key = _dcvfm_fund_key(page_url, funds)
        slug = unquote(urlsplit(page_url).path.rstrip("/").rsplit("/", 1)[-1])
        if fund_key is not None and _DCVFM_WEEKLY_PAGE_RE.search(slug):
            candidates.append((page_url, fund_key))
    log.info(
        "DCVFM: %d/%d report pages are weekly candidates for registered funds",
        len(candidates),
        len(pages),
    )

    refs: list[FilingRef] = []
    dead: list[DeadRef] = []
    seen_files: set[str] = set()
    for index, (page_url, fund_key) in enumerate(candidates, start=1):
        try:
            response = _dcvfm_get(client, page_url)
        except RuntimeError as exc:
            dead.append(
                DeadRef(
                    manager_id=manager_id,
                    fund_key=fund_key,
                    title=page_url,
                    published=None,
                    reason=f"report page failed after retries: {exc}",
                )
            )
            continue
        if response.status_code != 200:
            dead.append(
                DeadRef(
                    manager_id=manager_id,
                    fund_key=fund_key,
                    title=page_url,
                    published=None,
                    reason=f"report page returned HTTP {response.status_code}",
                )
            )
            continue

        file_urls = {
            urljoin(page_url, html.unescape(href))
            for href in _DCVFM_FILE_RE.findall(response.text)
        }
        if not file_urls:
            dead.append(
                DeadRef(
                    manager_id=manager_id,
                    fund_key=fund_key,
                    title=page_url,
                    published=None,
                    reason="report page advertises a report but yields no file link",
                )
            )
            continue

        for file_url in sorted(file_urls):
            name = unquote(urlsplit(file_url).path.rsplit("/", 1)[-1])
            if _DCVFM_DAILY_RE.search(name):
                continue  # point-in-time NAV has no flows
            if _DCVFM_PERIOD_RE.search(name):
                # DCBF Ky files are valid subweekly changes, but they overlap
                # BC_TUAN windows and would double-count flows in one panel.
                continue
            if not _DCVFM_WEEKLY_RE.search(name):
                if _DCVFM_OTHER_BC_RE.search(name):
                    dead.append(
                        DeadRef(
                            manager_id=manager_id,
                            fund_key=fund_key,
                            title=name,
                            published=None,
                            reason=(
                                "unrecognised _BC_ filename; expected the included "
                                "_BC_TUAN_ or excluded _BC_Ky_/_BC_Ngay_"
                            ),
                        )
                    )
                continue

            file_fund_key = _fund_key_from_url(file_url, funds)
            if file_fund_key is None:
                dead.append(
                    DeadRef(
                        manager_id=manager_id,
                        fund_key=None,
                        title=name,
                        published=None,
                        reason="change-report filename has no registered fund prefix",
                    )
                )
                continue
            if file_fund_key != fund_key:
                dead.append(
                    DeadRef(
                        manager_id=manager_id,
                        fund_key=file_fund_key,
                        title=name,
                        published=None,
                        reason=(
                            f"page maps to {fund_key!r} but filename prefix maps to "
                            f"{file_fund_key!r}; filename identity used"
                        ),
                    )
                )
            if file_url in seen_files:
                continue
            seen_files.add(file_url)
            refs.append(
                FilingRef(
                    manager_id=manager_id,
                    fund_key=file_fund_key,
                    fund_code=(funds[file_fund_key] or {}).get(
                        "code", file_fund_key.upper()
                    ),
                    url=file_url,
                    published=None,
                    title=name,
                    label=None,
                    filename_date=_filename_date(name),
                )
            )

        if index % 100 == 0 or index == len(candidates):
            log.info("DCVFM: inspected %d/%d candidate report pages", index, len(candidates))

    return refs, dead


# --------------------------------------------------------------------------
# SSIAM
# --------------------------------------------------------------------------

# SSIAM embeds every document a fund has ever published on that fund's single
# information page, several thousand links, with no pagination at all.
_SSIAM_FILE_RE = re.compile(r'href="([^"]+\.(?:pdf|xlsx|xls))"', re.IGNORECASE)
# The weekly change report. SSIAM has cycled through several naming schemes:
#   SSIBF_Bao-cao-tuan-ve-thay-doi-GTTSR-quy-mo-PLXXIV-TT98-20231211.pdf
#   SSIBF_Bao-cao-ve-thay-doi-GTTSR-quy-mo-PLXXIV-TT98-2023-11-27.pdf
#   BCThayDoiGTTSR_TT98 tuan_SSIBF 01-07_06_2021_merged.pdf
# All of them say "thay doi" (change), which the point-in-time daily reports
# named "Giatritaisanrongquymo" do not.
_SSIAM_CHANGE_RE = re.compile(r"thay[\s_-]?doi|thaydoi", re.IGNORECASE)
_SSIAM_DATE_RES = (
    re.compile(r"(\d{4})(\d{2})(\d{2})"),
    re.compile(r"(\d{4})-(\d{2})-(\d{2})"),
    re.compile(r"(\d{2})[_.-](\d{2})[_.-](\d{4})"),
)


def _ssiam_date(name: str) -> date | None:
    """Best-effort period date from the filename.

    Only used for ordering and for reporting coverage. The authoritative dates
    always come from inside the document, which is why several of these naming
    schemes can coexist without consequence.
    """
    for index, pattern in enumerate(_SSIAM_DATE_RES):
        match = pattern.search(name)
        if not match:
            continue
        a, b, c = (int(g) for g in match.groups())
        year, month, day = (c, b, a) if index == 2 else (a, b, c)
        try:
            return date(year, month, day)
        except ValueError:
            continue
    return None


def _discover_ssiam(
    manager_id: str,
    cfg: dict,
    *,
    client: httpx.Client,
    max_pages: int,
) -> tuple[list[FilingRef], list[DeadRef]]:
    base_url = cfg.get("base_url", "https://ssiam.com.vn")
    funds: dict = cfg.get("funds") or {}

    refs: list[FilingRef] = []
    dead: list[DeadRef] = []
    seen: set[str] = set()

    for fund_key, meta in funds.items():
        page_url = urljoin(base_url, (meta or {}).get("page"))
        response = client.get(page_url)
        if response.status_code != 200:
            dead.append(
                DeadRef(
                    manager_id=manager_id,
                    fund_key=fund_key,
                    title=page_url,
                    published=None,
                    reason=f"fund page returned HTTP {response.status_code}",
                )
            )
            continue

        found = 0
        for href in _SSIAM_FILE_RE.findall(response.text):
            absolute = urljoin(base_url, html.unescape(href))
            if absolute in seen:
                continue
            seen.add(absolute)
            name = unquote(absolute.rsplit("/", 1)[-1])
            if not _SSIAM_CHANGE_RE.search(name):
                continue  # daily point-in-time report or an unrelated document
            found += 1
            refs.append(
                FilingRef(
                    manager_id=manager_id,
                    fund_key=fund_key,
                    fund_code=(meta or {}).get("code", fund_key.upper()),
                    url=absolute,
                    published=None,
                    title=name,
                    label=None,
                    filename_date=_ssiam_date(name),
                )
            )
        log.info("%s/%s: %d change reports", manager_id, fund_key, found)

    return refs, dead


_CRAWLERS = {
    "vcbf": _discover_vcbf,
    "vinacapital": _discover_vinacapital,
    "dcvfm": _discover_dcvfm,
    "ssiam": _discover_ssiam,
}


def discover_filings(
    manager_id: str,
    cfg: dict | None = None,
    *,
    max_pages: int = 200,
    client: httpx.Client | None = None,
) -> tuple[list[FilingRef], list[DeadRef]]:
    """Enumerate every filing a manager lists, plus every entry that links to nothing.

    Returns refs sorted by fund then date so downstream chaining is deterministic.
    """
    cfg = cfg or manager_config(manager_id)
    crawler_name = cfg.get("crawler")
    if crawler_name is None:
        raise NotImplementedError(
            f"manager {manager_id!r} has no crawler; status is "
            f"{cfg.get('status', 'unknown')}. Resolve its listing page first "
            f"(spec section 9) before including it in a run."
        )
    crawler = _CRAWLERS.get(crawler_name)
    if crawler is None:
        raise NotImplementedError(f"no crawler registered under {crawler_name!r}")

    owns_client = client is None
    if owns_client:
        client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=60.0,
        )
    try:
        # The listing itself is a host request like any other, so it is paced
        # by the same one-per-second budget as the filings.
        import time

        original_get = client.get

        def paced_get(*args, **kwargs):
            response = original_get(*args, **kwargs)
            time.sleep(MIN_INTERVAL_S)
            return response

        client.get = paced_get  # type: ignore[method-assign]
        refs, dead = crawler(manager_id, cfg, client=client, max_pages=max_pages)
    finally:
        if owns_client:
            client.close()

    refs.sort(key=lambda r: (r.fund_key, r.filename_date or date.min, r.url))
    return refs, dead


def discover_all(
    manager_ids: list[str] | None = None, sources: dict | None = None
) -> tuple[list[FilingRef], list[DeadRef]]:
    """Enumerate every verified manager, skipping those still unverified."""
    sources = sources or load_sources()
    managers = sources.get("managers", {})
    targets = manager_ids or [
        mid for mid, cfg in managers.items() if cfg.get("status") == "verified"
    ]

    refs: list[FilingRef] = []
    dead: list[DeadRef] = []
    for manager_id in targets:
        found, missing = discover_filings(manager_id, managers[manager_id])
        log.info("%s: %d filings, %d dead entries", manager_id, len(found), len(missing))
        refs.extend(found)
        dead.extend(missing)
    return refs, dead
