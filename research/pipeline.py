"""Factor preprocessing, single-factor tests, LASSO combination and long-short backtest.

Run:  python pipeline.py            (writes reports/backtest/*)
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.linear_model import LassoCV
from sklearn.model_selection import TimeSeriesSplit

from factors import FACTOR_NAMES, compute_all
from panel import DATA, build_panel

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "backtest"
OUT.mkdir(parents=True, exist_ok=True)

BT_START = pd.Timestamp("2021-11-18")
TRAIN_DAYS, RETRAIN_EVERY = 252, 5
TOP_Q = 0.10                       # long top 10%, short bottom 10%
GROSS_LONG, GROSS_SHORT = 0.70, 0.70
NAME_CAP = 0.02
SLIPPAGE_BPS = 5.0                 # per side, on traded notional
ORDER_FEE, EQUITY0 = 0.99, 100_000.0
SHORT_BORROW_APR = 0.06


# ------------------------------------------------------------------ preprocessing
def preprocess(facs: dict[str, pd.DataFrame], U: pd.DataFrame, ind: pd.Series,
               mcap: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Per date, inside the universe: MAD winsorize (5x) -> z-score ->
    OLS-neutralize on industry dummies + log market cap -> z-score residual."""
    names = list(facs)
    dates = U.index
    dummies = pd.get_dummies(ind).astype(float)
    out = {n: np.full(U.shape, np.nan, dtype=np.float32) for n in names}
    stack = np.stack([facs[n].reindex(index=dates, columns=U.columns).to_numpy(np.float32) for n in names], -1)
    lmc = np.log(mcap.reindex(index=dates, columns=U.columns).to_numpy(float))
    D = dummies.reindex(U.columns).fillna(0).to_numpy()
    Um = U.to_numpy()
    for t in range(len(dates)):
        if t % 100 == 0:
            print(f"preprocess {t}/{len(dates)}", flush=True)
        m = Um[t] & np.isfinite(lmc[t])
        if m.sum() < 100:
            continue
        Y = stack[t, m, :].copy()
        med = np.nanmedian(Y, 0)
        mad = np.nanmedian(np.abs(Y - med), 0) * 1.4826 + 1e-12
        Y = np.clip(Y, med - 5 * mad, med + 5 * mad)
        Y = (Y - np.nanmean(Y, 0)) / (np.nanstd(Y, 0) + 1e-12)
        Y = np.nan_to_num(Y, nan=0.0)                     # missing -> neutral after standardization
        X = np.column_stack([D[m], (lmc[t, m] - lmc[t, m].mean()) / (lmc[t, m].std() + 1e-12)])
        beta, *_ = np.linalg.lstsq(X, Y, rcond=None)
        R = Y - X @ beta
        R = (R - R.mean(0)) / (R.std(0) + 1e-12)
        for j, n in enumerate(names):
            out[n][t, m] = R[:, j]
    return {n: pd.DataFrame(v, index=dates, columns=U.columns) for n, v in out.items()}


# ------------------------------------------------------------------ single factor tests
def rank_ic(f: pd.DataFrame, fwd: pd.DataFrame) -> pd.Series:
    return f.rank(1).corrwith(fwd.where(f.notna()).rank(1), axis=1)


def nw_tstat(x: pd.Series, lags: int = 5) -> float:
    """Newey-West (Bartlett kernel) t-stat of the mean of a series."""
    x = x.dropna().to_numpy()
    n = len(x)
    e = x - x.mean()
    s = e @ e / n
    for L in range(1, lags + 1):
        s += 2 * (1 - L / (lags + 1)) * (e[L:] @ e[:-L]) / n
    return x.mean() / np.sqrt(s / n)


