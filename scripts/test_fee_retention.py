"""Does the redemption fee schedule explain the cross-manager retention gap?

The book-flow conversion table ranks managers by redemptions per 100 subscribed
and reads like a league table of distribution skill. It cannot be read that way
until exit costs are held constant. A fund charging 2.5% to leave inside a year
retains book flow that an otherwise identical fund loses, and none of that
difference is anything the manager did well.

This is descriptive and says so. The fee schedule is one number per fund, so
the effective sample is the number of funds, not the number of fund-months. Ten
equity and balanced funds cannot support an inference about ten funds; what they
can do is show whether the fee ordering and the retention ordering are the same
ordering, which is enough to decide whether the league table means what it looks
like it means.

SSIBF is flagged rather than trusted: Fmarket reports a redemption fee that
rises tenfold after six months, and the prospectus states only a 3% cap and
defers the schedule to a separate announcement, so the anomaly cannot be
resolved from published documents.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "output" / "growth_research"

# Fmarket's SSIBF tiers rise with holding period, which is backwards, and no
# published document confirms them.
UNVERIFIED_FEE_FUNDS = {"SSIBF"}

# The window the book-flow conversion table already uses.
WINDOW = ("2022-11", "2025-03")


def rank_correlation(left: pd.Series, right: pd.Series) -> float:
    """Spearman via Pearson on ranks. pandas defers to scipy, which is not a
    dependency of this project and is not worth becoming one for ten funds."""
    return float(left.rank().corr(right.rank()))


def fee_curve(schedule: pd.DataFrame) -> dict[str, float]:
    """Redemption fee payable at 6, 12 and 18 months held, plus a 24m average."""
    tiers = schedule[
        schedule["fee_type"].eq("SELL") & schedule["tier_unit"].eq("MONTH")
    ].copy()
    if tiers.empty:
        return {}

    def payable(months: float) -> float | None:
        hit = tiers[
            tiers["tier_from"].le(months)
            & (tiers["tier_to"].isna() | tiers["tier_to"].gt(months))
        ]
        return float(hit["fee_pct"].iloc[0]) if len(hit) else None

    monthly = [payable(m) for m in range(24)]
    known = [v for v in monthly if v is not None]
    return {
        "fee_at_6m": payable(6),
        "fee_at_12m": payable(12),
        "fee_at_18m": payable(18),
        "mean_fee_first_24m": float(np.mean(known)) if known else None,
    }


def main() -> None:
    fees = pd.read_csv(ROOT / "data" / "fee_schedules.csv")
    monthly = pd.read_csv(ROOT / "data" / "output" / "vngross_fund_month.csv")

    rows = []
    for fund_code, schedule in fees.groupby("fund_code"):
        curve = fee_curve(schedule)
        if curve:
            rows.append({"fund_code": fund_code, **curve})
    fee_frame = pd.DataFrame(rows)

    window = monthly[
        monthly["month"].between(*WINDOW)
        & monthly["gross_legs_disclosed"].fillna(False)
        & monthly["asset_class"].ne("bond")
        & monthly["fund_code"].ne("DCIP")
    ]
    flows = (
        window.groupby(["fund_code", "manager_id", "asset_class"], as_index=False)
        .agg(
            subscriptions=("subscriptions", "sum"),
            redemptions=("redemptions", "sum"),
            mean_redemption_rate=("gross_redemption_rate", "mean"),
            n_months=("month", "nunique"),
        )
    )
    flows["redemptions_per_100_subs"] = (
        flows["redemptions"].abs() / flows["subscriptions"] * 100
    )

    profile_path = OUT / "investor_segment_profile.csv"
    if profile_path.exists():
        profile = pd.read_csv(profile_path)[
            ["fund_code", "median_opening_foreign_share"]
        ]
        flows = flows.merge(profile, on="fund_code", how="left")

    joined = flows.merge(fee_frame, on="fund_code", how="left")
    joined["fee_verified"] = ~joined["fund_code"].isin(UNVERIFIED_FEE_FUNDS)
    joined = joined.sort_values("redemptions_per_100_subs")
    joined.to_csv(OUT / "fee_vs_retention_by_fund.csv", index=False)

    usable = joined.dropna(subset=["mean_fee_first_24m"])
    usable = usable[usable["fee_verified"]]

    print(f"equity/balanced funds in window with a usable fee curve: {len(usable)}")
    print()
    show = usable[
        [
            "manager_id",
            "fund_code",
            "fee_at_6m",
            "fee_at_12m",
            "fee_at_18m",
            "mean_fee_first_24m",
            "median_opening_foreign_share",
            "redemptions_per_100_subs",
            "n_months",
        ]
    ]
    print(show.round(2).to_string(index=False))
    print()

    for column in (
        "mean_fee_first_24m",
        "fee_at_6m",
        "fee_at_12m",
        "median_opening_foreign_share",
    ):
        if column not in usable:
            continue
        pearson = usable[column].corr(usable["redemptions_per_100_subs"])
        spearman = rank_correlation(usable[column], usable["redemptions_per_100_subs"])
        print(
            f"{column:22} vs redemptions/100 subs: "
            f"pearson {pearson:+.2f}  spearman {spearman:+.2f}  (n={len(usable)})"
        )
    print()
    print("n is the number of funds. This is an ordering check, not an estimate.")
    print()

    # The league table ranks managers. If the spread inside a manager is wider
    # than the spread between managers, the ranking is describing fund mix.
    print("=== within-manager vs between-manager spread ===")
    spread = usable.groupby("manager_id")["redemptions_per_100_subs"].agg(
        ["min", "max", "mean", "count"]
    )
    spread["within_range"] = spread["max"] - spread["min"]
    print(spread.round(1).to_string())
    between = spread["mean"].max() - spread["mean"].min()
    widest = spread["within_range"].max()
    print()
    print(f"between-manager range of means : {between:5.1f}")
    print(f"widest within-manager range    : {widest:5.1f}")
    verdict = (
        "fund mix, not manager"
        if widest > between
        else "manager effect survives fund mix"
    )
    print(f"-> {verdict}")

    spread.to_csv(OUT / "fee_vs_retention_manager_spread.csv")


if __name__ == "__main__":
    main()
