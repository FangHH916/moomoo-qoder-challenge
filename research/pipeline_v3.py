"""v3: principled upgrades, run once (no parameter fishing).

1. Target = 5-day forward open-to-open return (matches the weekly holding period).
2. Target residualized on industry dummies + log market cap + beta (pure alpha).
3. Inverse-volatility weights inside each leg (vs. equal weight).
4. Shorts only in shortable & easy-to-borrow names with market cap >= $1B.
5. Two more price-only factors: residual (idiosyncratic) momentum and MAX (lottery).
Everything else as v2: LASSO, weekly rebalance, 5-day smoothing, 3x buffer,
SPY beta hedge, vol targeting on new positions.

Run: python pipeline_v3.py      (writes reports/backtest_v3/*)
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
from sklearn.linear_model import LassoCV
from sklearn.model_selection import TimeSeriesSplit

import pipeline as v1
import pipeline_v2 as v2
from factors import compute_all, market_beta
from panel import DATA, build_panel

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "backtest_v3"
OUT.mkdir(parents=True, exist_ok=True)
v1.OUT = OUT
BT_START = v1.BT_START
H = 5                                   # holding / target horizon (trading days)
TRAIN_DAYS, RETRAIN_EVERY = 252, 5
MIN_SHORT_MCAP = 1e9
MAX_W_MULT = 3.0                        # inverse-vol weight capped at 3x the equal weight


def extra_factors(close: pd.DataFrame, mkt_ret: pd.Series) -> dict[str, pd.DataFrame]:
    r = close.pct_change()
    m = mkt_ret.reindex(close.index)
    beta = market_beta(close, mkt_ret)
    resid = r - beta.mul(m, axis=0)
    # residual momentum: t-252..t-21 cumulative residual return scaled by its volatility (Blitz et al.)
    cum = resid.rolling(231, min_periods=180).sum().shift(21)
    vol = resid.rolling(231, min_periods=180).std().shift(21)
    return {"resid_mom": cum / (vol + 1e-8), "max21": -r.rolling(21, min_periods=15).max()}


def residual_target(fwd_h: pd.DataFrame, U: pd.DataFrame, ind: pd.Series, mcap: pd.DataFrame,
                    beta: pd.DataFrame) -> pd.DataFrame:
    D = pd.get_dummies(ind).astype(float).reindex(U.columns).fillna(0).to_numpy()
    Y = fwd_h.to_numpy(float); L = np.log(mcap.to_numpy(float)); B = beta.reindex_like(fwd_h).to_numpy(float)
    Um = U.to_numpy()
    out = np.full(Y.shape, np.nan)
    for t in range(len(U.index)):
        m = Um[t] & np.isfinite(Y[t]) & np.isfinite(L[t]) & np.isfinite(B[t])
        if m.sum() < 100:
            continue
        X = np.column_stack([D[m], (L[t, m] - L[t, m].mean()) / (L[t, m].std() + 1e-12), np.clip(B[t, m], -1, 3)])
        y = np.clip(Y[t, m], *np.nanpercentile(Y[t, m], [1, 99]))
        coef, *_ = np.linalg.lstsq(X, y, rcond=None)
        res = y - X @ coef
        out[t, m] = (pd.Series(res).rank(pct=True) - 0.5).to_numpy()
    return pd.DataFrame(out, index=U.index, columns=U.columns)


def lasso_h(nf: dict[str, pd.DataFrame], y: pd.DataFrame, U: pd.DataFrame):
    names = list(nf)
    dates = U.index
    X3 = np.stack([nf[n].to_numpy(np.float32) for n in names], -1)
    Y = y.to_numpy(float)
    Um = U.to_numpy()
    scores = np.full(U.shape, np.nan, dtype=np.float32)
    coefs, coef = {}, None
    bt_idx = np.searchsorted(dates, BT_START)
    for t in range(bt_idx, len(dates)):
        if coef is None or (t - bt_idx) % RETRAIN_EVERY == 0:
            print(f"lasso {t - bt_idx}/{len(dates) - bt_idx}", flush=True)
            hi = t - 1 - H                       # 5-day target at s needs open[s+H+1] <= open[t]
            lo = max(0, hi - TRAIN_DAYS)
            Xs, ys = [], []
            for s in range(lo, hi):
                m = Um[s] & np.isfinite(Y[s]) & np.isfinite(X3[s]).all(1)
                if m.sum() >= 100:
                    Xs.append(X3[s, m]); ys.append(Y[s, m])
            if Xs:
                Xtr = np.vstack(Xs).astype(np.float64); ytr = np.concatenate(ys)
                if len(ytr) > 300_000:
                    sel = np.sort(np.random.default_rng(0).choice(len(ytr), 300_000, replace=False))
                    Xtr, ytr = Xtr[sel], ytr[sel]
                mdl = LassoCV(cv=TimeSeriesSplit(5), alphas=30, max_iter=10000).fit(Xtr, ytr)
                coef = mdl.coef_.copy()
                coefs[dates[t]] = dict(zip(names, coef), alpha=mdl.alpha_)
        if coef is not None:
            m = Um[t] & np.isfinite(X3[t]).all(1)
            scores[t, m] = X3[t, m] @ coef
    return pd.DataFrame(scores, index=dates, columns=U.columns), pd.DataFrame(coefs).T


def make_target_fn(shortable: pd.Series, mcap: pd.DataFrame, vol60: pd.DataFrame, inv_vol: bool):
    def target(score_row: pd.Series, n: int, prev: pd.Series, buffer: float, t) -> pd.Series:
        s = score_row.dropna()
        if len(s) < 4 * n:
            return pd.Series(dtype=float)
        can_short = shortable.reindex(s.index).fillna(False) & (mcap.loc[t].reindex(s.index) >= MIN_SHORT_MCAP)
        rk = s.rank(ascending=False)
        ss = s[can_short]
        rk_s = ss.rank(ascending=True)
        held_l = [c for c in prev[prev > 0].index if c in rk.index and rk[c] <= n * buffer]
        held_s = [c for c in prev[prev < 0].index if c in rk_s.index and rk_s[c] <= n * buffer]
        longs = (held_l + [c for c in rk.sort_values().index if c not in held_l])[:n]
        shorts = (held_s + [c for c in rk_s.sort_values().index if c not in held_s and c not in longs])[:n]
        def leg(names, gross):
            if not names:
                return pd.Series(dtype=float)
            if inv_vol:
                iv = 1 / vol60.loc[t].reindex(names).clip(lower=0.005).fillna(vol60.loc[t].median())
                w = iv / iv.sum() * gross
                w = w.clip(upper=gross / len(names) * MAX_W_MULT); w = w / w.sum() * gross
            else:
                w = pd.Series(gross / len(names), index=names)
            return w
        w = pd.concat([leg(longs, v1.GROSS_LONG), -leg(shorts, v1.GROSS_SHORT)])
        kept = w.index.intersection(prev.index)
        same = kept[np.sign(prev[kept]) == np.sign(w[kept])]
        w[same] = prev[same]
        return w
    return target


def backtest_v3(scores, fwd, beta, spy_fwd, target_fn, n):
    """v2.backtest with a pluggable target function. v2.backtest calls
    v1.target_weights(s.loc[t], ...), and that row's .name is the date t."""
    orig = v1.target_weights
    v1.target_weights = lambda row, n_, prev, buf: target_fn(row, n_, prev, buf, row.name)
    try:
        return v2.backtest(scores, fwd, beta, spy_fwd, n=n)
    finally:
        v1.target_weights = orig


