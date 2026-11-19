"""Round 11 (ROUND11_PREREG.md): can a third sleeve raise the Sharpe of P*?
Uses the research cache and the round-10 bar files (data/research_snapshot), so B0 reproduces P* exactly.
usage: python lab_round11.py
"""
from __future__ import annotations

import json
import shutil

import numpy as np
import pandas as pd

import r10_deploy as D

SECTORS = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC"]


def tom_sleeve(L, close: pd.DataFrame, fwd: pd.DataFrame, vol: pd.DataFrame) -> pd.DataFrame:
    """SPY from the open of the last session of a month to the open of the 4th session of the next month.
    Row t's return runs open(t+1) -> open(t+2), so row t is on when session t+1 is the month's last session
    or one of its first three. Size set at entry (0.10 / 60-day vol, <= 38%), then left to drift."""
    allD = pd.Series(L.DATES, index=L.DATES)
    month = allD.dt.to_period("M")
    first = allD.groupby(month).cumcount() + 1
    last = allD.groupby(month).cumcount(ascending=False) + 1
    on = ((last.shift(-1) == 1) | (first.shift(-1) <= 3)).fillna(False)
    eq, prev, rows = 1e5, 0.0, []
    for t in L.DATES[L.DATES >= L.BT_START]:
        if on[t]:
            w = prev if prev > 0 else float(np.clip(np.nan_to_num(0.10 / vol.at[t, "SPY"]), 0.0, 0.38))
        else:
            w = 0.0
        dw = abs(w - prev)
        cost = dw * 2.0 / 1e4 + (0.99 / eq if dw > 1e-6 else 0.0)
        r = float(np.nan_to_num(fwd.at[t, "SPY"]))
        g = w * r
        net = g - cost
        eq *= 1 + net
        rows.append({"date": t, "net": net, "long": w, "short": 0.0})
        prev = w * (1 + r) / (1 + g)
    return pd.DataFrame(rows).set_index("date")


def main() -> None:
    L, F, P = D.research()
    snap = D.DATA / "research_snapshot"
    if (snap / "bars_daily.parquet").exists():                  # the exact bar files round 10 used
        if not (snap / "bars_extra_etfs.parquet").exists():
            shutil.copy2(D.DATA / "bars_extra_etfs.parquet", snap / "bars_extra_etfs.parquet")
        P.DATA = snap
    res = D.run_pstar(L, F, P)
    pick = pd.read_csv(D.LAB / "daily_R10_pick.csv", index_col=0, parse_dates=True)
    b0_check = float((res["port"] - pick["P"]).abs().max())
    print("B0 vs round-10 pick, max abs diff:", b0_check, flush=True)

    t9, _ = D.t9_record(P)
    d = res["d77"]
    sleeves = {"T9": t9[["net", "long", "short"]],
               "D77": pd.DataFrame({"net": d["net"], "long": d["long_gross"] + d["hedge"].clip(lower=0),
                                    "short": d["short_gross"] + (-d["hedge"]).clip(lower=0)})}
    bars = pd.read_parquet(P.DATA / "bars_daily.parquet", columns=["symbol", "date", "open", "close"])
    x = bars[bars.symbol.isin(SECTORS + ["SPY"])]
    close = x.pivot(index="date", columns="symbol", values="close").reindex(L.DATES)
    opn = x.pivot(index="date", columns="symbol", values="open").reindex(L.DATES)
    fwd = opn.shift(-2) / opn.shift(-1) - 1
    vol = close.pct_change().rolling(60, min_periods=40).std() * np.sqrt(252)
    mom = close.shift(21) / close.shift(252) - 1

    def sec_w(t):
        top = mom.loc[t, SECTORS].dropna().nlargest(3).index
        w = pd.Series(0.0, index=SECTORS)
        w[top] = (0.10 / vol.loc[t, top]) / 3
        return w

    sec, _ = P.etf_sleeve(sec_w, SECTORS, close, fwd)
    sleeves["SEC3"] = sec[["net", "long", "short"]]
    sleeves["TOM"] = tom_sleeve(L, close, fwd, vol)
    for k, s in sleeves.items():
        print(k, {w: round(v["sharpe"], 3) for w, v in D.windows(s["net"], L).items()}, flush=True)

    idx = sleeves["T9"].index
    for s in sleeves.values():
        idx = idx.intersection(s.index)
    sets = {"B0": ["T9", "D77"], "B1": ["T9", "D77", "SEC3"], "B2": ["T9", "D77", "TOM"],
            "B3": ["T9", "D77", "SEC3", "TOM"]}
    rows, series = [], {}
    for name, members in sets.items():
        R = pd.DataFrame({m: sleeves[m].loc[idx, "net"] for m in members})
        MU = pd.DataFrame({m: 0.5 * sleeves[m].loc[idx, "long"] + 0.6 * sleeves[m].loc[idx, "short"] for m in members})
        LU = pd.DataFrame({m: sleeves[m].loc[idx, "long"] for m in members})
        p, W = P.combine(R, MU, "MV", False, LU)
        series[name] = p
        m = D.windows(p, L)
        yearly = p.groupby(p.index.year).apply(lambda z: (1 + z).prod() - 1)
        rows.append({"set": name, "sleeves": "+".join(members), "sel_sharpe": m["selection"]["sharpe"],
                     "hold_sharpe": m["holdout"]["sharpe"], "full_sharpe": m["full"]["sharpe"],
                     "full_ann": m["full"]["ann"], "full_mdd": m["full"]["mdd"], "worst_year": yearly.loc[2022:].min(),
                     "corr_with_B0": np.nan, **{f"avg_w_{k}": v for k, v in W.mean().items()}})
    t = pd.DataFrame(rows)
    t["corr_with_B0"] = [float(series[s].corr(series["B0"])) for s in t["set"]]
    t.to_csv(D.LAB / "round11_sets.csv", index=False)
    print(t.round(3).to_string(index=False), flush=True)

    b0 = t.set_index("set").loc["B0"]
    cand = t[t.set != "B0"].sort_values("full_sharpe", ascending=False).iloc[0]
    passes = bool(cand["full_sharpe"] >= b0["full_sharpe"] + 0.10 and cand["sel_sharpe"] > b0["sel_sharpe"]
                  and cand["hold_sharpe"] > b0["hold_sharpe"])
    prev = pd.read_csv(D.LAB / "registry_val.csv")["sharpe"].dropna().to_numpy()
    r10 = pd.read_csv(D.LAB / "round10_combos.csv")["FULL_sharpe"].to_numpy()
    best = t.sort_values("full_sharpe", ascending=False).iloc[0]["set"]
    dsr = L.v1.deflated_sharpe(series[best], np.concatenate([prev, r10, t["full_sharpe"].to_numpy()]))
    out = {"b0_reproduces_pstar_max_abs_diff": b0_check,
           "sleeves_alone": {k: D.windows(s["net"], L) for k, s in sleeves.items() if k in ("SEC3", "TOM")},
           "sets": t.to_dict(orient="records"), "best_candidate": cand["set"],
           "passes_rules_i_ii": passes, "best_set_overall": best, "deflated_sharpe_best_all_trials": dsr,
           "decision": ("candidate goes to the realistic check (rule iii)" if passes else
                        "no candidate meets the pre-registered rule: P* stays, the search ends here")}
    (D.LAB / "ROUND11.json").write_text(json.dumps(out, indent=2, default=float))
    print(json.dumps({k: out[k] for k in ("best_candidate", "passes_rules_i_ii", "best_set_overall",
                                         "deflated_sharpe_best_all_trials", "decision")}, indent=2, default=float))


if __name__ == "__main__":
    main()
