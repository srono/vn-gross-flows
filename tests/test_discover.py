from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from vngross import discover


class Response:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code


class Client:
    def __init__(self, responses: dict[str, Response]):
        self.responses = responses
        self.requested: list[str] = []

    def get(self, url: str, **kwargs) -> Response:
        self.requested.append(url)
        return self.responses[url]


def test_dcvfm_enumerates_change_files_and_caches_sitemap_walk(
    tmp_path: Path, monkeypatch
) -> None:
    sitemap = "https://archive.test/sitemap.xml"
    child_one = "https://archive.test/report-sitemap.xml"
    child_two = "https://archive.test/report-sitemap2.xml"
    weekly_page = "https://archive.test/r/dcds-bao-cao-tuan-2025/"
    daily_page = "https://archive.test/r/bao-cao-ngay-cua-quy-dcds/"
    period_page = "https://archive.test/r/dcbf-bao-cao-ky-2025/"
    dead_page = "https://archive.test/r/dcde-bao-cao-tuan-2025/"
    mismatch_page = "https://archive.test/r/dcds-mislabeled-bao-cao-tuan-2025/"
    unknown_page = "https://archive.test/r/dcds-bao-cao-tuan-unknown-2025/"
    unrelated_page = "https://archive.test/r/etf-bao-cao-tuan-2025/"

    index_xml = f"""<sitemapindex>
      <sitemap><loc>{child_one}</loc></sitemap>
      <sitemap><loc>{child_two}</loc></sitemap>
      <sitemap><loc>https://archive.test/post-sitemap.xml</loc></sitemap>
    </sitemapindex>"""
    first_xml = f"""<urlset>
      <url><loc>{weekly_page}</loc></url>
      <url><loc>{daily_page}</loc></url>
      <url><loc>{period_page}</loc></url>
    </urlset>"""
    second_xml = f"""<urlset>
      <url><loc>{period_page}</loc></url>
      <url><loc>{dead_page}</loc></url>
      <url><loc>{mismatch_page}</loc></url>
      <url><loc>{unknown_page}</loc></url>
      <url><loc>{unrelated_page}</loc></url>
    </urlset>"""
    weekly_file = "https://cdn.test/DCDS_BC_TUAN_20250116.xlsx"
    daily_file = "https://cdn.test/DCDS_BC_Ngay_20250116.xlsx"
    period_file = "https://cdn.test/DCBF_BC_Ky_20250116.xlsx"
    mismatch_file = "https://cdn.test/DCDE_BC_TUAN_20250116.xlsx"
    unknown_file = "https://cdn.test/DCDS_BC_THANG_20250116.xlsx"
    client = Client(
        {
            sitemap: Response(index_xml),
            child_one: Response(first_xml),
            child_two: Response(second_xml),
            weekly_page: Response(f'<a href="{weekly_file}">file</a>'),
            daily_page: Response(f'<a href="{daily_file}">file</a>'),
            period_page: Response(
                f'<a href="{period_file}">file</a><a href="{period_file}">duplicate</a>'
            ),
            dead_page: Response("<html>report without a download</html>"),
            mismatch_page: Response(f'<a href="{mismatch_file}">file</a>'),
            unknown_page: Response(f'<a href="{unknown_file}">file</a>'),
        }
    )
    cache = tmp_path / "dcvfm_pages.json"
    monkeypatch.setattr(discover, "_DCVFM_PAGES_CACHE", cache)
    cfg = {
        "sitemap_url": sitemap,
        "funds": {
            "dcds": {"code": "DCDS"},
            "dcbf": {"code": "DCBF"},
            "dcde": {"code": "DCDE"},
        },
    }

    refs, dead = discover._discover_dcvfm(
        "dcvfm", cfg, client=client, max_pages=200
    )

    assert {(ref.fund_key, ref.url) for ref in refs} == {
        ("dcds", weekly_file),
        ("dcde", mismatch_file),
    }
    assert all("_BC_Ngay_" not in ref.url for ref in refs)
    assert len(dead) == 3
    assert any(item.fund_key == "dcde" and "no file link" in item.reason for item in dead)
    assert any("filename identity used" in item.reason for item in dead)
    assert any("unrecognised _BC_ filename" in item.reason for item in dead)
    assert daily_page not in client.requested
    assert period_page not in client.requested
    assert unrelated_page not in client.requested
    cache_record = json.loads(cache.read_text(encoding="utf-8"))
    assert cache_record["sitemap_url"] == sitemap
    assert cache_record["sitemap_count"] == 2
    assert cache_record["page_count"] == 7

    cached_client = Client(
        {
            weekly_page: Response(f'<a href="{weekly_file}">file</a>'),
            daily_page: Response(f'<a href="{daily_file}">file</a>'),
            period_page: Response(f'<a href="{period_file}">file</a>'),
            dead_page: Response("<html>report without a download</html>"),
            mismatch_page: Response(f'<a href="{mismatch_file}">file</a>'),
            unknown_page: Response(f'<a href="{unknown_file}">file</a>'),
        }
    )
    cached_refs, _ = discover._discover_dcvfm(
        "dcvfm", cfg, client=cached_client, max_pages=200
    )
    assert {ref.url for ref in cached_refs} == {
        weekly_file,
        mismatch_file,
    }
    assert sitemap not in cached_client.requested
    assert child_one not in cached_client.requested
    assert child_two not in cached_client.requested


