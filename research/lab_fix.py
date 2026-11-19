"""Round 4 — corrected pipeline after finding the tie-break artefact.

Bug found: LassoCV returned all-zero coefficients in 35-68% of retrains. Scores were
then constant across stocks and the book picked names by index order (alphabetical),
so earlier LASSO/ENet results (incl. v2/v3 and lab rounds 1-3) are contaminated.

Fixes, applied to every model below:
  1. carry-forward: an all-zero fit keeps the last non-zero coefficient vector
     (no signal -> keep the previous view instead of an empty one);
  2. signal guard: if a day's scores have < 50 distinct values the book does not
     trade (holds existing positions) - never alphabetical selection;
  3. two combiners that cannot collapse to zero: Ridge and trailing-ICIR weights.

usage: python lab_fix.py round4
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

import lab_run as L
import pipeline as v1

TRADABLE = None


def tradable() -> np.ndarray:
    global TRADABLE
    if TRADABLE is None:
        TRADABLE = L.tradable_mask()
    return TRADABLE


# ------------------------------------------------------------------ guard
def guarded(fn):
    """Wrap a target function: no distinct signal that day -> hold previous book."""
    def g(row: pd.Series, n: int, prev: pd.Series, buf: float, t):
        s = row.dropna()
        if s.nunique() < 50 or float(s.std()) < 1e-12:
            return prev.copy() if len(prev) else pd.Series(dtype=float)
        return fn(row, n, prev, buf, t)
    return g


def run_guarded(scores: pd.DataFrame, book: str, n: int, rebal: int = 5, smooth: int = 5) -> pd.DataFrame:
    L.v2.REBAL_EVERY, L.v2.SMOOTH = rebal, smooth
    sh = pd.Series(np.load(L.CACHE / "shortable.npy"), index=L.SYMS)
    if book == "shortspy":
        fn = L.make_target_fn_short(sh, L.df("mcap"), L.df("vol60"))
    elif book == "ls":
        from pipeline_v3 import make_target_fn
        fn = make_target_fn(sh, L.df("mcap"), L.df("vol60"), True)
    else:
        raise ValueError(book)
    fn = guarded(fn)
    spy = pd.Series(np.load(L.CACHE / "spy_fwd.npy"), index=L.DATES)
    orig = v1.target_weights
    v1.target_weights = lambda row, n_, prev, buf: fn(row, n_, prev, buf, row.name)
    try:
        return L.v2.backtest(scores, L.df("fwd1"), L.df("beta"), spy, n=n)
    finally:
        v1.target_weights = orig


# ------------------------------------------------------------------ scores
def rescore_carry_forward(base: str, names: list[str], mask: np.ndarray) -> pd.DataFrame:
    """Rebuild scores from a saved coefficient history, replacing all-zero fits by the
    last non-zero vector (forward-filled between weekly retrains)."""
    c = pd.read_csv(L.LAB / f"coefs_{base}.csv", index_col=0)
    c.index = pd.to_datetime(c.index)
    c = c[names]
    zero = (c.abs() < 1e-12).all(axis=1)
    c = c.mask(zero).ffill()                        # all-zero fit -> previous non-zero fit
    daily = c.reindex(L.DATES).ffill()
    F = [L.factor(k) for k in names]
    out = np.full(mask.shape, np.nan, dtype=np.float32)
    bt = int(np.searchsorted(L.DATES, L.BT_START))
    for t in range(bt, len(L.DATES)):
        w = daily.iloc[t].to_numpy()
        if not np.isfinite(w).all():
            continue
        X = np.stack([np.asarray(f[t]) for f in F], -1)
        m = mask[t] & np.isfinite(X).all(1)
        out[t, m] = X[m] @ w
    return pd.DataFrame(out, index=L.DATES, columns=L.SYMS)


def icir_scores(names: list[str], mask: np.ndarray, win: int = 252, min_t: float = 1.5) -> pd.DataFrame:
    """Trailing-ICIR weights from daily rank IC vs. the 5-day forward return, measured
    inside the tradable universe; an IC observed at s is usable only from s+6 on."""
    fwd5 = L.df("fwd5").where(pd.DataFrame(mask, index=L.DATES, columns=L.SYMS))
    ics = {}
    for i, k in enumerate(names):
        f = pd.DataFrame(np.asarray(L.factor(k)), index=L.DATES, columns=L.SYMS).where(fwd5.notna())
        ics[k] = f.rank(axis=1).corrwith(fwd5.rank(axis=1), axis=1)
        print(f"ic {i + 1}/{len(names)}", flush=True)
    ic = pd.DataFrame(ics).shift(L.H + 1)                # usable only after the 5-day return is known
    mu = ic.rolling(win, min_periods=120).mean()
    sd = ic.rolling(win, min_periods=120).std()
    n = ic.rolling(win, min_periods=120).count()
    w = (mu / sd).where((mu / sd * np.sqrt(n)).abs() > min_t, 0.0)
    w = w.mask((w == 0).all(axis=1)).ffill().fillna(0.0)  # no significant factor -> keep last weights
    w.to_csv(L.LAB / "icir_tradable_weights.csv")
    F = [L.factor(k) for k in names]
    out = np.full(mask.shape, np.nan, dtype=np.float32)
    bt = int(np.searchsorted(L.DATES, L.BT_START))
    for t in range(bt, len(L.DATES)):
        wt = w.iloc[t].to_numpy()
        if not np.any(wt):
            continue
        X = np.stack([np.asarray(f[t]) for f in F], -1)
        m = mask[t] & np.isfinite(X).all(1)
        out[t, m] = X[m] @ wt
    return pd.DataFrame(out, index=L.DATES, columns=L.SYMS)


def trial(name: str, scores: pd.DataFrame, cfg_model: str) -> None:
    ic = L.composite_ic(scores)
    for book, n in (("shortspy", 20), ("shortspy", 30), ("ls", 20)):
        bt = run_guarded(scores, book, n)
        L.log_trial(f"R4_{name}_{book}_n{n}", {"model": cfg_model, "book": book, "n": n, "rebal": 5, "smooth": 5,
                                               "fix": "carry_forward+guard"}, bt, ic)


def round4() -> None:
    m = tradable()
    s = rescore_carry_forward("E8_tradable_lasso", L.V3_FACTORS, m)
    s.to_parquet(L.LAB / "scores_R4_E8cf.parquet"); trial("E8cf", s, "lasso_tradable_v3factors_cf")
    s = rescore_carry_forward("E11_tradable_all", L.ALL_FACTORS, m)
    s.to_parquet(L.LAB / "scores_R4_E11cf.parquet"); trial("E11cf", s, "lasso_tradable_allfactors_cf")
    print("ICIR weights in the tradable universe...", flush=True)
    s = icir_scores(L.ALL_FACTORS, m)
    s.to_parquet(L.LAB / "scores_R4_icir.parquet"); trial("ICIR", s, "icir_tradable_allfactors")
    print("Ridge in the tradable universe...", flush=True)
    s, co = L.fit_scores(L.ALL_FACTORS, "ridge", universe="tradable")
    s.to_parquet(L.LAB / "scores_R4_ridge.parquet"); co.to_csv(L.LAB / "coefs_R4_ridge.csv")
    trial("RIDGE", s, "ridge_tradable_allfactors")


def round5() -> None:
    """Same model as E11 (LASSO, all factors, tradable universe) with a 504-day training
    window: more data per fit -> fewer all-zero fits and steadier coefficients."""
    L.TRAIN_DAYS = 504
    s, co = L.fit_scores(L.ALL_FACTORS, "lasso", universe="tradable")
    co.to_csv(L.LAB / "coefs_R5_E11w504.csv")
    nz = (co.abs() > 1e-9)
    print(f"all-zero retrains: {int((nz.sum(1) == 0).sum())}/{len(co)}; median factors used {int(nz.sum(1).median())}",
          flush=True)
    s = rescore_carry_forward("R5_E11w504", L.ALL_FACTORS, tradable())
    s.to_parquet(L.LAB / "scores_R5_E11w504.parquet")
    trial("E11w504", s, "lasso_tradable_allfactors_cf_win504")


NEW_FACTORS = ["bm", "ep", "sp", "gpa", "roe", "ag", "accr", "nsi", "ear"]


def round6() -> None:
    """Pre-registered (ROUND6_PREREG.md): the round-4 pick plus 9 new-information factors."""
    import json
    names = json.loads((L.CACHE / "factors.json").read_text())
    assert all(k in names for k in NEW_FACTORS), "run lab_factors2.py first"
    # single-factor IC of the new factors on the selection window (reporting only)
    m = pd.DataFrame(tradable(), index=L.DATES, columns=L.SYMS)
    sel = (L.DATES >= L.BT_START) & (L.DATES <= L.VAL_END)
    f5 = L.df("fwd5").where(m).loc[sel]
    for k in NEW_FACTORS:
        f = pd.DataFrame(np.asarray(L.factor(k)), index=L.DATES, columns=L.SYMS).loc[sel]
        ic = L.v1.rank_ic(f, f5).dropna()
        print(f"new factor {k:5s} tradable IC5 {ic.mean():+.4f}  NW t {L.v1.nw_tstat(ic, 10):+.2f}", flush=True)
    L.TRAIN_DAYS = 252
    _, co = L.fit_scores(names, "lasso", universe="tradable")
    co.to_csv(L.LAB / "coefs_R6.csv")
    nz = (co.abs() > 1e-9)
    print(f"all-zero retrains {int((nz.sum(1) == 0).sum())}/{len(co)}; most used: "
          f"{nz.mean().sort_values(ascending=False).head(10).round(2).to_dict()}", flush=True)
    s = rescore_carry_forward("R6", names, tradable())
    s.to_parquet(L.LAB / "scores_R6.parquet")
    ic = L.composite_ic(s)
    for book in ("shortspy", "ls"):
        bt = run_guarded(s, book, 20)
        L.log_trial(f"R6_{book}_n20", {"model": "lasso_tradable_65factors_cf", "book": book, "n": 20,
                                        "rebal": 5, "smooth": 5, "fix": "carry_forward+guard"}, bt, ic)


PRIOR_SET = ["bm", "ep", "sp", "mom12_1", "ivol60", "roe", "gpa", "ag", "accr", "nsi", "ear"]


def round7() -> None:
    """Pre-registered (ROUND7_PREREG.md): no-estimation literature-prior composite."""
    m = tradable()
    acc, cnt = None, None
    for k in PRIOR_SET:
        f = np.asarray(L.factor(k), dtype=np.float32)
        v = np.where(np.isfinite(f), f, 0.0)
        acc = v if acc is None else acc + v
    s = np.where(m, acc / len(PRIOR_SET), np.nan).astype(np.float32)
    bt0 = int(np.searchsorted(L.DATES, L.BT_START))
    s[:bt0] = np.nan
    scores = pd.DataFrame(s, index=L.DATES, columns=L.SYMS)
    scores.to_parquet(L.LAB / "scores_R7.parquet")
    ic = L.composite_ic(scores)
    print(f"composite {ic}", flush=True)
    for book in ("shortspy", "ls"):
        bt = run_guarded(scores, book, 20)
        L.log_trial(f"R7_{book}_n20", {"model": "literature_prior_equal_weight_11", "book": book, "n": 20,
                                        "rebal": 5, "smooth": 5, "fix": "no_estimation+guard"}, bt, ic)


def round8() -> None:
    """Pre-registered (ROUND8_PREREG.md): round-7 composite, broad 100-name books."""
    scores = pd.read_parquet(L.LAB / "scores_R7.parquet")
    ic = L.composite_ic(scores)
    for book in ("ls", "shortspy"):
        bt = run_guarded(scores, book, 100)
        L.log_trial(f"R8_{book}_n100", {"model": "literature_prior_equal_weight_11", "book": book, "n": 100,
                                         "rebal": 5, "smooth": 5, "fix": "no_estimation+guard"}, bt, ic)


if __name__ == "__main__":
    if sys.argv[1] == "round8":
        round8()
    if sys.argv[1] == "round7":
        round7()
    if sys.argv[1] == "round4":
        round4()
    elif sys.argv[1] == "round5":
        round5()
    elif sys.argv[1] == "round6":
        round6()
    print("done", flush=True)
