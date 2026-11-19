"""Experiment runner on the cached factor library.

Discipline: everything is chosen on the selection window (BT_START .. VAL_END).
Holdout metrics (HOLD_START ..) are written to reports/lab/holdout_sealed.csv
and are NOT printed; they are opened once, at the very end.

usage:
  python lab_run.py screen                      # single-factor IC on the selection window
  python lab_run.py exp <name>                  # run one named experiment (see EXPERIMENTS)
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import ElasticNetCV, LassoCV, RidgeCV
from sklearn.model_selection import TimeSeriesSplit

import pipeline as v1
import pipeline_v2 as v2
from pipeline_v3 import make_target_fn

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "cache"
LAB = ROOT / "reports" / "lab"
LAB.mkdir(parents=True, exist_ok=True)
BT_START = pd.Timestamp("2021-11-18")
VAL_END = pd.Timestamp("2025-11-17")
HOLD_START = pd.Timestamp("2025-11-18")
H, TRAIN_DAYS, RETRAIN_EVERY = 5, 252, 5

_meta = json.loads((CACHE / "meta.json").read_text())
DATES = pd.DatetimeIndex(_meta["dates"])
SYMS = pd.Index(_meta["symbols"])
ALL_FACTORS = json.loads((CACHE / "factors.json").read_text())


def df(name: str) -> pd.DataFrame:
    return pd.DataFrame(np.load(CACHE / f"{name}.npy"), index=DATES, columns=SYMS)


def factor(name: str) -> np.ndarray:
    return np.load(CACHE / f"f_{name}.npy", mmap_mode="r")


# ------------------------------------------------------------------ screening
def screen() -> pd.DataFrame:
    U = df("U").astype(bool)
    fwd1, fwd5 = df("fwd1").where(U), df("fwd5").where(U)
    sel = (DATES >= BT_START) & (DATES <= VAL_END)
    rows = []
    for k in ALL_FACTORS:
        f = pd.DataFrame(np.asarray(factor(k)), index=DATES, columns=SYMS).loc[sel]
        ic1 = v1.rank_ic(f, fwd1.loc[sel]).dropna()
        ic5 = v1.rank_ic(f, fwd5.loc[sel]).dropna()
        rows.append({"factor": k, "IC1": ic1.mean(), "t1": v1.nw_tstat(ic1, 5),
                     "IC5": ic5.mean(), "t5": v1.nw_tstat(ic5, 10), "IC5_pos": (ic5 > 0).mean()})
        print(f"screen {len(rows)}/{len(ALL_FACTORS)} {k}", flush=True)
    out = pd.DataFrame(rows).set_index("factor").sort_values("t5", key=abs, ascending=False)
    out.to_csv(LAB / "screen.csv")
    return out


# ------------------------------------------------------------------ models
def make_model(kind: str):
    cv = TimeSeriesSplit(5)
    if kind == "lasso":
        return LassoCV(cv=cv, alphas=30, max_iter=10000)
    if kind == "enet":
        return ElasticNetCV(cv=cv, l1_ratio=[0.2, 0.5, 0.8], alphas=20, max_iter=10000)
    if kind == "ridge":
        return RidgeCV(alphas=np.logspace(0, 6, 25), cv=cv)
    raise ValueError(kind)


def tradable_mask() -> np.ndarray:
    """Names we can actually short: shortable & easy-to-borrow & market cap >= $1B (inside U)."""
    U = np.load(CACHE / "U.npy")
    sh = np.load(CACHE / "shortable.npy").astype(bool)
    mc = np.load(CACHE / "mcap.npy")
    return U & sh[None, :] & (np.nan_to_num(mc) >= 1e9)


def fit_scores(names: list[str], kind: str, universe: str = "all",
               target: str = "y_resid5") -> tuple[pd.DataFrame, pd.DataFrame]:
    U = np.load(CACHE / "U.npy") if universe == "all" else tradable_mask()
    Y = np.load(CACHE / f"{target}.npy")
    X3 = np.stack([np.asarray(factor(k)) for k in names], -1)          # (T, N, K) float32
    scores = np.full(U.shape, np.nan, dtype=np.float32)
    coefs, coef, icpt = {}, None, 0.0
    bt_idx = int(np.searchsorted(DATES, BT_START))
    for t in range(bt_idx, len(DATES)):
        if coef is None or (t - bt_idx) % RETRAIN_EVERY == 0:
            if (t - bt_idx) % 50 == 0:
                print(f"fit {t - bt_idx}/{len(DATES) - bt_idx}", flush=True)
            hi = t - 1 - H
            lo = max(0, hi - TRAIN_DAYS)
            Xs, ys = [], []
            for s in range(lo, hi):
                m = U[s] & np.isfinite(Y[s]) & np.isfinite(X3[s]).all(1)
                if m.sum() >= 100:
                    Xs.append(X3[s, m]); ys.append(Y[s, m])
            if Xs:
                Xtr = np.vstack(Xs).astype(np.float64); ytr = np.concatenate(ys).astype(np.float64)
                if len(ytr) > 300_000:
                    sel = np.sort(np.random.default_rng(0).choice(len(ytr), 300_000, replace=False))
                    Xtr, ytr = Xtr[sel], ytr[sel]
                mdl = make_model(kind).fit(Xtr, ytr)
                coef = np.asarray(mdl.coef_, dtype=np.float64).copy()
                coefs[DATES[t]] = dict(zip(names, coef))
        if coef is not None:
            m = U[t] & np.isfinite(X3[t]).all(1)
            scores[t, m] = X3[t, m] @ coef
    return pd.DataFrame(scores, index=DATES, columns=SYMS), pd.DataFrame(coefs).T


# ------------------------------------------------------------------ backtest + metrics
def run_book(scores: pd.DataFrame, n: int, rebal: int, smooth: int, inv_vol: bool = True) -> pd.DataFrame:
    v2.REBAL_EVERY, v2.SMOOTH = rebal, smooth
    shortable = pd.Series(np.load(CACHE / "shortable.npy"), index=SYMS)
    fn = make_target_fn(shortable, df("mcap"), df("vol60"), inv_vol)
    beta = df("beta")
    spy = pd.Series(np.load(CACHE / "spy_fwd.npy"), index=DATES)
    fwd1 = df("fwd1")
    orig = v1.target_weights
    v1.target_weights = lambda row, n_, prev, buf: fn(row, n_, prev, buf, row.name)
    try:
        return v2.backtest(scores, fwd1, beta, spy, n=n)
    finally:
        v1.target_weights = orig


def make_target_fn_ind(shortable: pd.Series, mcap: pd.DataFrame, vol60: pd.DataFrame, industry: pd.Series):
    """Industry-neutral book: long/short slots per industry in proportion to the
    industry's share of the scored universe; inverse-vol weights inside each
    industry leg; equal long and short dollars per industry (industry-neutral)."""
    def target(score_row: pd.Series, n: int, prev: pd.Series, buffer: float, t) -> pd.Series:
        s = score_row.dropna()
        if len(s) < 4 * n:
            return pd.Series(dtype=float)
        ind = industry.reindex(s.index).fillna("NA")
        can_short = shortable.reindex(s.index).fillna(False) & (mcap.loc[t].reindex(s.index) >= 1e9)
        share = ind.value_counts(normalize=True)
        slots = (share * n).round().astype(int)
        slots = slots[slots > 0]
        vol = vol60.loc[t].reindex(s.index).clip(lower=0.005)
        vol = vol.fillna(vol.median())
        legs = []
        total_slots = slots.sum()
        for g, k in slots.items():
            sg = s[ind == g]
            sgs = sg[can_short.reindex(sg.index)]
            if len(sg) < 2 * k or len(sgs) < k:
                continue
            held_l = [c for c in prev[prev > 0].index if c in sg.index and sg.rank(ascending=False)[c] <= k * buffer]
            held_s = [c for c in prev[prev < 0].index if c in sgs.index and sgs.rank()[c] <= k * buffer]
            longs = (held_l + [c for c in sg.sort_values(ascending=False).index if c not in held_l])[:k]
            shorts = (held_s + [c for c in sgs.sort_values().index if c not in held_s and c not in longs])[:k]
            gross_g = v1.GROSS_LONG * k / total_slots          # same dollars long and short in this industry
            for names, sign in ((longs, 1.0), (shorts, -1.0)):
                iv = 1 / vol[names]
                legs.append(sign * iv / iv.sum() * gross_g)
        if not legs:
            return pd.Series(dtype=float)
        w = pd.concat(legs)
        w = w[~w.index.duplicated()]
        kept = w.index.intersection(prev.index)
        same = kept[np.sign(prev[kept]) == np.sign(w[kept])]
        w[same] = prev[same]
        return w
    return target


def make_target_fn_short(shortable: pd.Series, mcap: pd.DataFrame, vol60: pd.DataFrame):
    """Short-alpha book: short the n lowest-scored borrowable names (>= $1B), inverse-vol
    weights, 70% gross; no single-stock longs. v2.backtest then sizes a long SPY
    position to offset the shorts' beta, so the long side is one cheap index trade."""
    def target(score_row: pd.Series, n: int, prev: pd.Series, buffer: float, t) -> pd.Series:
        s = score_row.dropna()
        ok = shortable.reindex(s.index).fillna(False) & (mcap.loc[t].reindex(s.index) >= 1e9)
        ss = s[ok]
        if len(ss) < 4 * n:
            return pd.Series(dtype=float)
        rk = ss.rank()
        held = [c for c in prev[prev < 0].index if c in rk.index and rk[c] <= n * buffer]
        shorts = (held + [c for c in rk.sort_values().index if c not in held])[:n]
        vol = vol60.loc[t].reindex(shorts).clip(lower=0.005)
        iv = (1 / vol.fillna(vol.median())).astype(float)
        w = -(iv / iv.sum() * v1.GROSS_SHORT)
        w = w.clip(lower=-v1.GROSS_SHORT / n * 3); w = w / -w.sum() * v1.GROSS_SHORT
        kept = w.index.intersection(prev.index)
        w[kept] = prev[kept].where(prev[kept] < 0, w[kept])
        return w
    return target


