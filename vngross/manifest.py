"""Build reproducibility metadata for generated output artifacts."""
from __future__ import annotations

import hashlib
import json
import platform
import subprocess
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pandas as pd


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(root: Path) -> tuple[str | None, bool | None]:
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip()
        dirty = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=root, text=True
            ).strip()
        )
        return commit, dirty
    except (OSError, subprocess.CalledProcessError):
        return None, None


def write_build_manifest(output: Path, managers: list[str], root: Path) -> dict:
    """Write counts and hashes only after all requested output stages succeed."""
    period_path = output / "vngross_fund_period.csv"
    month_path = output / "vngross_fund_month.csv"
    period = pd.read_csv(period_path)
    monthly = pd.read_csv(month_path)

    def rows(name: str) -> int:
        path = output / name
        if not path.exists() or path.stat().st_size == 0:
            return 0
        try:
            return len(pd.read_csv(path))
        except pd.errors.EmptyDataError:
            return 0

    commit, dirty = _git(root)
    packages = {}
    for package in ("pandas", "numpy", "openpyxl", "pdfplumber", "vnstock"):
        try:
            packages[package] = version(package)
        except PackageNotFoundError:
            packages[package] = None

    analysis_counts = {}
    for path in sorted(output.glob("analysis_*.csv")):
        frame = pd.read_csv(path)
        analysis_counts[path.name] = {
            "rows": len(frame),
            "n_obs": sorted(pd.to_numeric(frame.get("n_obs"), errors="coerce").dropna().unique().tolist())
            if "n_obs" in frame else [],
        }

    source_counts = {}
    for manager in managers:
        path = root / "data" / "interim" / f"refs_{manager}.json"
        if path.exists():
            source_counts[manager] = len(json.loads(path.read_text(encoding="utf-8")))

    manifest = {
        "build_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": commit,
        "git_dirty": dirty,
        "managers_included": managers,
        "source_reference_counts": source_counts,
        "hashes": {
            period_path.name: _sha256(period_path),
            month_path.name: _sha256(month_path),
        },
        "period_rows": len(period),
        "month_rows": len(monthly),
        "economic_fund_count": int(period["fund_code"].nunique()),
        "quarantine_count": rows("quarantine.csv"),
        "parse_failure_count": rows("parse_failures.csv"),
        "supersession_count": rows("superseded_duplicates.csv"),
        "continuity_count": rows("continuity_breaks.csv"),
        "analysis_sample_counts": analysis_counts,
        "versions": {"python": platform.python_version(), **packages},
    }
    path = output / "build_manifest.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
    return manifest
