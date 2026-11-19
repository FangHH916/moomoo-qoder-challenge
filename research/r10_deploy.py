"""Deployment reference for the round-10 pick ("P*"): T9 ETF trend + D77 broad short book, weekly
Ledoit-Wolf minimum-variance sleeve weights, frozen scale K_SCALE.

Single source of truth for the deployed strategy. Live code runs this file (or imports its functions)
instead of re-implementing anything, so live and backtest cannot drift apart.

Variants (VARIANTS; the deployed one is DEPLOYED, every command takes --variant):
  pstar    round 10: D77 composite of the 11 literature factors, K = 1.4634   files R10_*, r10_targets_*
  pstar15  round 14: the same + cfp op cop sg (15 factors),     K = 1.4886   files R14_*, r14_targets_*

usage (from research/, project venv):
  python r10_deploy.py reference
      re-run P* on the research cache while recording every ticker weight; assert that it reproduces
      reports/lab/daily_R10_pick.csv; write reports/lab/R10_reference_targets.csv (last 30 complete
      sessions) and reports/lab/R10_DEPLOY.json
  python r10_deploy.py realistic
      $100k account: whole shares at the next open, $0.99 per order charged once at portfolio level,
      5/2 bp slippage, 6% borrow, 4.8% financing, competition caps with DIA/QQQ routing
      -> reports/lab/R10_REALISTIC.json, reports/lab/daily_R10_realistic.csv
  python r10_deploy.py live [--equity 100000] [--hold SYMBOL=DOLLARS ...] [--skip-refresh] [--skip-events]
      refresh bars (adjusted + raw) and 8-K earnings dates -> lean cache in data/cache_live -> recompute
      P* through the latest completed session -> reports/live/r10_targets_<session>.json
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
LAB = ROOT / "reports" / "lab"
LIVE_OUT = ROOT / "reports" / "live"
LIVE_CACHE = DATA / "cache_live"
SNAPSHOT = DATA / "research_snapshot"

ETF9 = ["SPY", "QQQ", "IWM", "TLT", "IEF", "GLD", "SLV", "USO", "UUP"]
ROUTE = ["DIA", "QQQ"]                  # SPY exposure above the per-ticker cap moves here, in this order
ETFS = set(ETF9) | set(ROUTE)
PRIOR_SET = ["bm", "ep", "sp", "mom12_1", "ivol60", "roe", "gpa", "ag", "accr", "nsi", "ear"]
NEW15 = ["cfp", "op", "cop", "sg"]      # round 14: cash-flow yield, operating / cash profitability, -sales growth
N_SHORT = 77
K_SCALE = 1.4633955775862155            # round 10: uniform scale so that the 5-year peak margin is 90%
# variant -> (D77 composite factors, frozen scale, file tag). The deployed variant is chosen by the user.
VARIANTS = {"pstar": (PRIOR_SET, K_SCALE, "R10"),
            "pstar15": (PRIOR_SET + NEW15, 1.488567942168242, "R14")}
DEPLOYED = "pstar15"                     # user confirmed 2026-11-19 (round 14)
INITIAL_EQUITY = 100_000.0
TICKER_CAP = 0.38                       # of initial assets (competition limit: 40%)
MARGIN_CAP, MARGIN_LONG, MARGIN_SHORT = 0.90, 0.5, 0.6
MAX_POSITIONS, MAX_ORDERS = 100, 200
FEE, STOCK_SLIP, ETF_SLIP = 0.99, 5e-4, 2e-4
BORROW_APR, FIN_APR = 0.06, 0.048
REBAL_ANCHOR, REBAL_EVERY = pd.Timestamp("2021-11-18"), 5


# ------------------------------------------------------------------ research modules
def research(cache_dir: Path | None = None):
    """Import the research modules; cache_dir (e.g. data/cache_live) must be set before the first import.
    Research mode (no cache_dir) reads ETF bars from the round-10 snapshot so results stay reproducible after
    the live job has refreshed data/bars_daily.parquet."""
    if cache_dir is not None:
        os.environ["QR_CACHE_DIR"] = str(cache_dir)
    import lab_fix as F
    import lab_portfolio as P
    import lab_run as L
    assert F.PRIOR_SET == PRIOR_SET
    if cache_dir is None and (SNAPSHOT / "bars_daily.parquet").exists():
        if not (SNAPSHOT / "bars_extra_etfs.parquet").exists():
            shutil.copy2(DATA / "bars_extra_etfs.parquet", SNAPSHOT / "bars_extra_etfs.parquet")
        P.DATA = SNAPSHOT
    return L, F, P


# ------------------------------------------------------------------ sleeves (identical to the research runs)
def d77_record(L, F, scores: pd.DataFrame, n: int = N_SHORT):
    """pipeline_v2.backtest for the guarded short-alpha book (lab_fix.run_guarded(scores, 'shortspy', n)),
    copied line for line, plus the weights held each day. Returns (daily, book{date: (weights, spy_hedge)},
    guard{rebalance date: True if the signal guard kept the previous book})."""
    v1, v2 = L.v1, L.v2
    v2.REBAL_EVERY, v2.SMOOTH = REBAL_EVERY, 5
    sh = pd.Series(np.load(L.CACHE / "shortable.npy"), index=L.SYMS)
    fn = F.guarded(L.make_target_fn_short(sh, L.df("mcap"), L.df("vol60")))
    spy_fwd = pd.Series(np.load(L.CACHE / "spy_fwd.npy"), index=L.DATES)
    fwd, beta = L.df("fwd1"), L.df("beta")
    s = scores.rolling(v2.SMOOTH, min_periods=1).mean().where(scores.notna())
    dates = s.loc[v2.BT_START:].index
    prev, h_prev = pd.Series(dtype=float), 0.0
    equity, rows, hist, book, guard = v1.EQUITY0, [], [], {}, {}
    for i, t in enumerate(dates):
        if i % v2.REBAL_EVERY == 0:
            row = s.loc[t].dropna()
            guard[t] = bool(row.nunique() < 50 or float(row.std()) < 1e-12)
            mult = 1.0
            if len(hist) >= v2.VOL_WIN:
                rv = np.std(hist[-v2.VOL_WIN:]) * np.sqrt(252)
                mult = float(np.clip(v2.TARGET_VOL / max(rv, 1e-6), v2.VOL_FLOOR, 1.0))
            w = fn(s.loc[t], n, prev, v2.BUFFER, t)
            if w.empty:
                continue
            new = w.index.difference(prev.index)
            w[new] = w[new] * mult
            b = beta.loc[t].reindex(w.index).fillna(1.0).clip(-1, 3)
            h = -float((w * b).sum())
        else:
            w, h = prev, h_prev
            if w.empty:
                continue
        allidx = w.index.union(prev.index)
        dw = w.reindex(allidx, fill_value=0) - prev.reindex(allidx, fill_value=0)
        traded = float(dw.abs().sum())
        n_orders = int((dw.abs() > 1e-9).sum())
        dh = abs(h - h_prev)
        if dh > 0.005:
            n_orders += 1
        else:
            h, dh = h_prev, 0.0
        cost = traded * v2.STOCK_SLIP_BPS / 1e4 + dh * v2.SPY_SLIP_BPS / 1e4 + n_orders * v1.ORDER_FEE / equity
        short_gross = -w[w < 0].sum() + max(0.0, -h)
        long_gross = w[w > 0].sum() + max(0.0, h)
        carry = short_gross * v2.SHORT_APR / 252 + max(0.0, long_gross - 1.0) * v2.FIN_APR / 252
        r = fwd.loc[t, w.index].fillna(0.0)
        rs = float(np.nan_to_num(spy_fwd.get(t, 0.0)))
        gross = float((w * r).sum()) + h * rs
        net = gross - cost - carry
        equity *= 1 + net
        hist.append(net)
        rows.append({"date": t, "net": net, "hedge": h, "long_gross": w[w > 0].sum(),
                     "short_gross": -w[w < 0].sum(), "n_orders": n_orders})
        book[t] = (w.astype(float).copy(), float(h))
        if equity <= 0:
            break
        prev = w * (1 + r) / (1 + gross)
        h_prev = h * (1 + rs) / (1 + gross)
    return pd.DataFrame(rows).set_index("date"), book, guard


def t9_record(P):
    """Round-9/10 ETF trend sleeve (lab_portfolio.build_sleeves, 'T9'): daily returns and weights held."""
    close, fwd = P.etf_panel()
    vol = close.pct_change().rolling(60, min_periods=40).std() * np.sqrt(252)
    sig = (close / close.shift(252) - 1) > 0
    return P.etf_sleeve(lambda t: sig.loc[t, ETF9].astype(float) * (0.10 / vol.loc[t, ETF9]) / len(ETF9),
                        ETF9, close, fwd)


def mv_raw(P, R: pd.DataFrame) -> pd.DataFrame:
    """lab_portfolio.combine(method='MV') before scaling: weights sum to 1, re-estimated every 5 sessions
    on the trailing 252 sleeve returns realised by decision time (rows up to i-2)."""
    n = R.shape[1]
    W = pd.DataFrame(np.nan, index=R.index, columns=R.columns)
    for i in range(0, len(R), P.REB):
        hist = R.iloc[max(0, i - 1 - P.WIN): max(0, i - 1)]
        W.iloc[i] = P.w_mv(hist) if len(hist) >= P.MINH else np.full(n, 1 / n)
    return W.ffill()


def run_pstar(L, F, P, scores: pd.DataFrame | None = None, variant: str = "pstar") -> dict:
    names, k_scale, _ = VARIANTS[variant]
    if scores is None:
        scores = F.prior_scores(names)
    d77, book, guard = d77_record(L, F, scores)
    t9, t9w = t9_record(P)
    idx = t9.index.intersection(d77.index)
    R = pd.DataFrame({"T9": t9.loc[idx, "net"], "D77": d77.loc[idx, "net"]})
    LU = pd.DataFrame({"T9": t9.loc[idx, "long"],
                       "D77": d77.loc[idx, "long_gross"] + d77.loc[idx, "hedge"].clip(lower=0)})
    W = mv_raw(P, R) * k_scale
    port = (W * R).sum(axis=1) - ((W * LU).sum(axis=1) - 1.0).clip(lower=0) * FIN_APR / 252
    return {"W": W, "R": R, "port": port, "t9w": t9w, "book": book, "guard": guard, "d77": d77,
            "scores": scores, "dates": L.DATES}


def rebalance_sessions(dates: pd.DatetimeIndex) -> pd.DatetimeIndex:
    return dates[dates >= REBAL_ANCHOR][::REBAL_EVERY]


# ------------------------------------------------------------------ targets
def ticker_weights(res: dict, t) -> pd.DataFrame:
    """Strategy weights (fractions of equity) held from the open after session t, by ticker and sleeve."""
    wT, wD = float(res["W"].at[t, "T9"]), float(res["W"].at[t, "D77"])
    rows = [(k, "T9", float(v) * wT) for k, v in res["t9w"].loc[t].reindex(ETF9).fillna(0.0).items() if abs(v) > 1e-12]
    w, h = res["book"][t]
    rows += [(k, "D77", float(v) * wD) for k, v in w.items() if abs(v) > 1e-12]
    rows.append(("SPY", "D77_hedge", h * wD))
    return pd.DataFrame(rows, columns=["symbol", "sleeve", "weight"])


def _route_spy(x: pd.Series, cap: float, beta_to_spy: dict) -> tuple[pd.Series, dict, float]:
    x = x.copy()
    routed, excess = {}, max(0.0, float(x.get("SPY", 0.0)) - cap)
    if excess > 0:
        x["SPY"] = cap
        for r in ROUTE:
            if excess <= 1e-12:
                break
            b = float(np.clip(np.nan_to_num(beta_to_spy.get(r, 1.0), nan=1.0), 0.5, 1.5))
            room = cap - float(x.get(r, 0.0))
            if room <= 0:
                continue
            add = min(room, excess / b)
            x[r] = float(x.get(r, 0.0)) + add
            routed[r] = add
            excess -= add * b
    return x, routed, max(0.0, excess)


def apply_caps(w: pd.Series, equity: float, beta_to_spy: dict, hold: dict | None = None,
               initial: float = INITIAL_EQUITY) -> tuple[pd.Series, dict]:
    """Competition rules on top of the strategy weights (fractions of current equity):
    1. every ticker <= 38% of initial assets: SPY above the cap moves to DIA, then QQQ (sized by their 60-day
       beta to SPY, clipped to [0.5, 1.5]); any other ticker above the cap is clipped;
    2. margin 0.5 x long + 0.6 x short (strategy + discretionary holdings) <= 90% of equity: the strategy book
       is scaled down uniformly; SPY exposure that routing cannot absorb is removed the same way;
    3. positions (strategy + discretionary) <= 100 (reported; the strategy alone holds at most 77 + 10).
    hold: positions outside the strategy, {symbol: signed dollars}; they count toward margin and positions."""
    hold = {k: float(v) for k, v in (hold or {}).items() if abs(float(v)) > 0}
    cap = TICKER_CAP * initial / equity
    base = w.astype(float).groupby(level=0).sum()
    h_long = sum(v for v in hold.values() if v > 0) / equity
    h_short = -sum(v for v in hold.values() if v < 0) / equity
    scale = 1.0
    for _ in range(500):
        x, routed, left = _route_spy(base * scale, cap, beta_to_spy)
        over = [k for k in x.index if k != "SPY" and abs(x[k]) > cap]
        x[over] = np.sign(x[over]) * cap
        long, short = float(x[x > 0].sum()), float(-x[x < 0].sum())
        margin = MARGIN_LONG * (long + h_long) + MARGIN_SHORT * (short + h_short)
        if left <= 1e-9 and margin <= MARGIN_CAP + 1e-9:
            break
        scale *= 0.995
    x = x[x.abs() > 1e-12].sort_values()
    positions = len(x) + len([k for k in hold if k not in x.index])
    diag = {"equity": equity, "ticker_cap_weight": cap, "scale": scale, "routed": routed,
            "clipped": over, "spy_weight_uncapped": float(base.get("SPY", 0.0)),
            "long_gross": long, "short_gross": short, "hold_long": h_long, "hold_short": h_short,
            "margin": margin, "positions": positions, "n_short": int((x < 0).sum()),
            "ok": bool(positions <= MAX_POSITIONS and margin <= MARGIN_CAP + 1e-9 and left <= 1e-9)}
    return x, diag


# ------------------------------------------------------------------ realistic $100k simulation
TRADE_BAND = 0.0025      # execution rule: re-size an existing position only if it is off target by > 0.25% of equity
TRIM_AT, MARGIN_ALERT = 0.39, 0.95   # daily risk rule: ticker > 39% of initial -> back to 38%; margin > 95% -> 90%


def simulate(res: dict, L, initial: float = INITIAL_EQUITY, band: float = TRADE_BAND,
             daily_checks: bool = True) -> tuple[pd.DataFrame, dict]:
    W, book = res["W"], res["book"]
    dates = L.DATES
    pos_of = {d: i for i, d in enumerate(dates)}
    rebal = set(rebalance_sessions(dates))
    names = sorted(set().union(*[set(w.index) for w, _ in book.values()]) | ETFS)
    col = {k: j for j, k in enumerate(names)}
    is_etf = np.array([k in ETFS for k in names])
    Fw = np.nan_to_num(L.df("fwd1").reindex(columns=names).to_numpy(float))
    beta = L.df("beta")[ROUTE]
    raw = pd.read_parquet(DATA / "bars_daily_raw.parquet", columns=["symbol", "date", "open"])
    O = (raw[raw.symbol.isin(names)].pivot(index="date", columns="symbol", values="open")
         .reindex(index=dates, columns=names).to_numpy(float))
    del raw
    eq, val, rows, first = initial, np.zeros(len(names)), [], True
    stats_reb, n_trim, n_delever = [], 0, 0
    slip = np.where(is_etf, ETF_SLIP, STOCK_SLIP)
    for t in W.index:
        i = pos_of[t]
        cost, n_orders = 0.0, 0
        if i + 1 < len(dates):
            px = O[i + 1]                                   # orders fill at the next open (raw price)
            ok = np.isfinite(px) & (px > 0)
            safe = np.where(ok, px, 1.0)
            cur_sh = np.where(ok, np.round(val / safe), 0.0)
            tgt_sh = cur_sh.copy()
            if t in rebal or first:
                first = False
                tw = ticker_weights(res, t).groupby("symbol")["weight"].sum()
                cw, diag = apply_caps(tw, eq, beta.loc[t].to_dict(), initial=initial)
                want = np.zeros(len(names))
                for k, wk in cw.items():
                    j = col[k]
                    if ok[j]:
                        want[j] = np.round(wk * eq / px[j])
                entry_exit = (cur_sh == 0) | (want == 0)
                big = np.abs(want * safe - val) > band * eq
                tgt_sh = np.where(ok & (want != cur_sh) & (entry_exit | big), want, cur_sh)
                d77_target = [k for k in cw.index if k not in ETFS]
                stats_reb.append({"date": t, "short_target": len(d77_target),
                                  "short_held": int(sum(want[col[k]] != 0 for k in d77_target)),
                                  "scale": diag["scale"], "routed": sum(diag["routed"].values())})
            elif daily_checks:                              # between rebalances: only the risk rules trade
                v = cur_sh * safe
                over = ok & (np.abs(v) > TRIM_AT * initial)
                if over.any():
                    tgt_sh = np.where(over, np.sign(cur_sh) * np.floor(TICKER_CAP * initial / safe), tgt_sh)
                    n_trim += int(over.sum())
                m = (MARGIN_LONG * v[v > 0].sum() - MARGIN_SHORT * v[v < 0].sum()) / eq
                if m > MARGIN_ALERT:
                    tgt_sh = np.round(tgt_sh * MARGIN_CAP / m)
                    n_delever += 1
            change = ok & (tgt_sh != cur_sh)
            if change.any():
                new_val = np.where(change, tgt_sh * safe, val)
                n_orders = int(change.sum())
                cost = FEE * n_orders + float((np.abs(new_val - val) * slip).sum())
                val = new_val
        long, short = float(val[val > 0].sum()), float(-val[val < 0].sum())
        carry = short * BORROW_APR / 252 + max(0.0, long - eq) * FIN_APR / 252
        pnl = float((val * Fw[i]).sum())
        new_eq = eq + pnl - cost - carry
        rows.append({"date": t, "net": (new_eq - eq) / eq, "equity": new_eq, "orders": n_orders, "fees": FEE * n_orders,
                     "margin": (MARGIN_LONG * long + MARGIN_SHORT * short) / eq, "long": long / eq, "short": short / eq,
                     "spy_vs_initial": val[col["SPY"]] / initial, "max_ticker_vs_initial": np.abs(val).max() / initial,
                     "positions": int((val != 0).sum())})
        val = val * (1 + Fw[i])
        eq = new_eq
        if eq <= 0:
            break
    return pd.DataFrame(rows).set_index("date"), {"rebalances": pd.DataFrame(stats_reb), "trims": n_trim,
                                                  "delevers": n_delever}


def metrics(r: pd.Series) -> dict:
    r = r.dropna()
    eq = (1 + r).cumprod()
    return {"sharpe": float(r.mean() / r.std() * np.sqrt(252)), "ann": float(eq.iloc[-1] ** (252 / len(r)) - 1),
            "mdd": float((eq / eq.cummax() - 1).min()), "vol": float(r.std() * np.sqrt(252))}


def windows(r: pd.Series, L) -> dict:
    return {"selection": metrics(r.loc[L.BT_START:L.VAL_END]), "holdout": metrics(r.loc[L.HOLD_START:]),
            "full": metrics(r)}


# ------------------------------------------------------------------ commands: reference / realistic
def cmd_reference(variant: str = DEPLOYED) -> None:
    L, F, P = research()
    names, k_scale, tag = VARIANTS[variant]
    scores = F.prior_scores(names)
    res = run_pstar(L, F, P, scores, variant)
    chk = {}
    if variant == "pstar":
        saved = pd.read_parquet(LAB / "scores_R7.parquet")
        chk["scores_vs_saved"] = float(np.nanmax(np.abs(scores.to_numpy(float) - saved.to_numpy(float))))
        d77 = pd.read_csv(LAB / "daily_R10_D77.csv", index_col=0, parse_dates=True)
        chk["d77_net"] = float((res["d77"]["net"] - d77["net"]).abs().max())
    pick_file = LAB / f"daily_{tag}_pick.csv"
    if not pick_file.exists():                  # first run of a new variant: this run becomes its research record
        pd.DataFrame({"P": res["port"], "w_T9": res["W"]["T9"], "w_D77": res["W"]["D77"]}).to_csv(pick_file)
    pick = pd.read_csv(pick_file, index_col=0, parse_dates=True)
    chk["weights"] = float(max((res["W"]["T9"] - pick["w_T9"]).abs().max(), (res["W"]["D77"] - pick["w_D77"]).abs().max()))
    chk["portfolio"] = float((res["port"] - pick["P"]).abs().max())
    print(f"[{variant}] reproduction check (max abs diff):", chk, flush=True)
    assert max(chk.values()) < 1e-6, chk
    # weights only change on the shared rebalance sessions
    changed = res["W"].index[res["W"].diff().abs().sum(axis=1) > 1e-12]
    assert set(changed) <= set(rebalance_sessions(L.DATES)), "sleeve weights moved off-schedule"
    beta = L.df("beta")[ROUTE]
    sessions = res["W"].index[-31:-1]           # the last research bar (2026-11-18) was downloaded intraday
    recs, diags = [], {}
    for t in sessions:
        tw = ticker_weights(res, t)
        agg = tw.groupby("symbol")["weight"].sum()
        cw, diag = apply_caps(agg, INITIAL_EQUITY, beta.loc[t].to_dict())
        diags[str(t.date())] = diag
        sleeve = tw.groupby("symbol")["sleeve"].agg(lambda s: "+".join(sorted(set(s))))
        for k in agg.index.union(cw.index):
            recs.append({"date": t, "symbol": k, "sleeve": sleeve.get(k, "route"), "weight": float(agg.get(k, 0.0)),
                         "weight_capped_100k": float(cw.get(k, 0.0))})
    ref = pd.DataFrame(recs)
    ref.to_csv(LAB / f"{tag}_reference_targets.csv", index=False)
    t = sessions[-1]
    last = ref[ref.date == t]
    out = {"strategy": f"{variant}: T9 ETF trend + D77 short book ({len(names)}-factor literature composite), "
                       "Ledoit-Wolf minimum variance", "variant": variant, "factors": names,
           "k_scale": k_scale, "n_short": N_SHORT, "etfs": ETF9, "route": ROUTE, "ticker_cap_of_initial": TICKER_CAP,
           "margin_cap": MARGIN_CAP, "rebalance_anchor": str(REBAL_ANCHOR.date()), "rebalance_every": REBAL_EVERY,
           "reproduction_check_max_abs_diff": chk, "metrics": windows(res["port"], L),
           "last_complete_session": str(t.date()),
           "last_session": {"sleeve_weights": res["W"].loc[t].to_dict(), "diag": diags[str(t.date())],
                            "etfs": last[last.symbol.isin(ETFS)].set_index("symbol")["weight_capped_100k"].round(4).to_dict(),
                            "n_shorts": int((last.weight_capped_100k < 0).sum()),
                            "short_gross": float(-last.weight_capped_100k[last.weight_capped_100k < 0].sum())},
           "max_spy_weight_uncapped_30d": float(max(d["spy_weight_uncapped"] for d in diags.values()))}
    (LAB / f"{tag}_DEPLOY.json").write_text(json.dumps(out, indent=2, default=float))
    print(json.dumps({"metrics": {w: round(v["sharpe"], 4) for w, v in out["metrics"].items()},
                      **out["last_session"]}, indent=2, default=float), flush=True)


def _sim_report(sim: pd.DataFrame, extra: dict, L) -> dict:
    reb = extra["rebalances"]
    yr = sim.groupby(sim.index.year)
    return {"metrics": windows(sim["net"], L),
            "fees_per_year_usd": yr["fees"].sum().round(0).to_dict(), "orders_per_year": yr["orders"].sum().to_dict(),
            "max_orders_per_day": int(sim["orders"].max()), "max_positions": int(sim["positions"].max()),
            "max_margin": float(sim["margin"].max()), "max_spy_vs_initial": float(sim["spy_vs_initial"].max()),
            "max_ticker_vs_initial": float(sim["max_ticker_vs_initial"].max()),
            "avg_short_names_target": float(reb["short_target"].mean()),
            "avg_short_names_held_whole_shares": float(reb["short_held"].mean()),
            "rebalances_scaled_down": int((reb["scale"] < 1).sum()),
            "rebalances_with_routing": int((reb["routed"] > 0).sum()),
            "daily_trims": extra["trims"], "daily_delevers": extra["delevers"],
            "yearly_return": {int(k): float(v) for k, v in yr["net"].apply(lambda x: (1 + x).prod() - 1).items()}}


def cmd_realistic(variant: str = DEPLOYED) -> None:
    """Execution rules are fixed in advance (TRADE_BAND, TRIM_AT, MARGIN_ALERT), not tuned; the naive variant
    (re-size everything at every rebalance, no daily checks) is reported for comparison."""
    L, F, P = research()
    tag = VARIANTS[variant][2]
    res = run_pstar(L, F, P, variant=variant)
    sim, extra = simulate(res, L)
    sim.to_csv(LAB / f"daily_{tag}_realistic.csv")
    naive, extra0 = simulate(res, L, band=0.0, daily_checks=False)
    out = {"ideal": windows(res["port"], L),
           "execution_rules": {"trade_band_of_equity": TRADE_BAND, "trim_ticker_above_of_initial": TRIM_AT,
                               "trim_to": TICKER_CAP, "delever_margin_above": MARGIN_ALERT, "delever_to": MARGIN_CAP},
           "realistic_100k": _sim_report(sim, extra, L), "naive_100k": _sim_report(naive, extra0, L)}
    (LAB / f"{tag}_REALISTIC.json").write_text(json.dumps(out, indent=2, default=float))
    print(json.dumps({k: {w: round(v["sharpe"], 4) for w, v in (out[k] if k == "ideal" else out[k]["metrics"]).items()}
                      for k in ("ideal", "realistic_100k", "naive_100k")}, indent=2), flush=True)


# ------------------------------------------------------------------ live: data refresh
def _fetch(symbols: list[str], start: str, adjustment: str) -> pd.DataFrame:
    import alpaca_client as A
    params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start, "adjustment": adjustment,
              "feed": "sip", "limit": 10000, "sort": "asc"}
    rows = []
    while True:
        r = A.get(f"{A.DATA_URL}/v2/stocks/bars", params)
        r.raise_for_status()
        j = r.json()
        for sym, bars in (j.get("bars") or {}).items():
            for b in bars:
                b["symbol"] = sym
                rows.append(b)
        tok = j.get("next_page_token")
        if not tok:
            break
        params["page_token"] = tok
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.rename(columns={"t": "date", "o": "open", "h": "high", "l": "low", "c": "close",
                            "v": "volume", "n": "trades", "vw": "vwap"})
    df["date"] = pd.to_datetime(df["date"]).dt.tz_convert("America/New_York").dt.normalize().dt.tz_localize(None)
    return df


def _batched(symbols: list[str], start: str, adjustment: str, size: int = 100) -> pd.DataFrame:
    parts = [_fetch(symbols[i:i + size], start, adjustment) for i in range(0, len(symbols), size)]
    parts = [p for p in parts if not p.empty]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def completed_before() -> pd.Timestamp:
    """Bars dated before this are complete (a session counts as complete from 16:30 ET)."""
    now = pd.Timestamp.now(tz="America/New_York")
    today = now.normalize().tz_localize(None)
    return today + pd.Timedelta(days=1) if (now.hour, now.minute) >= (16, 30) else today


def refresh_bars(adjustment: str) -> dict:
    """Re-download the last 14 calendar days for every stored symbol and splice them in. Adjusted series whose
    overlap no longer matches (new split/dividend factor) are re-downloaded in full so the history stays on
    one adjustment basis. The stored last bar is never used for the overlap test (it may have been intraday)."""
    path = DATA / ("bars_daily.parquet" if adjustment == "all" else "bars_daily_raw.parquet")
    old = pd.read_parquet(path)
    last = old["date"].max()
    start = last - pd.Timedelta(days=14)
    cut = completed_before()
    syms = sorted(old["symbol"].unique())
    new = _batched(syms, start.strftime("%Y-%m-%d"), adjustment)
    new = new[new["date"] < cut]
    refetch: list[str] = []
    if adjustment == "all" and not new.empty:
        o = old[(old["date"] >= start) & (old["date"] < last)].set_index(["symbol", "date"])["close"]
        n = new.set_index(["symbol", "date"])["close"]
        j = o.to_frame("o").join(n.rename("n"), how="inner")
        refetch = sorted(j.index[(j["n"] / j["o"] - 1).abs() > 1e-6].get_level_values(0).unique())
    keep = old[~((old["date"] >= start) & old["symbol"].isin(new["symbol"].unique()))]
    if refetch:
        full = _batched(refetch, "2019-10-01", adjustment)
        full = full[full["date"] < cut]
        keep = keep[~keep["symbol"].isin(refetch)]
        new = pd.concat([new[~new["symbol"].isin(refetch)], full], ignore_index=True)
    merged = (pd.concat([keep, new[keep.columns.intersection(new.columns)]], ignore_index=True)
              .drop_duplicates(["symbol", "date"], keep="last").sort_values(["symbol", "date"]))
    merged.to_parquet(path)
    info = {"file": path.name, "last_session": str(merged["date"].max().date()), "rows_spliced": int(len(new)),
            "symbols_refetched_full": len(refetch)}
    print("refresh", info, flush=True)
    return info


def snapshot_research_inputs() -> None:
    """Keep the exact research inputs once, before live refreshes overwrite them."""
    SNAPSHOT.mkdir(exist_ok=True)
    for f in ("bars_daily.parquet", "bars_daily_raw.parquet", "earnings_dates.parquet"):
        if not (SNAPSHOT / f).exists():
            shutil.copy2(DATA / f, SNAPSHOT / f)


def build_live_cache() -> None:
    """Lean version of lab_cache.py + lab_factors2.py: only the arrays and the 11 factors P* uses, same code."""
    LIVE_CACHE.mkdir(parents=True, exist_ok=True)
    import pipeline as v1
    from factors import market_beta, us_classics
    from panel import build_panel
    P = build_panel()
    p, U, ind = P["p"], P["U"], P["industry"]
    spy = p["close"]["SPY"].pct_change()
    beta = market_beta(p["close"], spy)
    mcap = p["mcap"]
    fwd1 = p["fwd_ret"].astype(float)
    vol60 = p["close"].pct_change().rolling(60, min_periods=40).std()
    assets = pd.read_parquet(DATA / "assets.parquet").set_index("symbol")       # frozen research snapshot
    shortable = (assets["shortable"] & assets["easy_to_borrow"]).reindex(U.columns).fillna(False)
    meta = {"dates": [str(d.date()) for d in U.index], "symbols": list(U.columns), "industry": ind.fillna("NA").tolist()}
    (LIVE_CACHE / "meta.json").write_text(json.dumps(meta))
    for name, arr in (("U", U.to_numpy()), ("fwd1", fwd1.to_numpy(np.float32)), ("beta", beta.to_numpy(np.float32)),
                      ("vol60", vol60.to_numpy(np.float32)), ("mcap", mcap.to_numpy(np.float32)),
                      ("spy_fwd", fwd1["SPY"].to_numpy(np.float32)), ("shortable", shortable.to_numpy())):
        np.save(LIVE_CACHE / f"{name}.npy", arr)
    cls = us_classics(p["close"], p["amt"], spy)
    raw = {k: cls[k].astype("float32") for k in ("mom12_1", "ivol60")}
    del cls, P, p, fwd1, vol60, beta
    gc.collect()
    for k, df in v1.preprocess(raw, U, ind, mcap).items():
        np.save(LIVE_CACHE / f"f_{k}.npy", df.to_numpy(np.float32))
    del raw
    gc.collect()
    os.environ["QR_CACHE_DIR"] = str(LIVE_CACHE)
    import lab_factors2 as F2                                   # reads the live meta written above
    raw = F2.build()
    raw.update(F2.family2(mcap.astype(float)))                  # round-14 factors (used by variant pstar15)
    ind2 = pd.Series(meta["industry"], index=U.columns).replace("NA", np.nan)
    for k, df in v1.preprocess(raw, U, ind2, mcap).items():
        np.save(LIVE_CACHE / f"f_{k}.npy", df.to_numpy(np.float32))
    (LIVE_CACHE / "factors.json").write_text(json.dumps(PRIOR_SET + NEW15))
    print(f"live cache built: {len(meta['dates'])} sessions, last {meta['dates'][-1]}", flush=True)


def _file_info(path: Path) -> dict:
    st = path.stat()
    return {"bytes": st.st_size, "mtime_utc": pd.Timestamp(st.st_mtime, unit="s", tz="UTC").isoformat()}


def cmd_live(args) -> None:
    os.environ["QR_CACHE_DIR"] = str(LIVE_CACHE)
    refresh = {}
    if not args.skip_refresh:
        snapshot_research_inputs()
        refresh["adjusted"] = refresh_bars("all")
        refresh["raw"] = refresh_bars("raw")
        if not args.skip_events:
            import fundamentals
            fundamentals.events()
            refresh["events"] = "refreshed"
    if not args.skip_cache:
        build_live_cache()
    L, F, P = research(LIVE_CACHE)
    variant = args.variant
    names, k_scale, tag = VARIANTS[variant]
    res = run_pstar(L, F, P, variant=variant)
    t = res["W"].index[-1]
    hold = dict(h.split("=", 1) for h in args.hold)
    hold = {k.upper(): float(v) for k, v in hold.items()}
    tw = ticker_weights(res, t)
    agg = tw.groupby("symbol")["weight"].sum()
    beta = L.df("beta")
    cw, diag = apply_caps(agg, args.equity, beta.loc[t, ROUTE].to_dict(), hold)
    sleeve = tw.groupby("symbol")["sleeve"].agg(lambda s: "+".join(sorted(set(s))))
    reb = rebalance_sessions(L.DATES)
    last_reb = reb[reb <= t][-1]
    pos = int(np.searchsorted(L.DATES[L.DATES >= REBAL_ANCHOR], t))
    targets = [{"symbol": k, "side": "long" if v > 0 else "short", "weight": round(float(v), 8),
                "dollars": round(float(v) * args.equity, 2), "uncapped_weight": round(float(agg.get(k, 0.0)), 8),
                "sleeve": sleeve.get(k, "route")} for k, v in cw.items()]
    targets.sort(key=lambda d: (d["side"], d["symbol"]))
    body = json.dumps(targets, sort_keys=True, separators=(",", ":")).encode()
    # consistency with the research run on sessions both runs know completely
    pick = pd.read_csv(LAB / f"daily_{tag}_pick.csv", index_col=0, parse_dates=True)
    common = res["W"].index.intersection(pick.index[:-2])
    cons = {"sessions_compared": int(len(common)),
            "sleeve_weight_max_abs_diff": float((res["W"].loc[common] - pick.loc[common, ["w_T9", "w_D77"]]
                                                 .set_axis(["T9", "D77"], axis=1)).abs().max().max()),
            "return_max_abs_diff": float((res["port"].loc[common] - pick.loc[common, "P"]).abs().max())}
    out = {"strategy_id": f"{tag.lower()}-{variant}", "variant": variant, "factors": names, "schema": 1,
           "session": str(t.date()),
           "generated_at_utc": pd.Timestamp.now(tz="UTC").isoformat(),
           "rebalance_session": bool(t == last_reb), "last_rebalance_session": str(last_reb.date()),
           "sessions_until_next_rebalance": int((REBAL_EVERY - pos % REBAL_EVERY) % REBAL_EVERY or REBAL_EVERY),
           "trade_at": "next regular-session open after `session`",
           "equity_input": args.equity, "initial_equity": INITIAL_EQUITY, "hold": hold,
           "k_scale": k_scale, "sleeve_weights": {k: float(v) for k, v in res["W"].loc[t].items()},
           "signal_guard_held_previous_book": bool(res["guard"].get(last_reb, False)),
           "targets": targets, "diagnostics": diag, "checksum_sha256": hashlib.sha256(body).hexdigest(),
           "data": {"refresh": refresh, "last_bar_session": str(L.DATES[-1].date()),
                    "files": {f: _file_info(DATA / f) for f in ("bars_daily.parquet", "bars_daily_raw.parquet",
                                                                "earnings_dates.parquet", "fund_frames.parquet",
                                                                "shares_q.parquet", "assets.parquet")}},
           "consistency_with_research": cons}
    LIVE_OUT.mkdir(parents=True, exist_ok=True)
    f = LIVE_OUT / f"{tag.lower()}_targets_{t.date()}.json"
    f.write_text(json.dumps(out, indent=2, default=float))
    print(json.dumps({k: out[k] for k in ("session", "rebalance_session", "sleeve_weights", "diagnostics",
                                         "consistency_with_research")}, indent=2, default=float), flush=True)
    print(f"wrote {f}", flush=True)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["reference", "realistic", "live"])
    ap.add_argument("--equity", type=float, default=INITIAL_EQUITY)
    ap.add_argument("--hold", nargs="*", default=[], help="discretionary positions, e.g. ANTH=40000 (short: negative)")
    ap.add_argument("--skip-refresh", action="store_true")
    ap.add_argument("--skip-events", action="store_true")
    ap.add_argument("--skip-cache", action="store_true", help="reuse data/cache_live (only after a same-day build)")
    ap.add_argument("--variant", choices=sorted(VARIANTS), default=DEPLOYED,
                    help=f"strategy variant (deployed: {DEPLOYED})")
    args = ap.parse_args(argv)
    {"reference": lambda: cmd_reference(args.variant), "realistic": lambda: cmd_realistic(args.variant),
     "live": lambda: cmd_live(args)}[args.cmd]()


if __name__ == "__main__":
    sys.exit(main())
