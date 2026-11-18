"""v2.1: (a) which factor set works — user's 16 vs. US classics vs. all 21;
(b) tighter risk: full-book vol targeting + drawdown de-risking;
(c) leg attribution (long / short / SPY hedge). LASSO, 20 names per side,
weekly rebalance, 5-day smoothing, 3x buffer, SPY beta hedge (as v2).

Run: python pipeline_v21.py     (writes reports/backtest_v21/*)
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
import pipeline_v2 as v2
from factors import compute_all, market_beta
from panel import build_panel

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "backtest_v21"
OUT.mkdir(parents=True, exist_ok=True)
v1.OUT = OUT
BT_START = v1.BT_START
N = 20
CLASSIC = ["mom12_1", "high52", "ivol60", "rev21", "amihud"]
DD_HALF, DD_HALT, COOLDOWN = -0.06, -0.08, 10
GROSS = 0.70


def backtest_v21(scores, fwd, beta, spy_fwd, n=N):
    """v2 mechanics + full-book vol scaling + drawdown de-risking + leg attribution."""
    s = scores.rolling(v2.SMOOTH, min_periods=1).mean().where(scores.notna())
    dates = s.loc[BT_START:].index
    prev, h_prev = pd.Series(dtype=float), 0.0
    equity, peak, rows, hist = v1.EQUITY0, v1.EQUITY0, [], []
    cool, half_mode = 0, False
    for i, t in enumerate(dates):
        dd = equity / peak - 1
        flatten = False
        if dd <= DD_HALT and cool == 0 and not prev.empty:
            flatten, cool = True, COOLDOWN          # kill switch: flat, wait, resume at half size
        rebalance = (i % v2.REBAL_EVERY == 0) or flatten
        if cool > 0 and not flatten:
            cool -= 1
            if cool == 0:
                half_mode, peak = True, equity      # resume at half size, reset the high-water mark
        if rebalance:
            if flatten or cool > 0:
                w, h = pd.Series(dtype=float), 0.0
            else:
                mult = 1.0
                if len(hist) >= v2.VOL_WIN:
                    rv = np.std(hist[-v2.VOL_WIN:]) * np.sqrt(252)
                    mult = float(np.clip(v2.TARGET_VOL / max(rv, 1e-6), v2.VOL_FLOOR, 1.0))
                if dd <= DD_HALF or half_mode:
                    mult *= 0.5
                if half_mode and dd > -0.02:
                    half_mode = False
                w = v1.target_weights(s.loc[t], n, prev, v2.BUFFER)
                if w.empty:
                    continue
                # full-book scaling: rescale both legs to GROSS*mult if the book drifted >10% from target
                for side in (w > 0, w < 0):
                    g = w[side].abs().sum()
                    tgt = GROSS * mult
                    if g > 0 and abs(g / tgt - 1) > 0.10:
                        w[side] = w[side] * (tgt / g)
                b = beta.loc[t].reindex(w.index).fillna(1.0).clip(-1, 3)
                h = -float((w * b).sum())
        else:
            w, h = prev, h_prev
        allidx = w.index.union(prev.index)
        dw = w.reindex(allidx, fill_value=0) - prev.reindex(allidx, fill_value=0)
        traded = float(dw.abs().sum())
        n_orders = int((dw.abs() > 1e-9).sum())
        dh = abs(h - h_prev)
        if dh > 0.005 or (flatten and h_prev != 0):
            n_orders += 1
        else:
            h, dh = h_prev, 0.0
        cost = traded * v2.STOCK_SLIP_BPS / 1e4 + dh * v2.SPY_SLIP_BPS / 1e4 + n_orders * v1.ORDER_FEE / equity
        short_gross = -w[w < 0].sum() + max(0.0, -h)
        long_gross = w[w > 0].sum() + max(0.0, h)
        carry = short_gross * v2.SHORT_APR / 252 + max(0.0, long_gross - 1.0) * v2.FIN_APR / 252
        r = fwd.loc[t, w.index].fillna(0.0) if len(w) else pd.Series(dtype=float)
        rs = float(np.nan_to_num(spy_fwd.get(t, 0.0)))
        long_c = float((w[w > 0] * r[w > 0]).sum()) if len(w) else 0.0
        short_c = float((w[w < 0] * r[w < 0]).sum()) if len(w) else 0.0
        gross = long_c + short_c + h * rs
        net = gross - cost - carry
        equity *= 1 + net
        peak = max(peak, equity)
        hist.append(net)
        rows.append({"date": t, "gross": gross, "net": net, "cost": cost, "borrow": carry,
                     "turnover": (traded + dh) / 2, "n_long": int((w > 0).sum()), "n_short": int((w < 0).sum()),
                     "n_orders": n_orders, "hedge": h, "long_c": long_c, "short_c": short_c, "hedge_c": h * rs,
                     "equity": equity, "spy": rs, "dd": equity / peak - 1, "flat": int(cool > 0)})
        prev = (w * (1 + r) / (1 + gross)) if len(w) else pd.Series(dtype=float)
        h_prev = h * (1 + rs) / (1 + gross)
    return pd.DataFrame(rows).set_index("date")


def attribution(bt: pd.DataFrame) -> dict:
    return {k: bt[c].mean() * 252 for k, c in (("long_leg_ann", "long_c"), ("short_leg_ann", "short_c"),
                                                ("hedge_ann", "hedge_c"), ("cost_ann", "cost"), ("carry_ann", "borrow"))
            if c in bt}


def main() -> None:
    print("building panel...", flush=True)
    P = build_panel()
    p, U = P["p"], P["U"]
    facs = {k: v.astype("float32") for k, v in compute_all(p).items()}
    beta = market_beta(p["close"], p["close"]["SPY"].pct_change()).astype("float32")
    spy_fwd = p["fwd_ret"]["SPY"].astype(float)
    mcap, fwd, industry = p["mcap"], p["fwd_ret"], P["industry"]
    del P, p; gc.collect()
    nf_all = v1.preprocess(facs, U, industry, mcap)
    del facs; gc.collect()
    sets = {"user16": [k for k in nf_all if k not in CLASSIC], "classic5": CLASSIC, "all21": list(nf_all)}

    results, daily, comp = [], {}, {}
    for sname, cols in sets.items():
        print(f"LASSO on {sname} ({len(cols)} factors)...", flush=True)
        sc, coefs = v1.lasso_scores({k: nf_all[k] for k in cols}, fwd, U)
        sc.to_parquet(OUT / f"scores_{sname}.parquet"); coefs.to_csv(OUT / f"lasso_coefs_{sname}.csv")
        ic = v1.rank_ic(sc.loc[BT_START:], fwd).dropna()
        comp[sname] = {"IC_mean": ic.mean(), "ICIR_ann": ic.mean() / ic.std() * np.sqrt(252), "t_NW": v1.nw_tstat(ic)}
        print(f"composite {sname}: {comp[sname]}", flush=True)
        for risk, fn in (("v2risk", lambda: v2.backtest(sc, fwd, beta, spy_fwd, n=N)),
                         ("v21risk", lambda: backtest_v21(sc, fwd, beta, spy_fwd))):
            bt = fn()
            key = f"{sname}_{risk}"
            daily[key] = bt
            bt.to_csv(OUT / f"daily_{key}.csv")
            m, g = v1.metrics(bt, "net"), v1.metrics(bt, "gross")
            b = np.cov(bt["net"], bt["spy"])[0, 1] / np.var(bt["spy"])
            row = {"trial": key, "factors": sname, "risk": risk, **m, "gross_annual_return": g["annual_return"],
                   "realized_beta": b, **attribution(bt)}
            results.append(row)
            print(f"{key}: net ann={m['annual_return']:.2%} sharpe={m['sharpe']:.2f} mdd={m['max_drawdown']:.2%} "
                  f"vol={m['annual_vol']:.1%} turnover/day={m['avg_daily_turnover']:.2%}", flush=True)
    res = pd.DataFrame(results).set_index("trial")
    prev_sh = np.concatenate([pd.read_csv(ROOT / "reports" / d / "sweep.csv")["sharpe"].to_numpy()
                              for d in ("backtest", "backtest_v2")])
    all_sh = np.concatenate([prev_sh, res["sharpe"].to_numpy()])
    best = res["sharpe"].idxmax()
    res["deflated_sharpe_prob"] = np.nan
    res.loc[best, "deflated_sharpe_prob"] = v1.deflated_sharpe(daily[best]["net"], all_sh)
    res.to_csv(OUT / "sweep.csv")
    summary = {"composites": comp, "best_trial": best, "best": res.loc[best].to_dict(), "n_trials_total": int(len(all_sh))}
    (OUT / "metrics.json").write_text(json.dumps(summary, indent=2, default=float))
    print(json.dumps(summary, indent=2, default=float), flush=True)

    fig, ax = plt.subplots(2, 1, figsize=(11, 7), sharex=True)
    for k, d in daily.items():
        (1 + d["net"]).cumprod().plot(ax=ax[0], label=k, lw=1.8 if k == best else 0.8)
    ax[0].legend(fontsize=7, ncol=2); ax[0].set_title("v2.1: factor sets x risk overlay (LASSO, 20/side, net)")
    eq = (1 + daily[best]["net"]).cumprod(); (eq / eq.cummax() - 1).plot(ax=ax[1], color="tab:red")
    ax[1].set_title(f"Drawdown ({best})")
    fig.tight_layout(); fig.savefig(OUT / "equity.png", dpi=120)
    print("done", flush=True)


if __name__ == "__main__":
    main()