def run_book_short(scores: pd.DataFrame, n: int, rebal: int, smooth: int) -> pd.DataFrame:
    v2.REBAL_EVERY, v2.SMOOTH = rebal, smooth
    fn = make_target_fn_short(pd.Series(np.load(CACHE / "shortable.npy"), index=SYMS), df("mcap"), df("vol60"))
    spy = pd.Series(np.load(CACHE / "spy_fwd.npy"), index=DATES)
    orig = v1.target_weights
    v1.target_weights = lambda row, n_, prev, buf: fn(row, n_, prev, buf, row.name)
    try:
        return v2.backtest(scores, df("fwd1"), df("beta"), spy, n=n)
    finally:
        v1.target_weights = orig


FF12_ETF = {"NoDur": "XLP", "Durbl": "XLY", "Manuf": "XLI", "Enrgy": "XLE", "Chems": "XLB", "BusEq": "XLK",
            "Telcm": "XLC", "Utils": "XLU", "Shops": "XLY", "Hlth": "XLV", "Money": "XLF", "Other": "SPY", "NA": "SPY"}


def make_target_fn_short_sector(shortable: pd.Series, mcap: pd.DataFrame, vol60: pd.DataFrame,
                                industry: pd.Series, beta: pd.DataFrame):
    """Short-alpha book hedged with the matching SPDR sector ETF: each short is offset
    by a long position in its industry's ETF sized to the short's SPY beta divided by
    the ETF's SPY beta. Hedges are recomputed at every rebalance (not left to drift).
    Any residual SPY beta is closed by v2.backtest's SPY overlay."""
    base = make_target_fn_short(shortable, mcap, vol60)
    etfs = set(FF12_ETF.values())

    def target(score_row: pd.Series, n: int, prev: pd.Series, buffer: float, t) -> pd.Series:
        prev_stocks = prev[[c for c in prev.index if c not in etfs]].astype(float) if len(prev) else prev
        w = base(score_row, n, prev_stocks, buffer, t)
        if w.empty:
            return w
        b = beta.loc[t]
        hedge = {}
        for name, wi in w.items():
            etf = FF12_ETF.get(industry.get(name, "NA"), "SPY")
            bi = float(np.clip(np.nan_to_num(b.get(name, 1.0), nan=1.0), 0.3, 3.0))
            be = float(np.clip(np.nan_to_num(b.get(etf, 1.0), nan=1.0), 0.5, 1.5))
            hedge[etf] = hedge.get(etf, 0.0) + abs(wi) * bi / be
        return pd.concat([w, pd.Series(hedge)])
    return target


