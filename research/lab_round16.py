"""Round 16 (ROUND16_PREREG.md): classic price-based anomalies on top of P*15, single trial (cached factors).
usage: python lab_round16.py
"""
from __future__ import annotations

import json

import pandas as pd

import lab_round12 as R12
import r10_deploy as D

NEW = ["rev21", "high52", "max21", "bab", "ind_mom6"]


def main() -> None:
    R12._init()
    L, F, P = R12._W["L"], R12._W["F"], R12._W["P"]
    base_names = D.VARIANTS["pstar15"][0]
    out = {}
    for label, names in (("pstar15", base_names), ("pstar20", base_names + NEW)):
        p, W, d = R12.portfolio(tuple((k, 1.0) for k in names))
        out[label] = {**D.windows(p, L), "avg_w_d77": float(W["D77"].mean()), "d77_sleeve": D.windows(d, L)}
        print(label, {w: round(out[label][w]["sharpe"], 3) for w in ("selection", "holdout", "full")},
              "| D77 alone", {w: round(out[label]["d77_sleeve"][w]["sharpe"], 3) for w in ("selection", "holdout", "full")},
              flush=True)
    a, b = out["pstar20"], out["pstar15"]
    out["passes_ideal_rule"] = bool(a["selection"]["sharpe"] > b["selection"]["sharpe"]
                                    and a["holdout"]["sharpe"] > b["holdout"]["sharpe"]
                                    and a["full"]["sharpe"] >= b["full"]["sharpe"] + 0.10)
    out["proposed"] = False
    if out["passes_ideal_rule"]:
        names = base_names + NEW
        sc = R12.composite(tuple((k, 1.0) for k in names))
        d77, _, _ = D.d77_record(L, F, sc)
        t9, _ = D.t9_record(P)
        idx = t9.index.intersection(d77.index)
        dl = d77.loc[idx, "long_gross"] + d77.loc[idx, "hedge"].clip(lower=0)
        ds = d77.loc[idx, "short_gross"] + (-d77.loc[idx, "hedge"]).clip(lower=0)
        R = pd.DataFrame({"T9": t9.loc[idx, "net"], "D77": d77.loc[idx, "net"]})
        MU = pd.DataFrame({"T9": 0.5 * t9.loc[idx, "long"] + 0.6 * t9.loc[idx, "short"], "D77": 0.5 * dl + 0.6 * ds})
        D.VARIANTS["pstar20"] = (names, 0.9 / float((D.mv_raw(P, R) * MU).sum(axis=1).max()), "R16")
        reals = {}
        for v in ("pstar15", "pstar20"):
            sim, extra = D.simulate(D.run_pstar(L, F, P, variant=v), L)
            reals[v] = D._sim_report(sim, extra, L)
            print(v, "realistic", {w: round(x["sharpe"], 3) for w, x in reals[v]["metrics"].items()}, flush=True)
        out["realistic"] = reals
        out["proposed"] = bool(reals["pstar20"]["metrics"]["full"]["sharpe"]
                               >= reals["pstar15"]["metrics"]["full"]["sharpe"] + 0.10)
    (D.LAB / "ROUND16.json").write_text(json.dumps(out, indent=2, default=float))
    print("proposed to the user:", out["proposed"], flush=True)


if __name__ == "__main__":
    main()
