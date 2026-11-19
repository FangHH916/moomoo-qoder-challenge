"""Round 9 (ROUND9_PREREG.md): ETF time-series momentum sleeve T + broad short book D,
combined by trailing-volatility risk parity, scaled to the 90% margin cap.
usage: python lab_combo.py
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

import lab_run as L

ETFS = ["SPY", "QQQ", "IWM", "TLT", "IEF", "GLD", "SLV", "USO", "UUP"]
REBAL, SLIP_BPS, FEE, EQ0 = 5, 2.0, 0.99, 100_000.0
CAP_TICKER, MARGIN_CAP = 0.38, 0.90


def sleeve_T() -> pd.DataFrame:
    bars = pd.read_parquet(L.CACHE.parent / "bars_daily.parquet", columns=["symbol", "date", "close"])
    close = bars[bars.symbol.isin(ETFS)].pivot(index="date", columns="symbol", values="close").reindex(L.DATES)[ETFS]
    r = close.pct_change()
    sig = (close / close.shift(252) - 1) > 0
    vol = r.rolling(60, min_periods=40).std() * np.sqrt(252)
    fwd = L.df("fwd1")[ETFS].astype(float)
    dates = L.DATES[L.DATES >= L.BT_START]
    prev = pd.Series(0.0, index=ETFS)
    equity, rows = EQ0, []
    for i, t in enumerate(dates):
        if i % REBAL == 0:
            w = (sig.loc[t].astype(float) * (0.10 / vol.loc[t]) / len(ETFS)).fillna(0.0).clip(upper=CAP_TICKER)
            if w.sum() > 1.0:
                w = w / w.sum()
        else:
            w = prev
        dw = (w - prev).abs()
        n_orders = int((dw > 1e-6).sum())
        cost = dw.sum() * SLIP_BPS / 1e4 + n_orders * FEE / equity
        fin = max(0.0, w.sum() - 1.0) * 0.048 / 252
        rr = fwd.loc[t].fillna(0.0)
        gross = float((w * rr).sum())
        net = gross - cost - fin
        equity *= 1 + net
        rows.append({"date": t, "net": net, "long_gross": w.sum(), "short_gross": 0.0, "n_orders": n_orders})
        prev = w * (1 + rr) / (1 + gross)
    return pd.DataFrame(rows).set_index("date")


def sleeve_D() -> pd.DataFrame:
    d = pd.read_csv(L.LAB / "daily_R8_shortspy_n95_deployable.csv", index_col=0, parse_dates=True)
    d["long_total"] = d["long_gross"] + d["hedge"].clip(lower=0)
    d["short_total"] = d["short_gross"] + (-d["hedge"]).clip(lower=0)
    return d


def stats(r: pd.Series) -> dict:
    r = r.dropna()
    eq = (1 + r).cumprod()
    yrs = len(r) / 252
    return {"sharpe": r.mean() / r.std() * np.sqrt(252), "ann": eq.iloc[-1] ** (1 / yrs) - 1,
            "mdd": (eq / eq.cummax() - 1).min(), "vol": r.std() * np.sqrt(252)}


def main() -> None:
    T = sleeve_T()
    D = sleeve_D()
    idx = T.index.intersection(D.index)
    T, D = T.loc[idx], D.loc[idx]
    rT, rD = T["net"], D["net"]
    # weekly risk parity on trailing 126-day vol. The net return stamped at day s is realised at the
    # open of s+2, so at the decision time (close of t) only returns up to s = t-2 are known: shift(2).
    vT = rT.rolling(126, min_periods=60).std().shift(2)
    vD = rD.rolling(126, min_periods=60).std().shift(2)
    wT, wD = (1 / vT), (1 / vD)
    s = wT + wD
    wT, wD = (wT / s).fillna(0.5), (wD / s).fillna(0.5)
    reb = pd.Series(np.arange(len(idx)) % REBAL == 0, index=idx)
    wT, wD = wT.where(reb).ffill(), wD.where(reb).ffill()
    rP = wT * rT + wD * rD
    margin = wT * (0.5 * T["long_gross"]) + wD * (0.5 * D["long_total"] + 0.6 * D["short_total"])
    k = MARGIN_CAP / margin.max()
    rP = rP * k
    # stand-alone sleeves scaled to the same cap for comparability
    kT = MARGIN_CAP / (0.5 * T["long_gross"]).max()
    kD = MARGIN_CAP / (0.5 * D["long_total"] + 0.6 * D["short_total"]).max()
    series = {"T_trend": rT * min(1, kT), "D_shortbook": rD * min(1, kD), "P_combined": rP}
    out = {}
    for name, r in series.items():
        out[name] = {lab: stats(r.loc[lo:hi]) for lab, lo, hi in
                     (("selection", L.BT_START, L.VAL_END), ("holdout", L.HOLD_START, None), ("full", L.BT_START, None))}
    out["corr_T_D_full"] = float(rT.corr(rD))
    out["avg_weight_T"] = float(wT.mean())
    out["scale_P"] = float(k)
    (L.LAB / "ROUND9.json").write_text(json.dumps(out, indent=2, default=float))
    rows = []
    for name in series:
        for lab in ("selection", "holdout", "full"):
            m = out[name][lab]
            rows.append({"strategy": name, "window": lab, "sharpe": m["sharpe"], "ann": m["ann"], "mdd": m["mdd"], "vol": m["vol"]})
    print(pd.DataFrame(rows).round(3).to_string(index=False))
    print(f"corr(T, D) = {out['corr_T_D_full']:.3f}; avg weight on T = {out['avg_weight_T']:.2f}; P scale = {k:.3f}")
    pd.DataFrame({"T": rT, "D": rD, "P": rP, "wT": wT}).to_csv(L.LAB / "daily_R9_combo.csv")


if __name__ == "__main__":
    main()