def main() -> None:
    print("building panel...", flush=True)
    P = build_panel()
    p, U = P["p"], P["U"]
    facs = {k: v.astype("float32") for k, v in compute_all(p).items()}
    spy = p["close"]["SPY"].pct_change()
    facs.update({k: v.astype("float32") for k, v in extra_factors(p["close"], spy).items()})
    beta = market_beta(p["close"], spy).astype("float32")
    vol60 = p["close"].pct_change().rolling(60, min_periods=40).std().astype("float32")
    o = p["open"].astype(float)
    fwd5 = o.shift(-(H + 1)) / o.shift(-1) - 1.0
    spy_fwd = p["fwd_ret"]["SPY"].astype(float)
    mcap, fwd, industry = p["mcap"], p["fwd_ret"], P["industry"]
    assets = pd.read_parquet(DATA / "assets.parquet").set_index("symbol")
    shortable = (assets["shortable"] & assets["easy_to_borrow"]).reindex(U.columns).fillna(False)
    print("easy-to-borrow share of universe:", round(float(shortable.mean()), 3), flush=True)
    del P, p, o; gc.collect()
    print("factors:", len(facs), list(facs), flush=True)
    nf = v1.preprocess(facs, U, industry, mcap)
    del facs; gc.collect()
    sf = v1.single_factor_report(nf, fwd, mcap)
    sf.to_csv(OUT / "single_factor.csv")
    print(sf[["IC_mean", "ICIR_ann", "t_NW", "IC_lag10", "IC_small", "IC_large"]].round(4).to_string(), flush=True)

    print("residualized 5-day target...", flush=True)
    y = residual_target(fwd5.where(U), U, industry, mcap, beta)
    scores, coefs = lasso_h(nf, y, U)
    scores.to_parquet(OUT / "scores_v3.parquet"); coefs.to_csv(OUT / "lasso_coefs.csv")
    ic1 = v1.rank_ic(scores.loc[BT_START:], fwd).dropna()
    ic5 = v1.rank_ic(scores.loc[BT_START:], fwd5).dropna()
    comp = {"IC1_mean": ic1.mean(), "IC1_tNW": v1.nw_tstat(ic1), "IC5_mean": ic5.mean(), "IC5_tNW": v1.nw_tstat(ic5, lags=10)}
    print("composite:", comp, flush=True)

    results, daily = [], {}
    for key, inv, n in (("v3_eq_n20", False, 20), ("v3_invvol_n20", True, 20), ("v3_invvol_n30", True, 30)):
        fn = make_target_fn(shortable, mcap, vol60, inv)
        bt = backtest_v3(scores, fwd, beta, spy_fwd, fn, n)
        daily[key] = bt; bt.to_csv(OUT / f"daily_{key}.csv")
        m, g = v1.metrics(bt, "net"), v1.metrics(bt, "gross")
        b = np.cov(bt["net"], bt["spy"])[0, 1] / np.var(bt["spy"])
        results.append({"trial": key, **m, "gross_annual_return": g["annual_return"], "realized_beta": b})
        print(f"{key}: net ann={m['annual_return']:.2%} sharpe={m['sharpe']:.2f} mdd={m['max_drawdown']:.2%} "
              f"vol={m['annual_vol']:.1%} turnover/day={m['avg_daily_turnover']:.2%}", flush=True)
    res = pd.DataFrame(results).set_index("trial")
    prev_sh = np.concatenate([pd.read_csv(ROOT / "reports" / d / "sweep.csv")["sharpe"].to_numpy()
                              for d in ("backtest", "backtest_v2", "backtest_v21")])
    all_sh = np.concatenate([prev_sh, res["sharpe"].to_numpy()])
    best = res["sharpe"].idxmax()
    res["deflated_sharpe_prob"] = np.nan
    res.loc[best, "deflated_sharpe_prob"] = v1.deflated_sharpe(daily[best]["net"], all_sh)
    res.to_csv(OUT / "sweep.csv")
    summary = {"composite": comp, "best_trial": best, "best": res.loc[best].to_dict(), "n_trials_total": int(len(all_sh))}
    (OUT / "metrics.json").write_text(json.dumps(summary, indent=2, default=float))
    print(json.dumps(summary, indent=2, default=float), flush=True)
    fig, ax = plt.subplots(2, 1, figsize=(11, 7), sharex=True)
    for k, d in daily.items():
        (1 + d["net"]).cumprod().plot(ax=ax[0], label=k, lw=1.8 if k == best else 0.9)
    (1 + daily[best]["spy"]).cumprod().plot(ax=ax[0], label="SPY", color="gray", ls="--", lw=0.8)
    ax[0].legend(fontsize=8); ax[0].set_title("v3: 5-day residual target, inverse-vol, borrowable shorts (net)")
    eq = (1 + daily[best]["net"]).cumprod(); (eq / eq.cummax() - 1).plot(ax=ax[1], color="tab:red")
    ax[1].set_title(f"Drawdown ({best})")
    fig.tight_layout(); fig.savefig(OUT / "equity.png", dpi=120)
    print("done", flush=True)


if __name__ == "__main__":
    main()
