"""Round 15 (ROUND15_PREREG.md): investment and intangibles family (capxg, invg, rdm) on top of P*15, single trial.
Downloads three SEC XBRL frames concepts into data/fund_frames2.parquet (the round-6 file is left untouched);
new factor arrays are saved as data/cache/f_<name>.npy, factors.json unchanged.
usage: python lab_round15.py
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

import lab_round12 as R12
import r10_deploy as D

NEW = ["capxg", "invg", "rdm"]
FRAMES2 = D.DATA / "fund_frames2.parquet"


def download() -> pd.DataFrame:
    if FRAMES2.exists():
        return pd.read_parquet(FRAMES2)
    import fundamentals as FD                             # same client: generic User-Agent, <= 8 requests/s
    jobs = [(c, f"CY{y}", "annual") for c in ("PaymentsToAcquirePropertyPlantAndEquipment",
                                               "ResearchAndDevelopmentExpense") for y in range(2017, 2026)]
    jobs += [("InventoryNet", f"CY{y}Q{q}I", "q_instant") for y in range(2018, 2027) for q in range(1, 5)]
    rows = []
    for concept, per, kind in jobs:
        r = FD.get(f"https://data.sec.gov/api/xbrl/frames/us-gaap/{concept}/USD/{per}.json")
        if r is not None and r.status_code == 200:
            d = pd.DataFrame(r.json()["data"])[["cik", "end", "val"]]
            d["concept"], d["period"], d["frame"] = concept, kind, per
            rows.append(d)
    out = pd.concat(rows, ignore_index=True)
    out["end"] = pd.to_datetime(out["end"])
    out.to_parquet(FRAMES2)
    return out


def build_new(L) -> dict:
    import lab_factors2 as F2
    import pipeline as v1
    fr2, fr = download(), pd.read_parquet(D.DATA / "fund_frames.parquet")
    print("frames rows:", fr2.groupby("concept").size().to_dict(), flush=True)
    cmap = F2.cik_map()
    mcap = L.df("mcap").astype(float)

    def annual(f, c):
        return F2.daily_from_frames(f[(f.concept == c) & (f.period == "annual")], 100, cmap)

    def instant(f, c):
        return F2.daily_from_frames(f[(f.concept == c) & (f.period == "q_instant")], 75, cmap)

    capx = annual(fr2, "PaymentsToAcquirePropertyPlantAndEquipment")
    capx = capx.where(capx > 0)
    rd = annual(fr2, "ResearchAndDevelopmentExpense")
    inv = instant(fr2, "InventoryNet")
    assets = instant(fr, "Assets")
    pos_as = assets.where(assets > 0)
    raw = {"capxg": -(capx / capx.shift(252) - 1), "invg": -((inv - inv.shift(252)) / pos_as), "rdm": rd / mcap}
    raw = {k: v.replace([np.inf, -np.inf], np.nan).astype("float32") for k, v in raw.items()}
    U = L.df("U").astype(bool)
    cover = {k: float(v.where(U).notna().sum().sum() / U.sum().sum()) for k, v in raw.items()}
    ind = pd.Series(L._meta["industry"], index=L.SYMS).replace("NA", np.nan)
    for k, df in v1.preprocess(raw, U, ind, mcap).items():
        np.save(L.CACHE / f"f_{k}.npy", df.to_numpy(np.float32))
    return cover


def main() -> None:
    R12._init()
    L, F, P = R12._W["L"], R12._W["F"], R12._W["P"]
    cover = build_new(L)
    print("coverage inside the universe:", {k: round(v, 3) for k, v in cover.items()}, flush=True)
    base_names = D.VARIANTS["pstar15"][0]
    out = {"coverage": cover}
    for label, names in (("pstar15", base_names), ("pstar18", base_names + NEW)):
        p, W, d = R12.portfolio(tuple((k, 1.0) for k in names))
        out[label] = {**D.windows(p, L), "avg_w_d77": float(W["D77"].mean()), "d77_sleeve": D.windows(d, L)}
        print(label, {w: round(out[label][w]["sharpe"], 3) for w in ("selection", "holdout", "full")},
              "| D77 alone", {w: round(out[label]["d77_sleeve"][w]["sharpe"], 3) for w in ("selection", "holdout", "full")},
              flush=True)
    a, b = out["pstar18"], out["pstar15"]
    passes = bool(a["selection"]["sharpe"] > b["selection"]["sharpe"] and a["holdout"]["sharpe"] > b["holdout"]["sharpe"]
                  and a["full"]["sharpe"] >= b["full"]["sharpe"] + 0.10)
    out["passes_ideal_rule"] = passes
    if passes:                                             # realistic $100k check (rule iii)
        names = base_names + NEW
        sc = R12.composite(tuple((k, 1.0) for k in names))
        d77, _, _ = D.d77_record(L, F, sc)
        t9, _ = D.t9_record(P)
        idx = t9.index.intersection(d77.index)
        dl = d77.loc[idx, "long_gross"] + d77.loc[idx, "hedge"].clip(lower=0)
        ds = d77.loc[idx, "short_gross"] + (-d77.loc[idx, "hedge"]).clip(lower=0)
        R = pd.DataFrame({"T9": t9.loc[idx, "net"], "D77": d77.loc[idx, "net"]})
        MU = pd.DataFrame({"T9": 0.5 * t9.loc[idx, "long"] + 0.6 * t9.loc[idx, "short"], "D77": 0.5 * dl + 0.6 * ds})
        k18 = 0.9 / float((D.mv_raw(P, R) * MU).sum(axis=1).max())
        D.VARIANTS["pstar18"] = (names, k18, "R15")
        reals = {}
        for v in ("pstar15", "pstar18"):
            res = D.run_pstar(L, F, P, variant=v)
            sim, extra = D.simulate(res, L)
            reals[v] = D._sim_report(sim, extra, L)
            print(v, "realistic", {w: round(x["sharpe"], 3) for w, x in reals[v]["metrics"].items()}, flush=True)
        out["k_scale_pstar18"] = k18
        out["realistic"] = reals
        out["proposed"] = bool(reals["pstar18"]["metrics"]["full"]["sharpe"]
                               >= reals["pstar15"]["metrics"]["full"]["sharpe"] + 0.10)
    else:
        out["proposed"] = False
    (D.LAB / "ROUND15.json").write_text(json.dumps(out, indent=2, default=float))
    print("proposed to the user:", out["proposed"], flush=True)


if __name__ == "__main__":
    main()