def single_factor_report(nf: dict[str, pd.DataFrame], fwd: pd.DataFrame,
                         mcap: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for n, f in nf.items():
        f = f.loc[BT_START:]
        r = fwd.reindex_like(f)
        ic = rank_ic(f, r).dropna()
        q = f.rank(1, pct=True)
        q1 = r.where(q <= 0.2).mean(1)
        q5 = r.where(q > 0.8).mean(1)
        ls = (q5 - q1).dropna()
        auto = f.rank(1).corrwith(f.shift(1).rank(1), axis=1).mean()
        row = {
            "factor": n, "IC_mean": ic.mean(), "IC_std": ic.std(),
            "ICIR_ann": ic.mean() / ic.std() * np.sqrt(252), "t_NW": nw_tstat(ic),
            "IC_pos_pct": (ic > 0).mean(), "Q5_ann": q5.mean() * 252, "Q1_ann": q1.mean() * 252,
            "LS_ann_ret": ls.mean() * 252, "LS_sharpe": ls.mean() / ls.std() * np.sqrt(252),
            "rank_autocorr": auto,
        }
        # IC decay: factor at t vs. fwd return k-1 days later (k=1 is the tradable next day)
        for k in (2, 3, 5, 10):
            row[f"IC_lag{k}"] = rank_ic(f, r.shift(-(k - 1))).mean()
        # IC by size tercile (does it only work in small caps?)
        sz = mcap.reindex_like(f).where(f.notna()).rank(1, pct=True)
        for name, lo, hi in (("small", 0, 1 / 3), ("mid", 1 / 3, 2 / 3), ("large", 2 / 3, 1.01)):
            msk = (sz > lo) & (sz <= hi)
            row[f"IC_{name}"] = rank_ic(f.where(msk), r.where(msk)).mean()
        rows.append(row)
    return pd.DataFrame(rows).set_index("factor").sort_values("ICIR_ann", ascending=False)


# ------------------------------------------------------------------ LASSO combination
def lasso_scores(nf: dict[str, pd.DataFrame], fwd: pd.DataFrame, U: pd.DataFrame):
    names = list(nf)
    dates = U.index
    X3 = np.stack([nf[n].to_numpy(np.float32) for n in names], -1)   # (T, N, K)
    # target: cross-sectional rank of fwd return, centred to [-0.5, 0.5]
    y = fwd.where(U).rank(1, pct=True).sub(0.5).to_numpy(float)
    Um = U.to_numpy()
    scores = np.full(U.shape, np.nan)
    coefs = {}
    bt_idx = np.searchsorted(dates, BT_START)
    coef = None
    for t in range(bt_idx, len(dates)):
        if coef is None or (t - bt_idx) % RETRAIN_EVERY == 0:
            print(f"lasso {t - bt_idx}/{len(dates) - bt_idx}", flush=True)
            lo, hi = max(0, t - 1 - TRAIN_DAYS), t - 1          # fwd known only up to t-2
            Xs, ys = [], []
            for s in range(lo, hi):
                m = Um[s] & np.isfinite(y[s]) & np.isfinite(X3[s]).all(1)
                if m.sum() < 100:
                    continue
                Xs.append(X3[s, m]); ys.append(y[s, m])
            if not Xs:
                continue
            Xtr, ytr = np.vstack(Xs).astype(np.float64), np.concatenate(ys).astype(np.float64)
            if len(ytr) > 300_000:                              # subsample for speed, keep time order
                sel = np.sort(np.random.default_rng(0).choice(len(ytr), 300_000, replace=False))
                Xtr, ytr = Xtr[sel], ytr[sel]
            model = LassoCV(cv=TimeSeriesSplit(5), alphas=30, max_iter=10000, fit_intercept=True)
            model.fit(Xtr, ytr)
            coef = model.coef_.copy()
            coefs[dates[t]] = dict(zip(names, coef), alpha=model.alpha_)
        m = Um[t] & np.isfinite(X3[t]).all(1)
        scores[t, m] = X3[t, m] @ coef
    return pd.DataFrame(scores, index=dates, columns=U.columns), pd.DataFrame(coefs).T


# ------------------------------------------------------------------ portfolio + backtest
def target_weights(score_row: pd.Series, n: int, prev: pd.Series, buffer: float) -> pd.Series:
    """Top-n long / bottom-n short. With buffer>1, a held name is kept while its
    rank stays within the top (bottom) n*buffer (turnover control).
    Names that stay in the book keep their drifted weight (no trade); only
    entries get the equal target weight and only entries/exits are orders."""
    s = score_row.dropna()
    if len(s) < 4 * n:
        return pd.Series(dtype=float)
    rk = s.rank(ascending=False)            # 1 = best
    rk_short = s.rank(ascending=True)       # 1 = worst
    held_l = [c for c in prev[prev > 0].index if c in rk.index and rk[c] <= n * buffer]
    held_s = [c for c in prev[prev < 0].index if c in rk_short.index and rk_short[c] <= n * buffer]
    longs = held_l + [c for c in rk.sort_values().index if c not in held_l][: max(0, n - len(held_l))]
    shorts = held_s + [c for c in rk_short.sort_values().index if c not in held_s and c not in longs][: max(0, n - len(held_s))]
    longs, shorts = longs[:n], shorts[:n]
    wl, ws = min(GROSS_LONG / n, NAME_CAP_FOR(n)), min(GROSS_SHORT / n, NAME_CAP_FOR(n))
    w = pd.concat([pd.Series(wl, longs), pd.Series(-ws, shorts)])
    kept = w.index.intersection(prev.index)
    same_side = kept[np.sign(prev[kept]) == np.sign(w[kept])]
    w[same_side] = prev[same_side]                    # hold, don't rebalance drift
    return w


def NAME_CAP_FOR(n: int) -> float:
    # cap only binds for very concentrated books; competition cap is 40% per symbol
    return max(NAME_CAP, GROSS_LONG / n)


def backtest(scores: pd.DataFrame, fwd: pd.DataFrame, n: int = 40, buffer: float = 1.0) -> pd.DataFrame:
    dates = scores.loc[BT_START:].index
    prev = pd.Series(dtype=float)
    equity, rows = EQUITY0, []
    for t in dates:
        w = target_weights(scores.loc[t], n, prev, buffer)
        if w.empty:
            continue
        allidx = w.index.union(prev.index)
        dw = w.reindex(allidx, fill_value=0) - prev.reindex(allidx, fill_value=0)
        traded = dw.abs().sum()
        n_orders = int((dw.abs() > 1e-9).sum())
        cost = traded * SLIPPAGE_BPS / 1e4 + n_orders * ORDER_FEE / equity
        borrow = -w[w < 0].sum() * SHORT_BORROW_APR / 252
        r = fwd.loc[t, w.index].fillna(0.0)
        gross = float((w * r).sum())
        net = gross - cost - borrow
        equity *= 1 + net
        if equity <= 0:                               # account wiped out: stop the simulation
            rows.append({"date": t, "gross": gross, "net": -1.0, "cost": cost, "borrow": borrow,
                         "turnover": traded / 2, "n_long": 0, "n_short": 0, "n_orders": n_orders, "equity": 0.0})
            break
        rows.append({"date": t, "gross": gross, "net": net, "cost": cost, "borrow": borrow,
                     "turnover": traded / 2, "n_long": int((w > 0).sum()), "n_short": int((w < 0).sum()),
                     "n_orders": n_orders, "equity": equity})
        # drift weights with returns (held names are not rebalanced)
        prev = w * (1 + r) / (1 + gross)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).set_index("date")


