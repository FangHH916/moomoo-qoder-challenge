"""Round 17 (ROUND17_PREREG.md): earnings-announcement premium indicator on top of P*15, single trial.
usage: python lab_round17.py
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

import lab_round12 as R12
import r10_deploy as D

YEAR, WINDOW = pd.Timedelta(days=364), pd.Timedelta(days=30)


def build_eap(L) -> float:
    import lab_factors2 as F2
    import pipeline as v1
    ev = pd.read_parquet(D.DATA / "earnings_dates.parquet")
    by_cik = {int(c): np.sort(g["date"].to_numpy(dtype="datetime64[ns]")) for c, g in ev.groupby("cik")}
    cmap = F2.cik_map()
    dates = L.DATES.to_numpy(dtype="datetime64[ns]")
    eap = np.full((len(dates), len(L.SYMS)), np.nan, dtype=np.float32)
    for j, (sym, cik) in enumerate(cmap.items()):
        if pd.isna(cik) or int(cik) not in by_cik:
            continue
        a = by_cik[int(cik)]
        first = np.searchsorted(a, dates - YEAR.to_timedelta64(), side="left")   # earliest announcement >= t - 364d
        ok = (first < len(a))
        nxt = np.where(ok, a[np.minimum(first, len(a) - 1)], np.datetime64("NaT"))
        ok &= nxt <= dates                                                       # it must already have happened
        days = (nxt + YEAR.to_timedelta64()) - dates
        eap[ok, j] = (days[ok] <= WINDOW.to_timedelta64()).astype(np.float32)
    raw = {"eap": pd.DataFrame(eap, index=L.DATES, columns=L.SYMS)}
    U = L.df("U").astype(bool)
    cover = float(raw["eap"].where(U).notna().sum().sum() / U.sum().sum())
    share_on = float(np.nanmean(np.where(U.to_numpy(), eap, np.nan)))
    ind = pd.Series(L._meta["industry"], index=L.SYMS).replace("NA", np.nan)
    for k, df in v1.preprocess(raw, U, ind, L.df("mcap").astype(float)).items():
        np.save(L.CACHE / f"f_{k}.npy", df.to_numpy(np.float32))
    print(f"eap coverage {cover:.3f}, share of stock-days flagged {share_on:.3f}", flush=True)
    return cover


def main() -> None:
    R12._init()
    L, F, P = R12._W["L"], R12._W["F"], R12._W["P"]
    out = {"coverage": build_eap(L)}
    base = D.VARIANTS["pstar15"][0]
    for label, names in (("pstar15", base), ("pstar16", base + ["eap"])):
        p, W, d = R12.portfolio(tuple((k, 1.0) for k in names))
        out[label] = {**D.windows(p, L), "avg_w_d77": float(W["D77"].mean()), "d77_sleeve": D.windows(d, L)}
        print(label, {w: round(out[label][w]["sharpe"], 3) for w in ("selection", "holdout", "full")},
              "| D77 alone", {w: round(out[label]["d77_sleeve"][w]["sharpe"], 3) for w in ("selection", "holdout", "full")},
              flush=True)
    a, b = out["pstar16"], out["pstar15"]
    out["passes_ideal_rule"] = bool(a["selection"]["sharpe"] > b["selection"]["sharpe"]
                                    and a["holdout"]["sharpe"] > b["holdout"]["sharpe"]
                                    and a["full"]["sharpe"] >= b["full"]["sharpe"] + 0.10)
    out["proposed"] = False                     # realistic check only needed if the ideal rule passes
    (D.LAB / "ROUND17.json").write_text(json.dumps(out, indent=2, default=float))
    print("passes ideal rule:", out["passes_ideal_rule"], flush=True)


if __name__ == "__main__":
    main()
