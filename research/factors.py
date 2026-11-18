"""Daily factors ported from 因子日历.docx to US daily data.

All inputs are wide DataFrames (index=date, columns=symbol). Every factor at
date t uses data up to and including t's close only.

US data gaps vs. A-share originals:
  - DC1-DC4 use Level-2 order-size money flow, not available for US stocks.
    *_approx versions use Alpaca trade counts (avg dollar per trade) as a
    "big-order intensity" proxy and the close-location value as a buy/sell
    pressure proxy. They are approximations, flagged as such in reports.
  - MD1 keeps only the opening-gap z-score part (no shareholder-count data).
  - MD2 (5-minute data) and PV2 (no definition in the doc) are skipped.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view
from scipy import stats

EPS = 1e-8


def _roll(a: np.ndarray, w: int) -> np.ndarray:
    """(T, N) -> (T, N, w) trailing windows; first w-1 rows are NaN-padded."""
    T, N = a.shape
    pad = np.full((w - 1, N), np.nan)
    return sliding_window_view(np.vstack([pad, a]), w, axis=0)


def _df(x: np.ndarray, like: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(x, index=like.index, columns=like.columns)


def _decay_sum(x: pd.DataFrame, window: int, rate: float) -> pd.DataFrame:
    w = rate ** np.arange(window)[::-1]  # oldest gets smallest weight
    win = _roll(x.to_numpy(float), window)
    return _df(np.einsum("tnw,w->tn", np.nan_to_num(win), w), x)


def _clv(h, l, c):
    rng = (h - l).replace(0, np.nan)
    return ((2 * c - h - l) / rng).fillna(0.0)  # close location value in [-1, 1]


# ---------------------------------------------------------------- PV family
def pv1(close, tvr, lookback=20, cutoff=0.6):
    k = max(2, int(lookback * cutoff))
    P, V = _roll(close.to_numpy(float), lookback), _roll(tvr.to_numpy(float), lookback)
    idx = np.argsort(-np.nan_to_num(P, nan=-np.inf), axis=2)[..., :k]
    Pt, Vt = np.take_along_axis(P, idx, 2), np.take_along_axis(V, idx, 2)
    dp, dv = Pt - Pt.mean(2, keepdims=True), Vt - Vt.mean(2, keepdims=True)
    with np.errstate(all="ignore"):
        rho = (dp * dv).sum(2) / np.sqrt((dp ** 2).sum(2) * (dv ** 2).sum(2))
    return _df(-(rho ** 2) * np.sign(rho), close)


def pv3(vwap, window=60, kmax=15):
    X = _roll(vwap.to_numpy(float), window)  # (T,N,w)
    N = window
    Lk = []
    for k in range(1, kmax + 1):
        tot = 0.0
        for m in range(k):
            sub = X[..., m::k]
            if sub.shape[-1] < 2:
                continue
            n_int = (N - m - 1) // k
            tot = tot + np.nansum(np.abs(np.diff(sub, axis=-1)), -1) * (N - 1) / (n_int * k)
        Lk.append(tot / k)
    logL = np.log(np.stack(Lk, -1) + EPS)
    logk = np.log(np.arange(1, kmax + 1))
    dx = logk - logk.mean()
    slope = ((logL - logL.mean(-1, keepdims=True)) * dx).sum(-1) / (dx ** 2).sum()
    out = -slope
    out[np.isnan(X).any(-1)] = np.nan
    return _df(out, vwap)


def pv4(open_, close, amt, window=21):
    down = np.maximum(-(close / open_ - 1.0), 0.0)
    ratio = down.rolling(window).sum() / ((amt * 1e-6).rolling(window).sum() + EPS)
    return np.log1p(ratio)


def pv5(close, tvr, window=10):
    return -close.rolling(window).corr(tvr).clip(-1, 1)


def pv6(close, low, window=5):
    return -np.log(close / (low + EPS)).rolling(window).mean()


def pv7(tvr, window=20):
    return -(tvr.rolling(window).max() - tvr.rolling(window).min())


def pv8(close, tvr, window=140):
    r = close.pct_change()
    up, dn = (r > 0).astype(float), (r < 0).astype(float)
    avg_up = (tvr * up).rolling(window).sum() / (up.rolling(window).sum() + EPS)
    avg_dn = (tvr * dn).rolling(window).sum() / (dn.rolling(window).sum() + EPS)
    return -(avg_up - avg_dn)


def pv9(close, window=100):
    r = close.pct_change()
    return -r.rolling(window).skew()  # pandas rolling skew is the bias-corrected sample skew


def pv10(tvr, window=20):
    return -tvr.rolling(window).std()


def pv11(tvr, window=20):
    return -tvr.rolling(window).std() / (tvr.rolling(window).mean() + EPS)


def pv12(close, amt, window=20):
    a = close.pct_change().abs()
    cv_r = a.rolling(window).std() / (a.rolling(window).mean() + EPS)
    cv_a = amt.rolling(window).std() / (amt.rolling(window).mean() + EPS)
    return cv_r / (cv_a + EPS)


# ---------------------------------------------------------------- MD1 (gap part)
def md1_gap(open_, close, window=40):
    gap = open_ / close.shift(1) - 1.0
    z = gap.sub(gap.mean(1), axis=0).div(gap.std(1) + 1e-9, axis=0)
    return z.rolling(window, min_periods=window // 2).min()


# ---------------------------------------------------------------- DC approximations
def _ticket(amt, trades):
    """Average dollar value per trade (proxy for big-order participation)."""
    return amt / trades.replace(0, np.nan)


def dc1_approx(high, low, close, amt, trades, mcap, flow_window=20, vol_window=5,
               decay=0.85, penalty=2.0):
    tk = _ticket(amt, trades)
    intensity = (tk / tk.rolling(60, min_periods=20).median()).clip(0, 5)
    flow = _clv(high, low, close) * amt * intensity.fillna(1.0)
    flow = flow.where(flow >= 0, flow * penalty)
    wsum = _decay_sum(flow, flow_window, decay)
    vol = close.pct_change().rolling(vol_window).std()
    vol = vol.where(vol >= 1e-6, 0.02)
    return -(wsum / ((mcap + EPS) * vol)).replace([np.inf, -np.inf], np.nan)


def dc2_approx(close, amt, trades, mcap, window=20, decay=0.9):
    score = amt * np.log1p(_ticket(amt, trades).fillna(0))
    wsum = _decay_sum(score, window, decay)
    vol = close.pct_change().rolling(5).std()
    vol = vol.where(vol >= 1e-6, 0.02)
    return -(wsum / ((mcap + EPS) * vol)).replace([np.inf, -np.inf], np.nan)


def dc3_approx(high, low, close, amt, trades, window=20, decay=0.9, smooth=5):
    tk = _ticket(amt, trades)
    big_share = (tk / tk.rolling(60, min_periods=20).median() - 1.0).clip(-1, 4).fillna(0)
    smart_ratio = _clv(high, low, close) * big_share / 4.0
    s = _decay_sum(smart_ratio, window, decay)
    return s.rolling(smooth).mean().clip(-0.5, 0.5)


def dc4_approx(close, amt, trades, window=20, k=4):
    small_ratio = -np.log(_ticket(amt, trades))  # higher = smaller tickets
    R = _roll(close.pct_change().to_numpy(float), window)
    S = _roll(small_ratio.to_numpy(float), window)
    idx = np.argsort(np.nan_to_num(S, nan=np.inf), axis=2)
    Rs = np.take_along_axis(R, idx, 2)
    with np.errstate(all="ignore"):
        a = np.nanmean(Rs[..., :k], 2) - np.nanmean(Rs[..., -k:], 2)
    return _df(-a, close)


# ---------------------------------------------------------------- US-documented classics
def us_classics(close: pd.DataFrame, amt: pd.DataFrame, mkt_ret: pd.Series) -> dict[str, pd.DataFrame]:
    """Factors with long US evidence: short-term reversal (Jegadeesh 1990),
    12-1 momentum (Jegadeesh-Titman), 52-week-high proximity (George-Hwang),
    Amihud illiquidity, idiosyncratic volatility (Ang et al. 2006)."""
    r = close.pct_change()
    m = mkt_ret.reindex(close.index)
    w = 60
    mr = r.rolling(w, min_periods=40)
    m_mean = m.rolling(w, min_periods=40).mean()
    m_var = m.rolling(w, min_periods=40).var()
    cov = r.mul(m, axis=0).rolling(w, min_periods=40).mean() - mr.mean().mul(m_mean, axis=0)
    beta = cov.div(m_var, axis=0)
    resid_var = (mr.var() - beta.pow(2).mul(m_var, axis=0)).clip(lower=0)
    return {
        "rev21": -(close / close.shift(21) - 1.0),
        "mom12_1": close.shift(21) / close.shift(252) - 1.0,
        "high52": close / close.rolling(252, min_periods=200).max(),
        "amihud": np.log1p((r.abs() / (amt + EPS) * 1e9).rolling(20, min_periods=15).mean()),
        "ivol60": -np.sqrt(resid_var),
    }


def market_beta(close: pd.DataFrame, mkt_ret: pd.Series, window: int = 60) -> pd.DataFrame:
    r = close.pct_change()
    m = mkt_ret.reindex(close.index)
    cov = r.mul(m, axis=0).rolling(window, min_periods=40).mean() - r.rolling(window, min_periods=40).mean().mul(
        m.rolling(window, min_periods=40).mean(), axis=0)
    return cov.div(m.rolling(window, min_periods=40).var(), axis=0)


FACTOR_NAMES = ["pv1", "pv3", "pv4", "pv5", "pv6", "pv7", "pv8", "pv9", "pv10", "pv11", "pv12",
                "md1_gap", "dc1_approx", "dc2_approx", "dc3_approx", "dc4_approx"]


def _per_stock(p: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    o, h, l, c, vw, n = (p[k] for k in ["open", "high", "low", "close", "vwap", "trades"])
    amt, tvr, mcap = p["amt"], p["tvr"], p["mcap"]
    return {
        "pv1": pv1(c, tvr), "pv3": pv3(vw), "pv4": pv4(o, c, amt), "pv5": pv5(c, tvr),
        "pv6": pv6(c, l), "pv7": pv7(tvr), "pv8": pv8(c, tvr), "pv9": pv9(c),
        "pv10": pv10(tvr), "pv11": pv11(tvr), "pv12": pv12(c, amt),
        "dc1_approx": dc1_approx(h, l, c, amt, n, mcap), "dc2_approx": dc2_approx(c, amt, n, mcap),
        "dc3_approx": dc3_approx(h, l, c, amt, n), "dc4_approx": dc4_approx(c, amt, n),
    }


def compute_all(p: dict[str, pd.DataFrame], chunk: int = 300) -> dict[str, pd.DataFrame]:
    """Time-series factors are computed in column chunks to bound memory;
    md1_gap needs the full cross-section, so it is computed once."""
    cols = p["close"].columns
    parts: dict[str, list] = {}
    for i in range(0, len(cols), chunk):
        sub = {k: v[cols[i:i + chunk]] for k, v in p.items()}
        for name, df in _per_stock(sub).items():
            parts.setdefault(name, []).append(df)
    out = {name: pd.concat(dfs, axis=1) for name, dfs in parts.items()}
    out["md1_gap"] = md1_gap(p["open"], p["close"])
    if "SPY" in p["close"].columns:
        out.update(us_classics(p["close"], p["amt"], p["close"]["SPY"].pct_change()))
    return out
