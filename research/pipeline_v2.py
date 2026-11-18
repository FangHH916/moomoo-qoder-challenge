"""v2 backtest: the five fixes after the v1 run.

1. Turnover control: rebalance every 5 trading days, 5-day score smoothing,
   wide buffer (a held name is kept until it leaves the top/bottom 3n).
2. Beta neutral (SPY overlay sized to offset the book's 60-day beta) and
   volatility targeting (scale new positions toward 10% annual vol).
3. Factor weighting without look-ahead: LASSO vs. trailing-ICIR weighting
   (factor kept only when its trailing 252-day IC t-stat > 1.5).
4. US-documented factors added (rev21, mom12_1, high52, amihud, ivol60).
5. 20-30 names per side so the $0.99/order fee stays small at $100k.

Run: python pipeline_v2.py      (writes reports/backtest_v2/*)
"""
from __future__ import annotations

import gc
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import pipeline as v1
from factors import compute_all, market_beta
from panel import build_panel

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "backtest_v2"
OUT.mkdir(parents=True, exist_ok=True)
v1.OUT = OUT

BT_START = v1.BT_START
REBAL_EVERY, SMOOTH, BUFFER = 5, 5, 3.0
TARGET_VOL, VOL_WIN, VOL_FLOOR = 0.10, 20, 0.3
ICIR_WIN, ICIR_MIN_T = 252, 1.5
STOCK_SLIP_BPS, SPY_SLIP_BPS = 5.0, 1.0
FIN_APR, SHORT_APR = 0.048, 0.06


def icir_scores(nf: dict[str, pd.DataFrame], fwd: pd.DataFrame, U: pd.DataFrame):
    names = list(nf)
    ic = pd.DataFrame({n: v1.rank_ic(nf[n], fwd.where(U)) for n in names})   # daily rank IC per factor
    dates = U.index
    bt_idx = np.searchsorted(dates, BT_START)
    scores = pd.DataFrame(np.nan, index=dates, columns=U.columns, dtype="float32")
    weights = {}
    for t in range(bt_idx, len(dates)):
        win = ic.iloc[max(0, t - 1 - ICIR_WIN): t - 1]          # IC known only through t-2
        mu, sd, k = win.mean(), win.std(), win.count()
        tstat = mu / sd * np.sqrt(k)
        w = (mu / sd).where(tstat.abs() > ICIR_MIN_T, 0.0).fillna(0.0)
        weights[dates[t]] = w.to_dict()
        if (w != 0).any():
            s = sum(nf[n].iloc[t] * w[n] for n in names if w[n] != 0)
            scores.iloc[t] = s.where(U.iloc[t]).to_numpy(np.float32)
        if (t - bt_idx) % 100 == 0:
            print(f"icir {t - bt_idx}/{len(dates) - bt_idx}", flush=True)
    return scores, pd.DataFrame(weights).T


def backtest(scores: pd.DataFrame, fwd: pd.DataFrame, beta: pd.DataFrame, spy_fwd: pd.Series,
             n: int, hedge: bool = True, vol_target: bool = True) -> pd.DataFrame:
    s = scores.rolling(SMOOTH, min_periods=1).mean().where(scores.notna())
    dates = s.loc[BT_START:].index
    prev, h_prev = pd.Series(dtype=float), 0.0
    equity, rows, hist = v1.EQUITY0, [], []
    for i, t in enumerate(dates):
        rebalance = i % REBAL_EVERY == 0
        if rebalance:
            mult = 1.0
            if vol_target and len(hist) >= VOL_WIN:
                rv = np.std(hist[-VOL_WIN:]) * np.sqrt(252)
                mult = float(np.clip(TARGET_VOL / max(rv, 1e-6), VOL_FLOOR, 1.0))
            w = v1.target_weights(s.loc[t], n, prev, BUFFER)
            if w.empty:
                continue
            new = w.index.difference(prev.index)
            w[new] = w[new] * mult
            b = beta.loc[t].reindex(w.index).fillna(1.0).clip(-1, 3)
            h = -float((w * b).sum()) if hedge else 0.0
        else:
            w, h = prev, h_prev
            if w.empty:
                continue
        allidx = w.index.union(prev.index)
        dw = w.reindex(allidx, fill_value=0) - prev.reindex(allidx, fill_value=0)
        traded = float(dw.abs().sum())
        n_orders = int((dw.abs() > 1e-9).sum())
        dh = abs(h - h_prev)
        hedge_trade = dh > 0.005
        if hedge_trade:
            n_orders += 1
        else:
            h = h_prev
            dh = 0.0
        cost = traded * STOCK_SLIP_BPS / 1e4 + dh * SPY_SLIP_BPS / 1e4 + n_orders * v1.ORDER_FEE / equity
        short_gross = -w[w < 0].sum() + max(0.0, -h)
        long_gross = w[w > 0].sum() + max(0.0, h)
        carry = short_gross * SHORT_APR / 252 + max(0.0, long_gross - 1.0) * FIN_APR / 252
        r = fwd.loc[t, w.index].fillna(0.0)
        rs = float(np.nan_to_num(spy_fwd.get(t, 0.0)))
        gross = float((w * r).sum()) + h * rs
        net = gross - cost - carry
        equity *= 1 + net
        hist.append(net)
        rows.append({"date": t, "gross": gross, "net": net, "cost": cost, "borrow": carry,
                     "turnover": (traded + dh) / 2, "n_long": int((w > 0).sum()), "n_short": int((w < 0).sum()),
                     "n_orders": n_orders, "hedge": h, "long_gross": w[w > 0].sum(), "short_gross": -w[w < 0].sum(),
                     "equity": equity, "spy": rs})
        if equity <= 0:
            break
        prev = w * (1 + r) / (1 + gross)
        h_prev = h * (1 + rs) / (1 + gross)
    return pd.DataFrame(rows).set_index("date") if rows else pd.DataFrame()


