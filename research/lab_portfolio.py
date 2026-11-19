"""Round 10 (ROUND10_PREREG.md): systematic portfolio-level Sharpe maximisation.
usage: python lab_portfolio.py
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from sklearn.covariance import LedoitWolf

import lab_fix as F
import lab_run as L

DATA = L.CACHE.parent
ETF9 = ["SPY", "QQQ", "IWM", "TLT", "IEF", "GLD", "SLV", "USO", "UUP"]
EXTRA = ["EFA", "EEM", "EWJ", "FXI", "HYG", "LQD", "TIP", "AGG", "DBC", "DBA", "UNG", "VNQ"]
ETF21 = ETF9 + EXTRA
RP4 = ["SPY", "TLT", "GLD", "IEF"]
REB, WIN, MINH, MARGIN_CAP = 5, 252, 126, 0.90


# ------------------------------------------------------------------ data
def etf_panel():
    a = pd.read_parquet(DATA / "bars_daily.parquet", columns=["symbol", "date", "open", "close"])
    b = pd.read_parquet(DATA / "bars_extra_etfs.parquet", columns=["symbol", "date", "open", "close"])
    x = pd.concat([a[a.symbol.isin(ETF9)], b])
    close = x.pivot(index="date", columns="symbol", values="close").reindex(L.DATES)[ETF21]
    opn = x.pivot(index="date", columns="symbol", values="open").reindex(L.DATES)[ETF21]
    fwd = opn.shift(-2) / opn.shift(-1) - 1          # signal at close t, trade at open t+1
    return close, fwd


# ------------------------------------------------------------------ sleeves
def etf_sleeve(weight_fn, cols, close, fwd, slip_bps=2.0):
    dates = L.DATES[L.DATES >= L.BT_START]
    prev = pd.Series(0.0, index=cols)
    eq, rows, wh = 1e5, [], {}
    for i, t in enumerate(dates):
        if i % REB == 0:
            w = weight_fn(t).reindex(cols).fillna(0.0).clip(lower=0, upper=0.38)
            if w.sum() > 1.0:
                w = w / w.sum()
        else:
            w = prev
        dw = (w - prev).abs()
        n = int((dw > 1e-6).sum())
        cost = dw.sum() * slip_bps / 1e4 + n * 0.99 / eq
        fin = max(0.0, w.sum() - 1.0) * 0.048 / 252
        r = fwd.loc[t, cols].fillna(0.0)
        g = float((w * r).sum())
        net = g - cost - fin
        eq *= 1 + net
        rows.append({"date": t, "net": net, "long": float(w.sum()), "short": 0.0})
        wh[t] = w
        prev = w * (1 + r) / (1 + g)
    return pd.DataFrame(rows).set_index("date"), pd.DataFrame(wh).T


def build_sleeves():
    close, fwd = etf_panel()
    ret = close.pct_change()
    vol = ret.rolling(60, min_periods=40).std() * np.sqrt(252)
    sig252 = (close / close.shift(252) - 1) > 0
    trend3 = sum(((close / close.shift(k) - 1) > 0).astype(float) for k in (21, 63, 252)) / 3

    sleeves, weights = {}, {}
    sleeves["T9"], weights["T9"] = etf_sleeve(
        lambda t: sig252.loc[t, ETF9].astype(float) * (0.10 / vol.loc[t, ETF9]) / len(ETF9), ETF9, close, fwd)
    sleeves["T21"], weights["T21"] = etf_sleeve(
        lambda t: trend3.loc[t, ETF21] * (0.10 / vol.loc[t, ETF21]) / len(ETF21), ETF21, close, fwd)
    sleeves["RP"], weights["RP"] = etf_sleeve(
        lambda t: (1 / vol.loc[t, RP4]) / (1 / vol.loc[t, RP4]).sum(), RP4, close, fwd)
    print("ETF sleeves built", flush=True)
    f = L.LAB / "daily_R10_D77.csv"
    if f.exists():                                 # reuse the round-10 D77 run (identical inputs)
        d = pd.read_csv(f, index_col=0, parse_dates=True)
    else:
        d = F.run_guarded(pd.read_parquet(L.LAB / "scores_R7.parquet"), "shortspy", 77)
        d.to_csv(f)
    sleeves["D77"] = pd.DataFrame({"net": d["net"], "long": d["long_gross"] + d["hedge"].clip(lower=0),
                                   "short": d["short_gross"] + (-d["hedge"]).clip(lower=0)})
    weights["D77_spy_hedge"] = d["hedge"]
    print("D77 built", flush=True)
    return sleeves, weights


# ------------------------------------------------------------------ combination methods
def w_iv(h):
    v = 1 / h.std().replace(0, np.nan)
    return (v / v.sum()).fillna(1 / h.shape[1]).to_numpy()


def _solve(obj, n, cap=1.0):
    cons = ({"type": "eq", "fun": lambda w: w.sum() - 1},)
    res = minimize(obj, np.full(n, 1 / n), bounds=[(0, cap)] * n, constraints=cons, method="SLSQP")
    return res.x if res.success else np.full(n, 1 / n)


def w_mv(h):
    S = LedoitWolf().fit(h.to_numpy()).covariance_
    return _solve(lambda w: w @ S @ w * 1e4, h.shape[1])


def w_erc(h):
    S = LedoitWolf().fit(h.to_numpy()).covariance_
    n = h.shape[1]

    def obj(w):
        rc = w * (S @ w)
        return float(((rc - rc.mean()) ** 2).sum()) * 1e8
    return _solve(obj, n)


def w_ms(h):
    S = LedoitWolf().fit(h.to_numpy()).covariance_
    mu = h.mean().to_numpy()
    mu = 0.5 * mu + 0.5 * mu.mean()
    if (mu <= 0).all():
        return w_iv(h)
    return _solve(lambda w: -(w @ mu) / np.sqrt(w @ S @ w + 1e-18), h.shape[1], cap=0.6)


METHODS = {"IV": w_iv, "MV": w_mv, "ERC": w_erc, "MS": w_ms}


def combine(R: pd.DataFrame, margin_unit: pd.DataFrame, method: str, overlay: bool, long_unit: pd.DataFrame = None):
    n = R.shape[1]
    W = pd.DataFrame(np.nan, index=R.index, columns=R.columns)
    for i in range(0, len(R), REB):
        hist = R.iloc[max(0, i - 1 - WIN): max(0, i - 1)]       # rows up to i-2 are realised at decision time
        W.iloc[i] = METHODS[method](hist) if len(hist) >= MINH else np.full(n, 1 / n)
    W = W.ffill()
    port = (W * R).sum(axis=1)
    if overlay:
        lv = port.rolling(252, min_periods=126).std().shift(2)
        sv = port.rolling(21, min_periods=15).std().shift(2)
        mult = (lv / sv).clip(0.5, 1.5).fillna(1.0)
        reb = pd.Series(np.arange(len(R)) % REB == 0, index=R.index)
        mult = mult.where(reb).ffill()
        port, W = port * mult, W.mul(mult, axis=0)
    margin = (W * margin_unit).sum(axis=1)
    k = MARGIN_CAP / margin.max()
    port, W = port * k, W * k
    if long_unit is not None:
        # portfolio-level financing: 4.8% p.a. on combined long exposure above 100% of equity
        long_total = (W * long_unit).sum(axis=1)
        port = port - (long_total - 1.0).clip(lower=0) * 0.048 / 252
    return port, W


def stats(r: pd.Series) -> dict:
    r = r.dropna()
    eq = (1 + r).cumprod()
    return {"sharpe": r.mean() / r.std() * np.sqrt(252), "ann": eq.iloc[-1] ** (252 / len(r)) - 1,
            "mdd": (eq / eq.cummax() - 1).min(), "vol": r.std() * np.sqrt(252)}


def main() -> None:
    sleeves, weights = build_sleeves()
    idx = sleeves["T9"].index
    for s in sleeves.values():
        idx = idx.intersection(s.index)
    sets = {"S1": ["T9", "D77"], "S2": ["T21", "D77"], "S3": ["T21", "D77", "RP"], "S4": ["T21", "RP"]}
    rows, series = [], {}
    for sname, members in sets.items():
        R = pd.DataFrame({m: sleeves[m].loc[idx, "net"] for m in members})
        MU = pd.DataFrame({m: 0.5 * sleeves[m].loc[idx, "long"] + 0.6 * sleeves[m].loc[idx, "short"] for m in members})
        LU = pd.DataFrame({m: sleeves[m].loc[idx, "long"] for m in members})
        for method in METHODS:
            for ov in (False, True):
                key = f"{sname}_{method}{'_V' if ov else ''}"
                p, W = combine(R, MU, method, ov, LU)
                series[key] = (p, W)
                sel, hol, full = (stats(p.loc[L.BT_START:L.VAL_END]), stats(p.loc[L.HOLD_START:]), stats(p))
                yearly = p.groupby(p.index.year).apply(lambda x: (1 + x).prod() - 1)
                rows.append({"combo": key, "sleeves": "+".join(members), "sel_sharpe": sel["sharpe"],
                             "hold_sharpe": hol["sharpe"], "FULL_sharpe": full["sharpe"], "FULL_ann": full["ann"],
                             "FULL_mdd": full["mdd"], "FULL_vol": full["vol"],
                             "worst_year": yearly.loc[2022:].min()})
    t = pd.DataFrame(rows).sort_values("FULL_sharpe", ascending=False)
    t.to_csv(L.LAB / "round10_combos.csv", index=False)
    print(t.round(3).to_string(index=False), flush=True)

    best = t.iloc[0]["combo"]
    p, W = series[best]
    sh_round = t["FULL_sharpe"].to_numpy()
    prev = pd.read_csv(L.LAB / "registry_val.csv")["sharpe"].dropna().to_numpy()
    dsr_round = L.v1.deflated_sharpe(p, sh_round)
    dsr_all = L.v1.deflated_sharpe(p, np.concatenate([prev, sh_round]))
    # sleeve-level exposures of the pick, for the competition caps
    spy = sum(W[m] * weights[m]["SPY"].reindex(W.index).fillna(0) for m in W.columns if m in ("T9", "T21", "RP")
              and "SPY" in weights[m].columns)
    if "D77" in W.columns:
        spy = spy + W["D77"] * weights["D77_spy_hedge"].reindex(W.index).fillna(0)
    out = {"pick": best, "full": stats(p), "selection": stats(p.loc[L.BT_START:L.VAL_END]),
           "holdout": stats(p.loc[L.HOLD_START:]), "deflated_sharpe_round10": dsr_round,
           "deflated_sharpe_all_trials": dsr_all, "n_trials_round10": int(len(sh_round)),
           "avg_sleeve_weights": W.mean().to_dict(), "max_spy_equivalent_weight": float(np.max(spy)),
           "scale_needed_for_38pct_spy_cap": float(min(1.0, 0.38 / np.max(spy)))}
    (L.LAB / "ROUND10.json").write_text(json.dumps(out, indent=2, default=float))
    print(json.dumps(out, indent=2, default=float), flush=True)
    yearly = p.groupby(p.index.year).apply(lambda x: pd.Series({"ret": (1 + x).prod() - 1,
                                                                 "sharpe": x.mean() / x.std() * np.sqrt(252)}))
    print(yearly.unstack().round(3).to_string(), flush=True)
    pd.DataFrame({"P": p, **{f"w_{c}": W[c] for c in W.columns}}).to_csv(L.LAB / "daily_R10_pick.csv")


if __name__ == "__main__":
    main()
