"""Pipeline driver: discover, fetch, parse, gate, assemble.

Usage:
    python -m vngross.run discover [manager ...]
    python -m vngross.run fetch    [manager ...]
    python -m vngross.run parse    [manager ...]
    python -m vngross.run panel    [manager ...]

Each stage writes to data/interim or data/output and is safe to re-run: fetch
skips cached URLs and parse reads only from disk.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import pandas as pd

from .appendix_xxiv import Filing, parse_filing
from .discover import discover_filings, load_sources, manager_config
from .fetch import FetchError, fetch

log = logging.getLogger("vngross.run")

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
INTERIM = ROOT / "data" / "interim"
OUTPUT = ROOT / "data" / "output"


def _verified_managers() -> list[str]:
    sources = load_sources()
    return [
        mid
        for mid, cfg in sources.get("managers", {}).items()
        if cfg.get("status") == "verified"
    ]


def _refs_path(manager_id: str) -> Path:
    return INTERIM / f"refs_{manager_id}.json"


def stage_discover(managers: list[str]) -> None:
    INTERIM.mkdir(parents=True, exist_ok=True)
    for manager_id in managers:
        refs, dead = discover_filings(manager_id)
        _refs_path(manager_id).write_text(
            json.dumps([r.as_row() for r in refs], default=str, indent=1),
            encoding="utf-8",
        )
        pd.DataFrame([d.as_row() for d in dead]).to_csv(
            INTERIM / f"dead_{manager_id}.csv", index=False
        )
        log.info("%s: %d filings enumerated, %d dead entries", manager_id, len(refs), len(dead))


def stage_fetch(managers: list[str]) -> None:
    for manager_id in managers:
        refs = json.loads(_refs_path(manager_id).read_text(encoding="utf-8"))
        cache = RAW / manager_id
        cache.mkdir(parents=True, exist_ok=True)
        failures: list[dict] = []
        for index, ref in enumerate(refs, start=1):
            try:
                path = fetch(ref["url"], cache)
            except FetchError as exc:
                log.warning("fetch failed %s: %s", ref["url"], exc)
                failures.append({**ref, "error": str(exc)})
                continue
            ref["path"] = str(path.relative_to(ROOT))
            if index % 50 == 0 or index == len(refs):
                log.info("%s: fetched %d/%d", manager_id, index, len(refs))
        _refs_path(manager_id).write_text(
            json.dumps(refs, default=str, indent=1), encoding="utf-8"
        )
        if failures:
            pd.DataFrame(failures).to_csv(
                INTERIM / f"fetch_failures_{manager_id}.csv", index=False
            )
        log.info("%s: %d fetch failures", manager_id, len(failures))


def load_filings(manager_id: str) -> tuple[list[Filing], list[dict]]:
    """Parse every cached filing for a manager.

    The fund code comes from the registry via the filename prefix, never from
    the document: VCBF renamed VCBBCF to VCBF-BCF mid-archive, and keying on the
    printed code would split one fund in two.
    """
    refs = json.loads(_refs_path(manager_id).read_text(encoding="utf-8"))
    cfg = manager_config(manager_id)
    funds = cfg.get("funds") or {}

    filings: list[Filing] = []
    failures: list[dict] = []
    for ref in refs:
        if not ref.get("path"):
            failures.append({**ref, "error": "not fetched"})
            continue
        path = ROOT / ref["path"]
        fund_code = (funds.get(ref["fund_key"]) or {}).get("code") or ref["fund_code"]
        try:
            filing = parse_filing(path, fund_code=fund_code)
        except Exception as exc:  # noqa: BLE001 - one bad filing must not abort
            # Report the file by its repo-relative path. The absolute path is a
            # detail of whoever ran the pipeline, and it would otherwise be
            # published in parse_failures.csv.
            detail = str(exc).replace(f"{ROOT}/", "")
            failures.append({**ref, "error": f"{type(exc).__name__}: {detail}"})
            continue
        filing.source = ref["url"]
        filings.append(filing)
    return filings, failures


def stage_parse(managers: list[str]) -> None:
    from .panel import deduplicate
    from .reconcile import chain_continuity, reconcile, summarise

    for manager_id in managers:
        filings, failures = load_filings(manager_id)
        filings, superseded = deduplicate(filings)
        log.info(
            "%s: parsed %d unique, %d superseded duplicates, %d failed",
            manager_id, len(filings), len(superseded), len(failures),
        )
        if failures:
            pd.DataFrame(failures).to_csv(
                INTERIM / f"parse_failures_{manager_id}.csv", index=False
            )
        checks = [reconcile(f) for f in filings] + chain_continuity(filings)
        print(summarise(checks))
        pd.DataFrame([c.as_row() for c in checks]).to_csv(
            INTERIM / f"checks_{manager_id}.csv", index=False
        )


def stage_panel(managers: list[str]) -> None:
    from .macro import attach_all_macro, cross_check_nav_per_unit
    from .panel import build_fund_period_panel, to_monthly

    OUTPUT.mkdir(parents=True, exist_ok=True)
    all_filings: list[Filing] = []
    all_failures: list[dict] = []
    fund_meta: dict[str, dict] = {}
    for manager_id in managers:
        # Rule 4.5 applies to the panel stage too: a filing that cannot be
        # parsed is an exclusion and must be written down. Discarding this list
        # here once hid 255 VinaCapital VLBF filings, a whole fund, behind a
        # panel that looked complete.
        filings, failures = load_filings(manager_id)
        all_filings.extend(filings)
        all_failures.extend(failures)
        cfg = manager_config(manager_id)
        for key, meta in (cfg.get("funds") or {}).items():
            code = (meta or {}).get("code", key.upper())
            fund_meta[code] = {
                "manager_id": manager_id,
                "fund_key": key,
                "fund_name": (meta or {}).get("name"),
                "asset_class": (meta or {}).get("asset_class"),
            }

    if all_failures:
        pd.DataFrame(all_failures).to_csv(OUTPUT / "parse_failures.csv", index=False)
        by_fund = pd.DataFrame(all_failures).groupby("fund_key").size()
        log.warning(
            "%d filings did not parse and are excluded; see parse_failures.csv "
            "(by fund: %s)",
            len(all_failures),
            by_fund.to_dict(),
        )

    result = build_fund_period_panel(all_filings, fund_meta=fund_meta)
    panel = attach_all_macro(result.panel)

    panel.to_csv(OUTPUT / "vngross_fund_period.csv", index=False)
    result.quarantine.to_csv(OUTPUT / "quarantine.csv", index=False)
    result.superseded.to_csv(OUTPUT / "superseded_duplicates.csv", index=False)
    result.continuity_breaks.to_csv(OUTPUT / "continuity_breaks.csv", index=False)
    result.diagnostics.to_csv(OUTPUT / "measurement_error_diagnostics.csv", index=False)
    to_monthly(panel).to_csv(OUTPUT / "vngross_fund_month.csv", index=False)

    # Independent validation: Fmarket publishes NAV per certificate from its own
    # feed, so agreement is evidence about the parse that no internal check can
    # give. Failure here is reported, not fatal; the panel stands on the filings.
    try:
        checked = cross_check_nav_per_unit(panel)
        if not checked.empty:
            checked.to_csv(OUTPUT / "fmarket_cross_check.csv", index=False)
            matched = checked[checked["fmarket"].notna()]
            log.info(
                "Fmarket cross-check: %d/%d parsed NAV per unit agree within 0.1%%"
                " (%d rows have no Fmarket observation)",
                int(matched["agrees"].sum()),
                len(matched),
                len(checked) - len(matched),
            )
    except Exception as exc:  # noqa: BLE001 - cross-check is evidence, not a gate
        log.warning("Fmarket cross-check unavailable: %s", exc)

    log.info(
        "panel %d rows, quarantine %d, continuity breaks %d",
        len(panel),
        len(result.quarantine),
        len(result.continuity_breaks),
    )


def stage_analysis(managers: list[str]) -> None:
    """Run the paired net-versus-gross comparison over the built panel."""
    from .analysis import (
        add_lagged_performance,
        prepare_sample,
        compare,
        netting_loss,
        paired_specifications,
        quintile_table,
    )

    period = pd.read_csv(OUTPUT / "vngross_fund_period.csv")
    monthly = pd.read_csv(OUTPUT / "vngross_fund_month.csv")

    for label, frame, time_col, windows in (
        ("weekly", period, "period_end", (1, 4, 12)),
        ("monthly", monthly, "period_end", (1, 3, 6)),
    ):
        frame, filters = prepare_sample(frame, time_col=time_col)
        print(f"\nsample filters: {filters}")
        frame = add_lagged_performance(frame, windows=windows, time_col=time_col)
        performance = f"ret_lag1_{windows[1]}"
        print(f"\n{'=' * 72}\n{label.upper()}  (performance = {performance})\n{'=' * 72}")

        table = quintile_table(frame, performance)
        if not table.empty:
            print("\nmean flow rate by past-performance quintile, percent of NAV")
            print(table.to_string())

        try:
            results = paired_specifications(frame, performance=performance)
        except ValueError as exc:
            print(f"  specification not estimable: {exc}")
            continue

        print(f"\nsame equation, same sample, three dependent variables")
        print(compare(results, performance).to_string(index=False))
        for result in results.values():
            print()
            print(result.summary())

        estimates = pd.concat(
            [r.as_frame().assign(dependent=r.dependent, n_obs=r.n_obs)
             for r in results.values()],
            ignore_index=True,
        )
        estimates.to_csv(OUTPUT / f"analysis_paired_{label}.csv", index=False)

        stats = netting_loss(frame)
        if stats:
            print("\nwhat netting hides")
            for key, value in stats.items():
                print(f"    {key:<32} {value:,.4f}" if isinstance(value, float)
                      else f"    {key:<32} {value}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vngross.run")
    parser.add_argument(
        "stage",
        choices=["discover", "fetch", "parse", "panel", "analysis", "all"],
    )
    parser.add_argument("managers", nargs="*")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)

    managers = args.managers or _verified_managers()
    stages = {
        "discover": stage_discover,
        "fetch": stage_fetch,
        "parse": stage_parse,
        "panel": stage_panel,
        "analysis": stage_analysis,
    }
    for name in (["discover", "fetch", "parse", "panel", "analysis"]
        if args.stage == "all"
        else [args.stage]):
        log.info("=== stage %s: %s ===", name, ", ".join(managers))
        stages[name](managers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