def main() -> None:
    print("building panel...", flush=True)
    P = build_panel()
    p, U = P["p"], P["U"]
    import os
    if os.environ.get("SMOKE"):
        global OUT
        cols = U.sum(0).sort_values(ascending=False).index[:400].union(pd.Index(["SPY"]))
        p = {k: v[cols] for k, v in p.items()}; U = U[cols]; P["industry"] = P["industry"].reindex(cols)
        OUT = ROOT / "reports" / "smoke_v2"; OUT.mkdir(parents=True, exist_ok=True); v1.OUT = OUT
    print("universe size per day (median):", int(U.sum(1).median()), flush=True)
    facs = {k: v.astype("float32") for k, v in compute_all(p).items()}
    beta = market_beta(p["close"], p["close"]["SPY"].pct_change()).astype("float32")
    spy_fwd = p["fwd_ret"]["SPY"].astype(float)
    mcap, fwd, industry = p["mcap"], p["fwd_ret"], P["industry"]
    del P, p; gc.collect()
    print("factors:", len(facs), list(facs), flush=True)
    nf = v1.preprocess(facs, U, industry, mcap)
    del facs; gc.collect()

    sf = v1.single_factor_report(nf, fwd, mcap)
    sf.to_csv(OUT / "single_factor.csv")
    print(sf[["IC_mean", "ICIR_ann", "t_NW", "IC_lag10", "IC_small", "IC_large"]].round(4).to_string(), flush=True)

    print("LASSO...", flush=True)
    s_lasso, c_lasso = v1.lasso_scores(nf, fwd, U)
    c_lasso.to_csv(OUT / "lasso_coefs.csv")
    print("ICIR weighting...", flush=True)
    s_icir, c_icir = icir_scores(nf, fwd, U)
    c_icir.to_csv(OUT / "icir_weights.csv")
    comp = {}
    for name, s in (("lasso", s_lasso), ("icir", s_icir)):
        ic = v1.rank_ic(s.loc[BT_START:], fwd).dropna()
        comp[name] = {"IC_mean": ic.mean(), "ICIR_ann": ic.mean() / ic.std() * np.sqrt(252), "t_NW": v1.nw_tstat(ic)}
        print(f"composite {name}: {comp[name]}", flush=True)

    results, daily = [], {}
    for method, s in (("lasso", s_lasso), ("icir", s_icir)):
        for n in (20, 30):
            bt = backtest(s, fwd, beta, spy_fwd, n=n)
            key = f"{method}_n{n}"
            if bt.empty:
                print(f"{key}: no trades", flush=True)
                continue
            daily[key] = bt
            bt.to_csv(OUT / f"daily_{key}.csv")
            m, g = v1.metrics(bt, "net"), v1.metrics(bt, "gross")
            beta_real = np.cov(bt["net"], bt["spy"])[0, 1] / np.var(bt["spy"])
            results.append({"trial": key, "method": method, "n_per_side": n, **m,
                            "gross_annual_return": g["annual_return"], "gross_sharpe": g["sharpe"],
                            "realized_beta": beta_real, "avg_hedge": bt["hedge"].mean()})
            print(f"{key}: net ann={m['annual_return']:.2%} sharpe={m['sharpe']:.2f} mdd={m['max_drawdown']:.2%} "
                  f"vol={m['annual_vol']:.1%} turnover/day={m['avg_daily_turnover']:.2%} beta={beta_real:.2f}", flush=True)
    res = pd.DataFrame(results).set_index("trial")
    # deflate against ALL trials run so far (v1's 8 + these)
    prev_sh = pd.read_csv(ROOT / "reports" / "backtest" / "sweep.csv")["sharpe"].to_numpy()
    all_sh = np.concatenate([prev_sh, res["sharpe"].to_numpy()])
    best = res["sharpe"].idxmax()
    res["deflated_sharpe_prob"] = np.nan
    res.loc[best, "deflated_sharpe_prob"] = v1.deflated_sharpe(daily[best]["net"], all_sh)
    res.to_csv(OUT / "sweep.csv")
    summary = {"composites": comp, "best_trial": best, "best": res.loc[best].to_dict(),
               "n_trials_total": int(len(all_sh)),
               "settings": {"rebalance_every": REBAL_EVERY, "smooth": SMOOTH, "buffer": BUFFER,
                            "target_vol": TARGET_VOL, "beta_hedge": "SPY overlay"}}
    (OUT / "metrics.json").write_text(json.dumps(summary, indent=2, default=float))
    print(json.dumps(summary, indent=2, default=float), flush=True)

    fig, ax = plt.subplots(2, 1, figsize=(11, 7), sharex=True)
    for k, d in daily.items():
        (1 + d["net"]).cumprod().plot(ax=ax[0], label=k, lw=1.8 if k == best else 0.9)
    spy_eq = (1 + daily[best]["spy"]).cumprod(); spy_eq.plot(ax=ax[0], label="SPY", color="gray", lw=0.8, ls="--")
    ax[0].legend(fontsize=8); ax[0].set_title("v2: weekly rebalance, beta-neutral, vol-targeted long-short (net of costs)")
    eq = (1 + daily[best]["net"]).cumprod(); (eq / eq.cummax() - 1).plot(ax=ax[1], color="tab:red")
    ax[1].set_title(f"Drawdown ({best}, net)")
    fig.tight_layout(); fig.savefig(OUT / "equity.png", dpi=120)
    print("done", flush=True)


if __name__ == "__main__":
    main()
