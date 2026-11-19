"""Extended price/volume factor library (US-documented anomalies).

Every factor at date t uses data up to t's close. Signs follow the literature's
"higher = better expected return" where obvious; the combination model can flip
signs anyway, so the sign only matters for single-factor reporting.

References (short): Jegadeesh 1990 (reversal), Jegadeesh-Titman 1993 (momentum),
Novy-Marx 2012 (intermediate momentum), Moskowitz-Grinblatt 1999 (industry
momentum), Blitz-Huij-Martens 2011 (residual momentum), Da-Gurun-Warachka 2014
(frog-in-the-pan), Lou-Polk-Skouras 2019 (overnight vs intraday), Bali-Cakici-
Whitelaw 2011 (MAX), Ang et al. 2006 (idio vol), Frazzini-Pedersen 2014 (BAB),
Amihud 2002, Gervais-Kaniel-Mingelgrin 2001 (volume shock), Heston-Sadka 2008
(seasonality), George-Hwang 2004 (52w high), Da-Liu-Schaumburg 2014 (residual
reversal).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

EPS = 1e-12


def _ind_mean(x: pd.DataFrame, ind: pd.Series) -> pd.DataFrame:
    """Cross-sectional industry mean, broadcast back to each stock."""
    out = pd.DataFrame(np.nan, index=x.index, columns=x.columns)
    for g, cols in ind.dropna().groupby(ind.dropna()).groups.items():
        cols = [c for c in cols if c in x.columns]
        if cols:
            m = x[cols].mean(axis=1)
            out[cols] = np.repeat(m.to_numpy()[:, None], len(cols), axis=1)
    return out


def library(p: dict[str, pd.DataFrame], ind: pd.Series, spy_ret: pd.Series, beta: pd.DataFrame) -> dict:
    o, h, l, c, v = p["open"], p["high"], p["low"], p["close"], p["volume"]
    amt, tvr, ntr = p["amt"], p["tvr"], p["trades"]
    r = c.pct_change()
    lr = np.log(c).diff()
    m = spy_ret.reindex(c.index)
    resid = r - beta.mul(m, axis=0)
    on = np.log(o / c.shift(1))                 # overnight log return
    intra = np.log(c / o)                       # intraday log return
    f = {}
    # --- momentum / trend
    f["mom1"] = c / c.shift(21) - 1
    f["mom6_1"] = c.shift(21) / c.shift(126) - 1
    f["mom12_7"] = c.shift(126) / c.shift(252) - 1
    ind_mom = _ind_mean(f["mom6_1"], ind)
    f["ind_mom6"] = ind_mom
    f["mom6_ind_adj"] = f["mom6_1"] - ind_mom
    pret = lr.rolling(252, min_periods=200).sum().shift(21)
    pos = (lr > 0).rolling(231, min_periods=180).mean().shift(21)
    neg = (lr < 0).rolling(231, min_periods=180).mean().shift(21)
    f["frog_id"] = -np.sign(pret) * (neg - pos)            # continuous info -> stronger momentum
    f["ma50_gap"] = c / c.rolling(50, min_periods=40).mean() - 1
    f["ma200_gap"] = c / c.rolling(200, min_periods=150).mean() - 1
    f["low52_gap"] = c / c.rolling(252, min_periods=200).min() - 1
    # --- reversal
    f["rev5"] = -(c / c.shift(5) - 1)
    f["rev21_ind_adj"] = -((c / c.shift(21) - 1) - _ind_mean(c / c.shift(21) - 1, ind))
    f["resid_rev21"] = -resid.rolling(21, min_periods=15).sum()
    # --- overnight vs intraday (Lou-Polk-Skouras)
    f["overnight_mom21"] = on.rolling(21, min_periods=15).sum()
    f["intraday_rev21"] = -intra.rolling(21, min_periods=15).sum()
    f["overnight_mom63"] = on.rolling(63, min_periods=45).sum()
    # --- volatility / lottery / beta
    f["vol21"] = -r.rolling(21, min_periods=15).std()
    f["downvol63"] = -r.where(r < 0).rolling(63, min_periods=15).std()   # >= 15 down days in the window
    f["park_vol21"] = -np.sqrt((np.log(h / l) ** 2).rolling(21, min_periods=15).mean() / (4 * np.log(2)))
    f["ivol21"] = -resid.rolling(21, min_periods=15).std()
    # MAX(5) proxy: mean of the max and the 80th percentile of 21 daily returns (fast, vectorised)
    f["max5"] = -(r.rolling(21, min_periods=15).max() + r.rolling(21, min_periods=15).quantile(0.8)) / 2
    f["min21"] = r.rolling(21, min_periods=15).min()
    f["bab"] = -beta
    f["skew63"] = -r.rolling(63, min_periods=45).skew()
    # --- liquidity / turnover / volume
    f["turn21"] = -tvr.rolling(21, min_periods=15).mean()
    f["abn_turn"] = -(tvr.rolling(21, min_periods=15).mean() / (tvr.rolling(252, min_periods=150).mean() + EPS))
    f["dvol_log"] = -np.log(amt.rolling(21, min_periods=15).mean() + 1)
    vshock = v / (v.rolling(63, min_periods=40).mean() + EPS)
    f["vol_shock5"] = vshock.rolling(5, min_periods=3).mean()            # high-volume return premium
    f["signed_vol5"] = (np.sign(r) * vshock).rolling(5, min_periods=3).mean()
    f["trade_size_chg"] = np.log((amt / ntr.replace(0, np.nan)).rolling(21, min_periods=15).mean()
                                 / (amt / ntr.replace(0, np.nan)).rolling(126, min_periods=90).mean())
    f["pv_corr21"] = -r.rolling(21, min_periods=15).corr(np.log(v + 1).diff())
    # --- price location
    rng = (h - l).replace(0, np.nan)
    f["clv21"] = ((2 * c - h - l) / rng).rolling(21, min_periods=15).mean()
    f["log_price"] = np.log(c)
    # --- seasonality: mean return of the same calendar month in prior years
    mret = c.resample("ME").last().pct_change()
    seas = pd.DataFrame(np.nan, index=mret.index, columns=c.columns)
    for i, d in enumerate(mret.index):
        prior = mret.iloc[:i]
        same = prior[prior.index.month == d.month]
        if len(same):
            seas.iloc[i] = same.mean().to_numpy()
    # value available for the month being traded: use next month's seasonal mean (known in advance)
    seas_next = seas.shift(-1)
    f["season_month"] = seas_next.reindex(c.index, method="ffill")
    return f
