"""Round-6 factor families from new information (SEC fundamentals + earnings events),
added to the existing cache (data/cache) without touching the 56 cached factors.

Point-in-time rule: an annual value is usable from fiscal-period end + 100 days, a
quarterly balance-sheet value from period end + 75 days, shares from period end + 60
days; earnings-announcement returns from the close of the day after the 8-K 2.02.

Factors (sign: higher = higher expected return, per the literature):
  bm   book-to-market            (Fama-French value)
  ep   earnings yield            (Basu)
  sp   sales yield               (Barbee et al.)
  gpa  gross profit / assets     (Novy-Marx 2013)
  roe  net income / book equity  (quality)
  ag   -asset growth             (Cooper-Gulen-Schill 2008)
  accr -accruals / assets        (Sloan 1996)
  nsi  -net share issuance 12m   (Pontiff-Woodgate 2008)
  ear  earnings-announcement abnormal return, held 60 days (PEAD; Chan-Jegadeesh-Lakonishok 1996)
usage: python lab_factors2.py
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

import pipeline as v1
from lab_cache import CACHE
from panel import DATA

_meta = json.loads((CACHE / "meta.json").read_text())
DATES = pd.DatetimeIndex(_meta["dates"])
SYMS = pd.Index(_meta["symbols"])


def cik_map() -> pd.Series:
    u = pd.read_parquet(DATA / "universe.parquet")[["symbol", "cik"]].drop_duplicates("symbol")
    return u.set_index("symbol")["cik"].astype(int).reindex(SYMS)


def daily_from_frames(fr: pd.DataFrame, lag_days: int, cmap: pd.Series) -> pd.DataFrame:
    """(cik, end, val) -> daily (DATES x SYMS) step function, value usable from end + lag."""
    fr = fr.dropna(subset=["val"]).copy()
    fr["avail"] = fr["end"] + pd.Timedelta(days=lag_days)
    fr = fr.sort_values("avail").drop_duplicates(["cik", "avail"], keep="last")
    w = fr.pivot(index="avail", columns="cik", values="val")
    w = w.reindex(w.index.union(DATES)).sort_index().ffill(limit=None).reindex(DATES)
    # stale after 15 months -> drop (company stopped filing this concept)
    age = fr.pivot(index="avail", columns="cik", values="avail")
    age = age.reindex(age.index.union(DATES)).sort_index().ffill().reindex(DATES)
    stale = age.apply(lambda col: (DATES.to_series().values - col.values).astype("timedelta64[D]").astype(float) > 460)
    w = w.mask(stale)
    out = pd.DataFrame(np.nan, index=DATES, columns=SYMS)
    for sym, cik in cmap.dropna().items():
        if cik in w.columns:
            out[sym] = w[cik].to_numpy()
    return out


def build() -> dict[str, pd.DataFrame]:
    fr = pd.read_parquet(DATA / "fund_frames.parquet")
    cmap = cik_map()
    mcap = pd.DataFrame(np.load(CACHE / "mcap.npy"), index=DATES, columns=SYMS).astype(float)

    def annual(concept):
        return daily_from_frames(fr[(fr.concept == concept) & (fr.period == "annual")], 100, cmap)

    def instant(concept):
        return daily_from_frames(fr[(fr.concept == concept) & (fr.period == "q_instant")], 75, cmap)

    ni, gp, cfo = annual("NetIncomeLoss"), annual("GrossProfit"), annual("NetCashProvidedByUsedInOperatingActivities")
    rev = annual("Revenues").combine_first(annual("RevenueFromContractWithCustomerExcludingAssessedTax"))
    assets, equity = instant("Assets"), instant("StockholdersEquity")
    pos_eq = equity.where(equity > 0)
    pos_as = assets.where(assets > 0)
    f = {
        "bm": pos_eq / mcap,
        "ep": ni / mcap,
        "sp": rev / mcap,
        "gpa": gp / pos_as,
        "roe": ni / pos_eq,
        "ag": -(pos_as / pos_as.shift(252) - 1),
        "accr": -(ni - cfo) / pos_as,
    }
    # net share issuance from the SEC shares history (same 60-day lag as the panel)
    sh = pd.read_parquet(DATA / "shares_q.parquet")
    shares = daily_from_frames(sh.rename(columns={"period_end": "end", "shares": "val"}), 60, cmap)
    f["nsi"] = -np.log(shares / shares.shift(252))
    # earnings-announcement abnormal return (close d-1 -> close d+1, minus SPY), held 60 trading days
    bars = pd.read_parquet(DATA / "bars_daily.parquet", columns=["symbol", "date", "close"])
    close = bars.pivot(index="date", columns="symbol", values="close").reindex(index=DATES, columns=SYMS)
    spy = close["SPY"]
    ev = pd.read_parquet(DATA / "earnings_dates.parquet")
    ear = pd.DataFrame(np.nan, index=DATES, columns=SYMS)
    pos = {d: i for i, d in enumerate(DATES)}
    by_cik = ev.groupby("cik")["date"].apply(list).to_dict()
    for sym, cik in cmap.dropna().items():
        for d in by_cik.get(int(cik), []):
            i = int(np.searchsorted(DATES, d))           # first trading day on/after the filing
            if i < 1 or i + 1 >= len(DATES):
                continue
            c0, c1 = close.iat[i - 1, close.columns.get_loc(sym)], close.iat[i + 1, close.columns.get_loc(sym)]
            if not (np.isfinite(c0) and np.isfinite(c1)) or c0 <= 0:
                continue
            ab = (c1 / c0 - 1) - (spy.iat[i + 1] / spy.iat[i - 1] - 1)
            ear.iloc[i + 1: i + 61, ear.columns.get_loc(sym)] = ab   # usable from the close of d+1
    f["ear"] = ear
    return {k: v.replace([np.inf, -np.inf], np.nan).astype("float32") for k, v in f.items()}


def family2(mcap: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Round-14 factor family (literature signs, same point-in-time lags): cash-flow yield, operating
    profitability, cash profitability, minus revenue growth. Identical to lab_round14.build_new."""
    fr = pd.read_parquet(DATA / "fund_frames.parquet")
    cmap = cik_map()

    def annual(c):
        return daily_from_frames(fr[(fr.concept == c) & (fr.period == "annual")], 100, cmap)

    def instant(c):
        return daily_from_frames(fr[(fr.concept == c) & (fr.period == "q_instant")], 75, cmap)

    cfo, oi = annual("NetCashProvidedByUsedInOperatingActivities"), annual("OperatingIncomeLoss")
    rev = annual("Revenues").combine_first(annual("RevenueFromContractWithCustomerExcludingAssessedTax"))
    eq, assets = instant("StockholdersEquity"), instant("Assets")
    pos_eq, pos_as = eq.where(eq > 0), assets.where(assets > 0)
    raw = {"cfp": cfo / mcap, "op": oi / pos_eq, "cop": cfo / pos_as, "sg": -(rev / rev.shift(252) - 1)}
    return {k: v.replace([np.inf, -np.inf], np.nan).astype("float32") for k, v in raw.items()}


def main() -> None:
    U = pd.DataFrame(np.load(CACHE / "U.npy"), index=DATES, columns=SYMS)
    ind = pd.Series(_meta["industry"], index=SYMS).replace("NA", np.nan)
    mcap = pd.DataFrame(np.load(CACHE / "mcap.npy"), index=DATES, columns=SYMS)
    raw = build()
    for k, v in raw.items():
        cov = float(v.where(U).notna().sum().sum() / U.sum().sum())
        print(f"{k}: coverage inside universe {cov:.1%}", flush=True)
    nf = v1.preprocess(raw, U, ind, mcap)
    names = json.loads((CACHE / "factors.json").read_text())
    for k, df in nf.items():
        np.save(CACHE / f"f_{k}.npy", df.to_numpy(np.float32))
        if k not in names:
            names.append(k)
    (CACHE / "factors.json").write_text(json.dumps(names))
    print(f"cache now holds {len(names)} factors", flush=True)


if __name__ == "__main__":
    main()
