"""Round 14 (ROUND14_PREREG.md): new factor family with literature signs (cfp, op, cop, sg), single trial.
New factor arrays are saved as data/cache/f_<name>.npy; factors.json is left unchanged (earlier rounds stay
reproducible).
usage: python lab_round14.py
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

import lab_round12 as R12
import r10_deploy as D

NEW = ["cfp", "op", "cop", "sg"]


def build_new(L) -> dict:
    import lab_factors2 as F2
    import pipeline as v1
    fr = pd.read_parquet(D.DATA / "fund_frames.parquet")
    cmap = F2.cik_map()
    mcap = L.df("mcap").astype(float)

    def annual(c):
        return F2.daily_from_frames(fr[(fr.concept == c) & (fr.period == "annual")], 100, cmap)

    def instant(c):
        return F2.daily_from_frames(fr[(fr.concept == c) & (fr.period == "q_instant")], 75, cmap)

    cfo, oi = annual("NetCashProvidedByUsedInOperatingActivities"), annual("OperatingIncomeLoss")
    rev = annual("Revenues").combine_first(annual("RevenueFromContractWithCustomerExcludingAssessedTax"))
    eq, assets = instant("StockholdersEquity"), instant("Assets")
    pos_eq, pos_as = eq.where(eq > 0), assets.where(assets > 0)
    raw = {"cfp": cfo / mcap, "op": oi / pos_eq, "cop": cfo / pos_as, "sg": -(rev / rev.shift(252) - 1)}
    raw = {k: v.replace([np.inf, -np.inf], np.nan).astype("float32") for k, v in raw.items()}
    U = L.df("U").astype(bool)
    cover = {k: float(v.where(U).notna().sum().sum() / U.sum().sum()) for k, v in raw.items()}
    ind = pd.Series(L._meta["industry"], index=L.SYMS).replace("NA", np.nan)
    for k, df in v1.preprocess(raw, U, ind, mcap).items():
        np.save(L.CACHE / f"f_{k}.npy", df.to_numpy(np.float32))
    return cover


def main() -> None:
    R12._init()
    L = R12._W["L"]
    cover = build_new(L)
    print("coverage inside the universe:", {k: round(v, 3) for k, v in cover.items()}, flush=True)
    base = tuple((k, 1.0) for k in D.PRIOR_SET)
    out = {"coverage": cover}
    for label, members in (("pstar", base), ("pstar15", base + tuple((k, 1.0) for k in NEW))):
        p, W, d = R12.portfolio(members)
        out[label] = {**D.windows(p, L), "avg_w_d77": float(W["D77"].mean()), "d77_sleeve": D.windows(d, L)}
        print(label, {w: round(out[label][w]["sharpe"], 3) for w in ("selection", "holdout", "full")},
              "| D77 alone", {w: round(out[label]["d77_sleeve"][w]["sharpe"], 3) for w in ("selection", "holdout", "full")},
              flush=True)
    a, b = out["pstar15"], out["pstar"]
    out["proposed"] = bool(a["selection"]["sharpe"] > b["selection"]["sharpe"] and a["holdout"]["sharpe"] > b["holdout"]["sharpe"]
                           and a["full"]["sharpe"] >= b["full"]["sharpe"] + 0.10)
    (D.LAB / "ROUND14.json").write_text(json.dumps(out, indent=2, default=float))
    print("proposed to the user:", out["proposed"], flush=True)


if __name__ == "__main__":
    main()