def test_dcvfm_stale_cache_is_refreshed(tmp_path: Path, monkeypatch) -> None:
    sitemap = "https://archive.test/sitemap.xml"
    child = "https://archive.test/report-sitemap.xml"
    current_page = "https://archive.test/r/dcds-bao-cao-tuan-current/"
    cache = tmp_path / "dcvfm_pages.json"
    cache.write_text(
        json.dumps(
            {
                "sitemap_url": sitemap,
                "created_at": (
                    datetime.now(timezone.utc) - timedelta(days=2)
                ).isoformat(),
                "sitemap_count": 1,
                "page_count": 1,
                "pages": ["https://archive.test/r/dcds-stale/"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(discover, "_DCVFM_PAGES_CACHE", cache)
    client = Client(
        {
            sitemap: Response(
                f"<sitemapindex><sitemap><loc>{child}</loc></sitemap></sitemapindex>"
            ),
            child: Response(f"<urlset><url><loc>{current_page}</loc></url></urlset>"),
        }
    )

    pages = discover._dcvfm_page_urls(sitemap, client=client, max_pages=200)

    assert pages == [current_page]
    assert client.requested == [sitemap, child]
    refreshed = json.loads(cache.read_text(encoding="utf-8"))
    assert refreshed["pages"] == [current_page]


def test_dcvfm_empty_child_sitemap_is_not_cached(tmp_path: Path, monkeypatch) -> None:
    sitemap = "https://archive.test/sitemap.xml"
    child = "https://archive.test/report-sitemap.xml"
    cache = tmp_path / "dcvfm_pages.json"
    monkeypatch.setattr(discover, "_DCVFM_PAGES_CACHE", cache)
    client = Client(
        {
            sitemap: Response(
                f"<sitemapindex><sitemap><loc>{child}</loc></sitemap></sitemapindex>"
            ),
            child: Response("<urlset></urlset>"),
        }
    )

    with pytest.raises(RuntimeError, match="contains no pages"):
        discover._dcvfm_page_urls(sitemap, client=client, max_pages=200)
    assert not cache.exists()


def test_dcvfm_full_cache_does_not_override_max_pages(
    tmp_path: Path, monkeypatch
) -> None:
    sitemap = "https://archive.test/sitemap.xml"
    child_one = "https://archive.test/report-sitemap.xml"
    child_two = "https://archive.test/report-sitemap2.xml"
    first_page = "https://archive.test/r/dcds-first/"
    second_page = "https://archive.test/r/dcds-second/"
    cache = tmp_path / "dcvfm_pages.json"
    cache.write_text(
        json.dumps(
            {
                "sitemap_url": sitemap,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "sitemap_count": 2,
                "page_count": 2,
                "pages": [first_page, second_page],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(discover, "_DCVFM_PAGES_CACHE", cache)
    client = Client(
        {
            sitemap: Response(
                f"""<sitemapindex>
                  <sitemap><loc>{child_one}</loc></sitemap>
                  <sitemap><loc>{child_two}</loc></sitemap>
                </sitemapindex>"""
            ),
            child_one: Response(f"<urlset><url><loc>{first_page}</loc></url></urlset>"),
        }
    )

    pages = discover._dcvfm_page_urls(sitemap, client=client, max_pages=1)

    assert pages == [first_page]
    assert client.requested == [sitemap, child_one]
    full_cache = json.loads(cache.read_text(encoding="utf-8"))
    assert full_cache["pages"] == [first_page, second_page]