def metrics(bt: pd.DataFrame, col: str = "net") -> dict:
    r = bt[col].dropna()
    eq = (1 + r).cumprod()
    yrs = len(r) / 252
    ann = eq.iloc[-1] ** (1 / yrs) - 1
    vol = r.std() * np.sqrt(252)
    down = r[r < 0].std() * np.sqrt(252)
    dd = eq / eq.cummax() - 1
    monthly = (1 + r).groupby(r.index.to_period("M")).prod() - 1
    return {
        "start": str(r.index[0].date()), "end": str(r.index[-1].date()), "days": len(r),
        "total_return": eq.iloc[-1] - 1, "annual_return": ann, "monthly_return_mean": monthly.mean(),
        "monthly_return_median": monthly.median(), "monthly_win_rate": (monthly > 0).mean(),
        "annual_vol": vol, "sharpe": r.mean() / r.std() * np.sqrt(252), "sortino": r.mean() * 252 / down,
        "max_drawdown": dd.min(), "calmar": ann / abs(dd.min()), "daily_win_rate": (r > 0).mean(),
        "avg_daily_turnover": bt["turnover"].mean(), "annual_turnover": bt["turnover"].mean() * 252,
        "avg_daily_cost_bps": bt["cost"].mean() * 1e4, "avg_orders_per_day": bt["n_orders"].mean(),
        "avg_names_long": bt["n_long"].mean(), "avg_names_short": bt["n_short"].mean(),
    }


