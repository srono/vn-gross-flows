"""Build the manager-growth evidence pack from the corrected local panel.

Usage:
    python scripts/build_growth_research.py --fetch-macro
    python scripts/build_growth_research.py

The first form refreshes a local FRED cache.  The second is fully offline and is
what a reproducible publication build should use.  Raw global observations are
not published because source licences differ; output contains estimates and
source metadata only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from urllib.request import urlopen

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vngross.analysis import netting_loss, prepare_sample  # noqa: E402
from vngross.growth import (  # noqa: E402
    aggregate_investor_demand_monthly,
    aggregate_segment_demand,
    book_flow_conversion,
    flow_persistence,
    infer_investor_demand,
    investor_demand_persistence,
    investor_macro_sensitivity,
    investor_performance_response,
    manager_rotation_upper_bound,
    performance_horizon_response,
    regime_transition_table,
)

OUTPUT = ROOT / "data" / "output"
RESEARCH = OUTPUT / "growth_research"
MACRO_CACHE = ROOT / "data" / "interim" / "global_macro"
FRED_SERIES = {
    "VIXCLS": {
        "column": "vix_mean",
        "transform": "mean",
        "effect_scale": 10.0,
        "label": "CBOE VIX (10 index points)",
        "source": "CBOE via FRED",
        "url": "https://fred.stlouisfed.org/series/VIXCLS",
    },
    "SP500": {
        "column": "sp500_return",
        "transform": "return",
        "effect_scale": 0.10,
        "label": "S&P 500 monthly return (10 percentage points)",
        "source": "S&P Dow Jones Indices via FRED",
        "url": "https://fred.stlouisfed.org/series/SP500",
    },
    "DTWEXBGS": {
        "column": "broad_dollar_return",
        "transform": "return",
        "effect_scale": 0.01,
        "label": "Broad U.S. dollar index return (1 percentage point)",
        "source": "Federal Reserve Board via FRED",
        "url": "https://fred.stlouisfed.org/series/DTWEXBGS",
    },
    "DGS10": {
        "column": "us10y_pct",
        "transform": "last",
        "effect_scale": 1.0,
        "label": "U.S. 10-year Treasury yield (1 percentage point)",
        "source": "Federal Reserve Board via FRED",
        "url": "https://fred.stlouisfed.org/series/DGS10",
    },
    "FEDFUNDS": {
        "column": "fed_funds_pct",
        "transform": "last",
        "effect_scale": 1.0,
        "label": "Effective federal funds rate (1 percentage point)",
        "source": "Federal Reserve Board via FRED",
        "url": "https://fred.stlouisfed.org/series/FEDFUNDS",
    },
}


UNPUBLISHED_OUTPUTS = frozenset(
    {
        "vinacapital_performance_case_study.csv",
        "book_flow_conversion_by_manager_asset.csv",
        "fee_vs_retention_manager_spread.csv",
    }
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fetch_macro() -> None:
    MACRO_CACHE.mkdir(parents=True, exist_ok=True)
    for series in FRED_SERIES:
        url = (
            "https://fred.stlouisfed.org/graph/fredgraph.csv?"
            f"id={series}&cosd=2021-01-01&coed=2026-08-31"
        )
        with urlopen(url, timeout=60) as response:  # noqa: S310 - fixed trusted host
            data = response.read()
        (MACRO_CACHE / f"{series}.csv").write_bytes(data)


def _macro_monthly() -> tuple[pd.DataFrame, list[dict[str, object]]]:
    deposit = pd.read_csv(ROOT / "data" / "deposit_rate_12m.csv")
    macro = deposit[["month", "rate_pct", "observed"]].rename(
        columns={"rate_pct": "deposit_rate_pct", "observed": "deposit_observed"}
    )
    macro["deposit_observed"] = (
        macro["deposit_observed"].astype(str).str.lower().eq("true")
    )
    sources: list[dict[str, object]] = [
        {
            "series": "deposit_rate_pct",
            "label": "Vietnam 12-month deposit rate (1 percentage point)",
            "source": "curated multi-source series; row-level provenance in data/deposit_rate_12m.csv",
            "url": "data/deposit_rate_12m.csv",
            "transform": "monthly level and first difference",
            "published_raw_observations": True,
        }
    ]
    for series, config in FRED_SERIES.items():
        path = MACRO_CACHE / f"{series}.csv"
        if not path.exists():
            raise FileNotFoundError(
                f"{path} is missing; run with --fetch-macro once, then rebuild offline"
            )
        data = pd.read_csv(path)
        data["observation_date"] = pd.to_datetime(data["observation_date"])
        data[series] = pd.to_numeric(data[series], errors="coerce")
        data["month"] = data["observation_date"].dt.to_period("M").astype(str)
        grouped = data.groupby("month")[series]
        if config["transform"] == "mean":
            monthly = grouped.mean()
        elif config["transform"] == "last":
            monthly = grouped.last()
        else:
            monthly = grouped.agg(
                lambda values: (
                    values.dropna().iloc[-1] / values.dropna().iloc[0] - 1
                    if values.notna().sum()
                    else np.nan
                )
            )
        macro = macro.merge(
            monthly.rename(config["column"]).reset_index(), on="month", how="outer"
        )
        sources.append(
            {
                "series": series,
                "analysis_column": config["column"],
                "label": config["label"],
                "source": config["source"],
                "url": config["url"],
                "transform": config["transform"],
                "published_raw_observations": False,
                "cache_sha256": _sha256(path),
            }
        )
    macro = macro.sort_values("month")
    macro["deposit_rate_change_pp"] = macro["deposit_rate_pct"].diff()
    macro["deposit_change_observed"] = (
        macro["deposit_observed"] & macro["deposit_observed"].shift(1).fillna(False)
    )
    macro["us10y_change_pp"] = macro["us10y_pct"].diff()
    macro["fed_funds_change_pp"] = macro["fed_funds_pct"].diff()
    return macro, sources


def _regression_frame(results: dict[int, dict[str, object]], specification: str) -> pd.DataFrame:
    rows = []
    for horizon, legs in results.items():
        for leg, result in legs.items():
            term = next(iter(result.coefficients))
            rows.append(
                {
                    "specification": specification,
                    "horizon_months": horizon,
                    "dependent": leg,
                    "term": term,
                    "coefficient_percentage_points": result.coefficients[term],
                    "std_error": result.std_errors[term],
                    "t_stat": result.t_stats()[term],
                    "n_observations": result.n_obs,
                    "n_funds": result.n_funds,
                    "n_months": result.n_clusters,
                    "fixed_effects": "+".join(result.absorbed),
                    "cluster": result.cluster_on,
                    "note": result.note,
                }
            )
    return pd.DataFrame(rows)


def _performance_outputs(monthly: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    core = pd.concat(
        [
            _regression_frame(performance_horizon_response(monthly), "raw"),
            _regression_frame(
                performance_horizon_response(monthly, winsor=0.01), "winsor_1pct"
            ),
        ],
        ignore_index=True,
    )
    robust = []
    comparable = monthly[
        monthly["asset_class"].isin(["equity", "balanced"])
        & monthly["fund_code"].ne("DCIP")
    ]
    for fund in sorted(comparable["fund_code"].dropna().unique()):
        table = _regression_frame(
            performance_horizon_response(monthly, exclude_funds=[fund]),
            "leave_one_fund_out",
        )
        table["excluded"] = fund
        robust.append(table)
    for manager in sorted(comparable["manager_id"].dropna().unique()):
        table = _regression_frame(
            performance_horizon_response(monthly, exclude_managers=[manager]),
            "leave_one_manager_out",
        )
        table["excluded"] = manager
        robust.append(table)
    return core, pd.concat(robust, ignore_index=True)


def _persistence_frame(monthly: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for sample in ("all", "equity_balanced", "bond", "vinacapital"):
        for leg, result in flow_persistence(monthly, sample=sample, winsor=0.01).items():
            term = next(iter(result.coefficients))
            rows.append(
                {
                    "sample": sample,
                    "leg": leg,
                    "lag_coefficient": result.coefficients[term],
                    "std_error": result.std_errors[term],
                    "t_stat": result.t_stats()[term],
                    "n_observations": result.n_obs,
                    "n_funds": result.n_funds,
                    "n_months": result.n_clusters,
                }
            )
    return pd.DataFrame(rows)


def _segment_profile(monthly_demand: pd.DataFrame) -> pd.DataFrame:
    frame = monthly_demand[
        monthly_demand["fund_month_order"].gt(4)
        & monthly_demand["fund_code"].ne("DCIP")
    ].copy()
    frame["segments_opposite_sign"] = (
        np.sign(frame["foreign_unit_demand_vnd"])
        * np.sign(frame["domestic_unit_demand_vnd"])
        < 0
    )
    return (
        frame.groupby(["fund_code", "manager_id", "asset_class"], observed=True)
        .agg(
            n_months=("month", "size"),
            sample_start=("month", "min"),
            sample_end=("month", "max"),
            median_opening_foreign_share=("opening_foreign_share", "median"),
            foreign_net_unit_demand_vnd=("foreign_unit_demand_vnd", "sum"),
            domestic_net_unit_demand_vnd=("domestic_unit_demand_vnd", "sum"),
            opposite_sign_months=("segments_opposite_sign", "sum"),
            opposite_sign_share=("segments_opposite_sign", "mean"),
        )
        .reset_index()
    )


def _split_exclusion_category(reason: object) -> str:
    """Collapse written split failures into stable publication categories."""
    text = str(reason or "")
    if text.startswith("non-contiguous filing pair"):
        return "non_contiguous_filing_pair"
    if text.startswith("critical fields missing"):
        return "critical_fields_missing"
    if "stock missing" in text or text.startswith("first observed row"):
        return "stock_history_missing"
    if "foreign_units" in text and "exceed total" in text:
        return "foreign_units_exceed_total_units"
    if "foreign_value identity" in text:
        return "foreign_value_identity"
    if "prior foreign units disagree" in text:
        return "prior_foreign_units_mismatch"
    if "unit-change/net-flow proxy error" in text:
        return "unit_flow_proxy_error"
    if text.startswith("NAV identity residual"):
        return "nav_identity_residual"
    return "other"


def _split_exclusion_summary(split: pd.DataFrame) -> pd.DataFrame:
    invalid = split[~split["split_valid"].fillna(False).astype(bool)].copy()
    if invalid.empty:
        return pd.DataFrame(
            columns=["check_category", "n_periods", "share_of_invalid", "example_reason"]
        )
    invalid["check_category"] = invalid["split_reason"].map(_split_exclusion_category)
    summary = (
        invalid.groupby("check_category", observed=True)
        .agg(
            n_periods=("split_reason", "size"),
            example_reason=("split_reason", "first"),
        )
        .reset_index()
        .sort_values(["n_periods", "check_category"], ascending=[False, True])
    )
    summary["share_of_invalid"] = summary["n_periods"] / len(invalid)
    return summary[
        ["check_category", "n_periods", "share_of_invalid", "example_reason"]
    ]


def _hidden_churn(period: pd.DataFrame) -> pd.DataFrame:
    # Winsorised, like every other reading. `corr_net_churn` and the
    # subscription/redemption volatility ratio are both non-robust, and one row
    # decides them here: VLGF subscribed 1,380% of its own NAV in the week to
    # 2022-04-06. Unwinsorised that single observation drags the correlation to
    # 0.99 and the volatility ratio to 13.2, which would say net flow explains
    # essentially all gross activity and quietly contradict the finding this
    # table exists to support. The inception filter cannot catch it: VLGF had
    # been filing for three months before it took the mandate, so the row is not
    # an opening week.
    #
    # The quiet-week statistics are unaffected either way, because clipping at
    # the 99th percentile never touches a week whose net flow is inside 0.1%.
    sample, _ = prepare_sample(period, time_col="period_end")
    sample = sample[sample["fund_code"].ne("DCIP")]
    rows = []
    for label, frame in (
        ("all_comparable", sample),
        ("equity_balanced", sample[sample["asset_class"].isin(["equity", "balanced"])]),
        ("vinacapital", sample[sample["manager_id"].eq("vinacapital")]),
    ):
        statistics = netting_loss(frame)
        disclosed = (
            frame["gross_legs_disclosed"].fillna(False).astype(bool)
            if "gross_legs_disclosed" in frame
            else pd.Series(True, index=frame.index)
        )
        quiet = frame[
            disclosed
            & pd.to_numeric(frame["net_flow_rate"], errors="coerce").abs().lt(0.001)
            & pd.to_numeric(frame["churn_rate"], errors="coerce").notna()
        ]
        example: dict[str, object] = {}
        if not quiet.empty:
            maximum = quiet.loc[pd.to_numeric(quiet["churn_rate"]).idxmax()]
            example = {
                "max_quiet_fund_code": maximum.get("fund_code"),
                "max_quiet_period_end": maximum.get("period_end"),
                "max_quiet_net_flow_rate": maximum.get("net_flow_rate"),
                "max_quiet_churn_rate": maximum.get("churn_rate"),
                "max_quiet_source": maximum.get("source"),
            }
        rows.append({"sample": label, **statistics, **example})
    return pd.DataFrame(rows)


def build(fetch_macro: bool = False) -> dict[str, object]:
    if fetch_macro:
        _fetch_macro()
    RESEARCH.mkdir(parents=True, exist_ok=True)
    period_path = OUTPUT / "vngross_fund_period.csv"
    month_path = OUTPUT / "vngross_fund_month.csv"
    period = pd.read_csv(period_path)
    monthly = pd.read_csv(month_path)

    split = infer_investor_demand(period)
    monthly_demand = aggregate_investor_demand_monthly(split)
    split.to_csv(RESEARCH / "investor_net_demand_period.csv", index=False)
    split.loc[~split["split_valid"]].to_csv(
        RESEARCH / "investor_net_demand_exclusions.csv", index=False
    )
    _split_exclusion_summary(split).to_csv(
        RESEARCH / "investor_net_demand_exclusion_summary.csv", index=False
    )
    monthly_demand.to_csv(RESEARCH / "investor_net_demand_monthly.csv", index=False)

    core, robustness = _performance_outputs(monthly)
    core.to_csv(RESEARCH / "performance_acquisition_response.csv", index=False)
    robustness.to_csv(RESEARCH / "performance_response_robustness.csv", index=False)
    vinacapital_performance = _regression_frame(
        performance_horizon_response(
            monthly[monthly["manager_id"].eq("vinacapital")], winsor=0.01
        ),
        "vinacapital_case_study_winsor_1pct",
    )
    vinacapital_performance["case_study_caveat"] = (
        "small manager-only peer set; useful for internal hypothesis generation, not industry generalisation"
    )
    vinacapital_performance.to_csv(
        RESEARCH / "vinacapital_performance_case_study.csv", index=False
    )
    _persistence_frame(monthly).to_csv(RESEARCH / "flow_persistence.csv", index=False)

    transitions = pd.concat(
        [
            regime_transition_table(monthly, sample=sample)
            for sample in ("all", "equity_balanced", "bond", "vinacapital")
        ],
        ignore_index=True,
    )
    transitions.to_csv(RESEARCH / "outflow_regime_transitions.csv", index=False)
    book_flow_conversion(monthly, by="fund").to_csv(
        RESEARCH / "book_flow_conversion_by_fund.csv", index=False
    )
    book_flow_conversion(monthly, by=("manager_id", "asset_class")).to_csv(
        RESEARCH / "book_flow_conversion_by_manager_asset.csv", index=False
    )
    common_conversion = book_flow_conversion(
        monthly,
        by=("manager_id", "asset_class"),
        start_month="2022-11",
        end_month="2025-03",
    )
    common_conversion["window_rationale"] = (
        "common mature equity/balanced lineup overlap before VLGF coverage ends"
    )
    common_conversion.to_csv(
        RESEARCH / "book_flow_conversion_common_window.csv", index=False
    )
    scenarios = common_conversion.copy()
    scenarios["illustrative_10pct_redemption_reduction_vnd"] = (
        scenarios["total_redemptions_vnd"] * 0.10
    )
    scenarios["illustrative_annualised_redemption_reduction_vnd"] = (
        scenarios["illustrative_10pct_redemption_reduction_vnd"]
        * 12
        / scenarios["n_months"]
    )
    scenarios["illustrative_10pp_conversion_gain_vnd"] = (
        scenarios["total_subscriptions_vnd"] * 0.10
    )
    scenarios["illustrative_annualised_conversion_gain_vnd"] = (
        scenarios["illustrative_10pp_conversion_gain_vnd"]
        * 12
        / scenarios["n_months"]
    )
    scenarios["scenario_caveat"] = (
        "mechanical arithmetic only; not a forecast, causal estimate, or achievable target"
    )
    scenarios.to_csv(RESEARCH / "illustrative_aum_scenarios.csv", index=False)
    offsets = manager_rotation_upper_bound(monthly)
    offsets.to_csv(
        RESEARCH / "sibling_fund_offset_upper_bound.csv", index=False
    )
    offset_summary = (
        offsets.groupby("manager_id", observed=True)
        .agg(
            mixed_months=("month", "size"),
            first_month=("month", "min"),
            last_month=("month", "max"),
            offset_upper_bound_vnd=("sibling_offset_upper_bound_vnd", "sum"),
            outflow_in_mixed_months_vnd=("negative_net_flow_abs_vnd", "sum"),
        )
        .reset_index()
    )
    offset_summary["upper_bound_share_of_mixed_month_outflow"] = (
        offset_summary["offset_upper_bound_vnd"]
        / offset_summary["outflow_in_mixed_months_vnd"]
    )
    offset_summary["metric_scope"] = "upper_bound_not_observed_customer_transfers"
    offset_summary.to_csv(RESEARCH / "sibling_fund_offset_summary.csv", index=False)
    _hidden_churn(period).to_csv(RESEARCH / "hidden_gross_churn.csv", index=False)

    segment_response = []
    for sample in ("all", "equity_balanced", "vinacapital"):
        segment_response.append(
            _regression_frame(
                investor_performance_response(
                    monthly, monthly_demand, sample=sample
                ),
                f"{sample}_validated_net_unit_demand",
            )
        )
    pd.concat(segment_response, ignore_index=True).to_csv(
        RESEARCH / "investor_segment_performance_response.csv", index=False
    )
    segment_persistence = []
    for sample in ("all", "equity_balanced", "bond", "vinacapital"):
        for segment, result in investor_demand_persistence(
            monthly_demand, sample=sample
        ).items():
            term = next(iter(result.coefficients))
            segment_persistence.append(
                {
                    "sample": sample,
                    "segment": segment,
                    "lag_coefficient": result.coefficients[term],
                    "std_error": result.std_errors[term],
                    "t_stat": result.t_stats()[term],
                    "n_observations": result.n_obs,
                    "n_funds": result.n_funds,
                    "n_months": result.n_clusters,
                    "note": result.note,
                }
            )
    pd.DataFrame(segment_persistence).to_csv(
        RESEARCH / "investor_segment_persistence.csv", index=False
    )
    _segment_profile(monthly_demand).to_csv(
        RESEARCH / "investor_segment_profile.csv", index=False
    )

    macro, macro_sources = _macro_monthly()
    predictor_scales = {
        "deposit_rate_pct": 1.0,
        "deposit_rate_change_pp": 1.0,
        "vix_mean": 10.0,
        "sp500_return": 0.10,
        "broad_dollar_return": 0.01,
        "us10y_pct": 1.0,
        "us10y_change_pp": 1.0,
        "fed_funds_pct": 1.0,
        "fed_funds_change_pp": 1.0,
    }
    observed = {
        "deposit_rate_pct": "deposit_observed",
        "deposit_rate_change_pp": "deposit_change_observed",
    }
    macro_results = []
    for sample in ("all", "equity_balanced", "bond", "vinacapital"):
        for controls in ((), ("trend", "local_market_return")):
            result = investor_macro_sensitivity(
                monthly_demand,
                macro,
                predictor_scales=predictor_scales,
                sample=sample,
                observed_only=observed,
                controls=controls,
            )
            result["specification"] = (
                "uncontrolled" if not controls else "trend_and_local_market"
            )
            macro_results.append(result)
    pd.concat(macro_results, ignore_index=True).to_csv(
        RESEARCH / "investor_segment_macro_sensitivity.csv", index=False
    )
    pd.DataFrame(macro_sources).to_csv(RESEARCH / "macro_source_metadata.csv", index=False)

    aggregates = []
    for sample in ("all", "equity_balanced", "bond", "vinacapital"):
        aggregate = aggregate_segment_demand(monthly_demand, sample=sample)
        aggregate["foreign_cumulative_net_demand_vnd"] = aggregate[
            "foreign_unit_demand_vnd"
        ].cumsum()
        aggregate["domestic_cumulative_net_demand_vnd"] = aggregate[
            "domestic_unit_demand_vnd"
        ].cumsum()
        aggregates.append(aggregate)
    pd.concat(aggregates, ignore_index=True).to_csv(
        RESEARCH / "investor_segment_monthly_aggregate.csv", index=False
    )

    # The manifest pins the *published* outputs. Three tables are a named
    # manager's scorecard rather than a description of the market, so they are
    # produced for local use, gitignored, and left out of the manifest: pinning
    # a hash for a file nobody else can obtain proves nothing and makes a clean
    # rebuild look broken.
    output_hashes = {
        path.name: _sha256(path)
        for path in sorted(RESEARCH.glob("*.csv"))
        if path.name not in UNPUBLISHED_OUTPUTS
    }
    manifest: dict[str, object] = {
        "period_panel_sha256": _sha256(period_path),
        "monthly_panel_sha256": _sha256(month_path),
        "period_rows": len(period),
        "monthly_rows": len(monthly),
        "economic_funds": int(period["fund_code"].nunique()),
        "investor_split_valid_periods": int(split["split_valid"].sum()),
        "investor_split_invalid_periods": int((~split["split_valid"]).sum()),
        "investor_split_valid_months": len(monthly_demand),
        "tests_expected_command": ".venv/bin/python -m pytest -q",
        "global_macro_raw_published": False,
        "output_hashes": output_hashes,
    }
    (RESEARCH / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch-macro", action="store_true")
    args = parser.parse_args()
    print(json.dumps(build(fetch_macro=args.fetch_macro), indent=2))


if __name__ == "__main__":
    main()