def run_book_short_sector(scores: pd.DataFrame, n: int, rebal: int, smooth: int) -> pd.DataFrame:
    v2.REBAL_EVERY, v2.SMOOTH = rebal, smooth
    beta = df("beta")
    fn = make_target_fn_short_sector(pd.Series(np.load(CACHE / "shortable.npy"), index=SYMS), df("mcap"),
                                     df("vol60"), pd.Series(_meta["industry"], index=SYMS), beta)
    spy = pd.Series(np.load(CACHE / "spy_fwd.npy"), index=DATES)
    orig = v1.target_weights
    v1.target_weights = lambda row, n_, prev, buf: fn(row, n_, prev, buf, row.name)
    try:
        return v2.backtest(scores, df("fwd1"), beta, spy, n=n)
    finally:
        v1.target_weights = orig


def fit_tail_scores(names: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Linear-probability LASSO for 'residual 5-day return lands in the bottom 10%',
    trained on the tradable universe. Score = -P(bottom tail), so the lowest scores
    are the likeliest losers (what the short book sells)."""
    Y0 = np.load(CACHE / "y_resid5.npy")
    tail = np.where(np.isfinite(Y0), (Y0 <= -0.4).astype(np.float32), np.nan)
    np.save(CACHE / "y_tail10.npy", tail)
    sc, co = fit_scores(names, "lasso", universe="tradable", target="y_tail10")
    return -sc, co


def run_book_ind(scores: pd.DataFrame, n: int, rebal: int, smooth: int) -> pd.DataFrame:
    v2.REBAL_EVERY, v2.SMOOTH = rebal, smooth
    shortable = pd.Series(np.load(CACHE / "shortable.npy"), index=SYMS)
    industry = pd.Series(_meta["industry"], index=SYMS)
    fn = make_target_fn_ind(shortable, df("mcap"), df("vol60"), industry)
    spy = pd.Series(np.load(CACHE / "spy_fwd.npy"), index=DATES)
    orig = v1.target_weights
    v1.target_weights = lambda row, n_, prev, buf: fn(row, n_, prev, buf, row.name)
    try:
        return v2.backtest(scores, df("fwd1"), df("beta"), spy, n=n)
    finally:
        v1.target_weights = orig


def seg_metrics(bt: pd.DataFrame, lo, hi) -> dict:
    part = bt.loc[lo:hi]
    if len(part) < 40:
        return {}
    m = v1.metrics(part, "net")
    m["beta"] = float(np.cov(part["net"], part["spy"])[0, 1] / np.var(part["spy"]))
    return m


def log_trial(name: str, cfg: dict, bt: pd.DataFrame, ic: dict) -> dict:
    val = seg_metrics(bt, BT_START, VAL_END)
    hold = seg_metrics(bt, HOLD_START, None)
    row = {"trial": name, "time": time.strftime("%Y-%m-%d %H:%M"), **cfg, **ic,
           **{f"val_{k}": val.get(k) for k in ("sharpe", "annual_return", "max_drawdown", "annual_vol",
                                                "avg_daily_turnover", "monthly_win_rate", "beta")}}
    with (LAB / "trials.jsonl").open("a", encoding="utf-8") as fh:        # one JSON per trial (schema-free)
        fh.write(json.dumps(row, default=float) + "\n")
    with (LAB / "holdout_sealed.jsonl").open("a", encoding="utf-8") as fh:   # sealed: not printed
        fh.write(json.dumps({"trial": name, **{f"hold_{k}": v for k, v in hold.items()}}, default=float) + "\n")
    bt.to_csv(LAB / f"daily_{name}.csv")
    print(f"[{name}] VAL sharpe={val.get('sharpe', float('nan')):.2f} ann={val.get('annual_return', float('nan')):.2%} "
          f"mdd={val.get('max_drawdown', float('nan')):.2%} vol={val.get('annual_vol', float('nan')):.1%} "
          f"turn={val.get('avg_daily_turnover', float('nan')):.2%}", flush=True)
    return row


def composite_ic(scores: pd.DataFrame) -> dict:
    U = df("U").astype(bool)
    sel = (DATES >= BT_START) & (DATES <= VAL_END)
    ic5 = v1.rank_ic(scores.loc[sel], df("fwd5").where(U).loc[sel]).dropna()
    return {"val_IC5": ic5.mean(), "val_IC5_t": v1.nw_tstat(ic5, 10)}


V3_FACTORS = ["pv1", "pv3", "pv4", "pv5", "pv6", "pv7", "pv8", "pv9", "pv10", "pv11", "pv12", "dc1_approx",
              "dc2_approx", "dc3_approx", "dc4_approx", "md1_gap", "rev21", "mom12_1", "high52", "amihud",
              "ivol60", "resid_mom", "max21"]


def screened(min_t: float = 2.0) -> list[str]:
    """Keep a factor if its selection-window IC is significant at either horizon (NW |t| >= min_t)."""
    s = pd.read_csv(LAB / "screen.csv", index_col=0)
    return s.index[(s["t5"].abs() >= min_t) | (s["t1"].abs() >= min_t)].tolist()


def experiment(name: str) -> None:
    book = dict(n=20, rebal=5, smooth=5)
    if name == "E1_v3base":
        names, kind = V3_FACTORS, "lasso"
    elif name == "E2_all_lasso":
        names, kind = ALL_FACTORS, "lasso"
    elif name == "E3_screen_lasso":
        names, kind = screened(), "lasso"
    elif name == "E4_screen_ridge":
        names, kind = screened(), "ridge"
    elif name == "E5_screen_enet":
        names, kind = screened(), "enet"
    else:
        raise SystemExit(f"unknown experiment {name}")
    print(f"{name}: {len(names)} factors, model={kind}", flush=True)
    scores, coefs = fit_scores(names, kind)
    scores.to_parquet(LAB / f"scores_{name}.parquet"); coefs.to_csv(LAB / f"coefs_{name}.csv")
    ic = composite_ic(scores)
    print(f"composite {ic}", flush=True)
    bt = run_book(scores, **book)
    log_trial(name, {"model": kind, "n_factors": len(names), **book}, bt, ic)


def book_grid(base: str) -> None:
    """Portfolio-construction grid on one model's scores (selection-window metrics only)."""
    scores = pd.read_parquet(LAB / f"scores_{base}.parquet")
    ic = composite_ic(scores)
    for n in (20, 30, 40):
        for rebal in (5, 10):
            cfg = dict(n=n, rebal=rebal, smooth=5)
            bt = run_book(scores, **cfg)
            log_trial(f"{base}_n{n}_r{rebal}", {"model": base, **cfg}, bt, ic)


def ensemble(parts: list[str], name: str = "E6_ensemble") -> None:
    """Average of cross-sectionally ranked scores from several models."""
    acc = None
    for p in parts:
        s = pd.read_parquet(LAB / f"scores_{p}.parquet")
        r = s.rank(axis=1, pct=True) - 0.5
        acc = r if acc is None else acc.add(r, fill_value=0)
    scores = acc / len(parts)
    scores.to_parquet(LAB / f"scores_{name}.parquet")
    ic = composite_ic(scores)
    print(f"composite {name} {ic}", flush=True)
    book = dict(n=20, rebal=5, smooth=5)
    log_trial(name, {"model": "+".join(parts), **book}, run_book(scores, **book), ic)


def queue() -> None:
    """Full research queue; selection uses validation-window Sharpe only."""
    print(screen().round(4).to_string(), flush=True)
    for e in ("E1_v3base", "E2_all_lasso", "E3_screen_lasso", "E4_screen_ridge", "E5_screen_enet"):
        try:
            experiment(e)
        except Exception as ex:  # noqa: BLE001
            print(f"{e} failed: {ex}", flush=True)
    ensemble(["E3_screen_lasso", "E4_screen_ridge", "E5_screen_enet"])
    t = pd.read_csv(LAB / "trials.csv")
    best = t.sort_values("val_sharpe", ascending=False).iloc[0]["trial"]
    print(f"best model on validation: {best}", flush=True)
    book_grid(best)


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "screen":
        print(screen().round(4).to_string())
    elif cmd == "exp":
        experiment(sys.argv[2])
    elif cmd == "grid":
        book_grid(sys.argv[2])
    elif cmd == "queue":
        queue()
    elif cmd == "round2":
        # E8: retrain on the tradable universe; then three book types on E1 and E8 scores
        print("E8_tradable_lasso: v3 factors, LASSO trained on shortable >= $1B names", flush=True)
        sc8, co8 = fit_scores(V3_FACTORS, "lasso", universe="tradable")
        sc8.to_parquet(LAB / "scores_E8_tradable_lasso.parquet"); co8.to_csv(LAB / "coefs_E8_tradable_lasso.csv")
        for base in ("E1_v3base", "E8_tradable_lasso"):
            scores = pd.read_parquet(LAB / f"scores_{base}.parquet")
            ic = composite_ic(scores)
            print(f"composite {base} {ic}", flush=True)
            for n in (20, 30, 40):
                cfg = dict(n=n, rebal=5, smooth=5)
                log_trial(f"{base}_SHORTSPY_n{n}", {"model": base, "book": "short_alpha_spy_long", **cfg},
                          run_book_short(scores, **cfg), ic)
                log_trial(f"{base}_IND_n{n}", {"model": base, "book": "industry_neutral", **cfg},
                          run_book_ind(scores, **cfg), ic)
            if base == "E8_tradable_lasso":
                cfg = dict(n=20, rebal=5, smooth=5)
                log_trial(f"{base}_LS_n20", {"model": base, "book": "long_short_v3", **cfg}, run_book(scores, **cfg), ic)
    elif cmd == "round3":
        base, n = sys.argv[2], int(sys.argv[3])
        scores = pd.read_parquet(LAB / f"scores_{base}.parquet")
        ic = composite_ic(scores)
        for rebal in (5, 10):
            cfg = dict(n=n, rebal=rebal, smooth=5)
            log_trial(f"{base}_SECTORHEDGE_n{n}_r{rebal}", {"model": base, "book": "short_alpha_sector_etf", **cfg},
                      run_book_short_sector(scores, **cfg), ic)
        print("E10_tail: LASSO on P(bottom-10% residual return), tradable universe", flush=True)
        st, ct = fit_tail_scores(V3_FACTORS)
        st.to_parquet(LAB / "scores_E10_tail.parquet"); ct.to_csv(LAB / "coefs_E10_tail.csv")
        ic_t = composite_ic(st)
        print(f"composite E10_tail {ic_t}", flush=True)
        cfg = dict(n=n, rebal=5, smooth=5)
        log_trial(f"E10_tail_SHORTSPY_n{n}", {"model": "E10_tail", "book": "short_alpha_spy_long", **cfg},
                  run_book_short(st, **cfg), ic_t)
        log_trial(f"E10_tail_SECTORHEDGE_n{n}_r5", {"model": "E10_tail", "book": "short_alpha_sector_etf", **cfg},
                  run_book_short_sector(st, **cfg), ic_t)
        print("E11_tradable_all: LASSO on all cached factors, tradable universe", flush=True)
        s11, c11 = fit_scores(ALL_FACTORS, "lasso", universe="tradable")
        s11.to_parquet(LAB / "scores_E11_tradable_all.parquet"); c11.to_csv(LAB / "coefs_E11_tradable_all.csv")
        ic11 = composite_ic(s11)
        print(f"composite E11_tradable_all {ic11}", flush=True)
        log_trial(f"E11_tradable_all_SHORTSPY_n{n}", {"model": "E11_tradable_all", "book": "short_alpha_spy_long", **cfg},
                  run_book_short(s11, **cfg), ic11)
    elif cmd == "shortbook":                     # robustness: short-alpha + SPY book at several sizes
        base = sys.argv[2]
        scores = pd.read_parquet(LAB / f"scores_{base}.parquet")
        ic = composite_ic(scores)
        for n in [int(x) for x in sys.argv[3:]]:
            cfg = dict(n=n, rebal=5, smooth=5)
            log_trial(f"{base}_SHORTSPY_n{n}", {"model": base, "book": "short_alpha_spy_long", **cfg},
                      run_book_short(scores, **cfg), ic)
    elif cmd == "indbook":                       # industry-neutral book on the best model so far
        t = pd.read_csv(LAB / "trials.csv")
        base = sys.argv[2] if len(sys.argv) > 2 else \
            t[~t["trial"].str.contains("_n\\d+_r\\d+$")].sort_values("val_sharpe", ascending=False).iloc[0]["trial"]
        scores = pd.read_parquet(LAB / f"scores_{base}.parquet")
        ic = composite_ic(scores)
        for n in (20, 30, 40):
            cfg = dict(n=n, rebal=5, smooth=5)
            log_trial(f"{base}_IND_n{n}", {"model": base, "book": "industry_neutral", **cfg},
                      run_book_ind(scores, **cfg), ic)
    print("done", flush=True)