def main() -> None:
    print("building panel...", flush=True)
    P = build_panel()
    p, U = P["p"], P["U"]
    import os
    if os.environ.get("SMOKE"):                       # quick end-to-end check on a subset
        cols = U.sum(0).sort_values(ascending=False).index[:300]
        p = {k: v[cols] for k, v in p.items()}; U = U[cols]; P["industry"] = P["industry"].reindex(cols)
        global OUT
        OUT = OUT.parent / "smoke"; OUT.mkdir(parents=True, exist_ok=True)
    print("universe size per day (median):", int(U.sum(1).median()), flush=True)
    print("computing factors...", flush=True)
    print("eligible names kept:", U.shape[1], flush=True)
    facs = {k: v.astype("float32") for k, v in compute_all(p).items()}
    mcap, fwd, industry = p["mcap"], p["fwd_ret"], P["industry"]
    del P, p
    import gc; gc.collect()
    print("preprocessing...", flush=True)
    nf = preprocess(facs, U, industry, mcap)
    del facs; gc.collect()

    sf = single_factor_report(nf, fwd, mcap)
    sf.to_csv(OUT / "single_factor.csv")
    print(sf.round(4).to_string(), flush=True)

    print("LASSO...", flush=True)
    scores, coefs = lasso_scores(nf, fwd, U)
    coefs.to_csv(OUT / "lasso_coefs.csv")
    ic = rank_ic(scores.loc[BT_START:], fwd).dropna()

    scores.to_parquet(OUT / "scores.parquet")
    print(f"combined score: IC_mean={ic.mean():.4f} ICIR_ann={ic.mean() / ic.std() * np.sqrt(252):.2f}", flush=True)

    # parameter sweep: names per side x turnover buffer (all trials recorded for the DSR)
    results, daily = [], {}
    for n in (20, 40, 80, 150):
        for buf in (1.0, 2.0):
            bt = backtest(scores, fwd, n=n, buffer=buf)
            key = f"n{n}_buf{buf:g}"
            if bt.empty:
                print(f"{key}: skipped (universe too small)", flush=True)
                continue
            daily[key] = bt
            bt.to_csv(OUT / f"daily_{key}.csv")
            m = metrics(bt, "net"); g = metrics(bt, "gross")
            results.append({"trial": key, "n_per_side": n, "buffer": buf, **m,
                            "gross_annual_return": g["annual_return"], "gross_sharpe": g["sharpe"]})
            print(f"{key}: net ann={m['annual_return']:.2%} sharpe={m['sharpe']:.2f} "
                  f"mdd={m['max_drawdown']:.2%} turnover/day={m['avg_daily_turnover']:.2%}", flush=True)
    res = pd.DataFrame(results).set_index("trial")
    best = res["sharpe"].idxmax()
    res["deflated_sharpe_prob"] = np.nan
    res.loc[best, "deflated_sharpe_prob"] = deflated_sharpe(daily[best]["net"], res["sharpe"].to_numpy())
    res.to_csv(OUT / "sweep.csv")
    summary = {"combined_IC_mean": ic.mean(), "combined_ICIR_ann": ic.mean() / ic.std() * np.sqrt(252),
               "best_trial": best, "best": res.loc[best].to_dict(), "n_trials": len(res)}
    (OUT / "metrics.json").write_text(json.dumps(summary, indent=2, default=float))
    print(json.dumps(summary, indent=2, default=float))

    bt = daily[best]
    fig, ax = plt.subplots(2, 1, figsize=(11, 7), sharex=True)
    for k, d in daily.items():
        (1 + d["net"]).cumprod().plot(ax=ax[0], label=k, lw=1.6 if k == best else 0.7)
    ax[0].legend(fontsize=7, ncol=4); ax[0].set_title("LASSO multi-factor long-short (70/70), open-to-open, net of costs")
    eq = (1 + bt["net"]).cumprod(); (eq / eq.cummax() - 1).plot(ax=ax[1], color="tab:red"); ax[1].set_title(f"Drawdown ({best}, net)")
    fig.tight_layout(); fig.savefig(OUT / "equity.png", dpi=120)


def deflated_sharpe(r: pd.Series, trial_sharpes: np.ndarray) -> float:
    """Bailey & Lopez de Prado (2014) deflated Sharpe ratio: probability that the
    best trial's true Sharpe > the expected max Sharpe of N unskilled trials."""
    from scipy.stats import norm, skew, kurtosis
    r = r.dropna()
    T = len(r)
    sr = r.mean() / r.std()                                   # per-period Sharpe
    srs = np.asarray(trial_sharpes) / np.sqrt(252)
    N = len(srs)
    emc = 0.5772156649
    sr0 = np.sqrt(np.var(srs, ddof=1)) * ((1 - emc) * norm.ppf(1 - 1 / N) + emc * norm.ppf(1 - 1 / (N * np.e)))
    g3, g4 = skew(r), kurtosis(r, fisher=False)
    return float(norm.cdf((sr - sr0) * np.sqrt(T - 1) / np.sqrt(1 - g3 * sr + (g4 - 1) / 4 * sr ** 2)))


if __name__ == "__main__":
    main()
