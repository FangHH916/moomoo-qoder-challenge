"""Assemble wide daily panels from downloaded data.

Conventions
  * Adjusted (split+dividend) prices -> returns and price-shape factors.
  * Raw prices/volume x SEC shares outstanding -> market cap, turnover and the
    competition's $1 price / $500M market-cap eligibility test.
  * SEC shares are used only from period_end + 60 days (filing lag, no look-ahead).
  * fwd_ret[t] = open[t+2] / open[t+1] - 1 : signal after close t, trade at open t+1.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path(__file__).resolve().parents[1] / "data"
SHARES_LAG_DAYS = 60
MIN_PRICE, MIN_MCAP, MIN_DOLLAR_VOL = 1.0, 5e8, 5e6


def _wide(df: pd.DataFrame, field: str) -> pd.DataFrame:
    return df.pivot(index="date", columns="symbol", values=field).sort_index()


def build_panel() -> dict:
    adj = pd.read_parquet(DATA / "bars_daily.parquet")
    raw = pd.read_parquet(DATA / "bars_daily_raw.parquet")
    # drop today's bar if the US session has not closed yet (partial OHLCV)
    now_et = pd.Timestamp.now(tz="America/New_York")
    if now_et.hour < 17:
        today = now_et.normalize().tz_localize(None)
        adj, raw = adj[adj["date"] < today], raw[raw["date"] < today]
    meta = pd.read_parquet(DATA / "universe.parquet")[["symbol", "cik", "industry"]]

    p = {f: _wide(adj, f) for f in ["open", "high", "low", "close", "volume", "vwap", "trades"]}
    cols, idx = p["close"].columns, p["close"].index
    # bad prints: an open that is >50% away from BOTH the prior close and the same-day close
    o, c = p["open"], p["close"]
    bad_open = ((o / c.shift(1) - 1).abs() > 0.5) & ((o / c - 1).abs() > 0.5)
    p["open"] = o.mask(bad_open)
    print("bad opens removed:", int(bad_open.sum().sum()), flush=True)
    raw_close = _wide(raw, "close").reindex(index=idx, columns=cols)
    raw_vol = _wide(raw, "volume").reindex(index=idx, columns=cols)
    raw_vwap = _wide(raw, "vwap").reindex(index=idx, columns=cols)

    # shares outstanding step function (available 60 days after period end)
    sh = pd.read_parquet(DATA / "shares_q.parquet")
    sh = sh.merge(meta[["symbol", "cik"]], on="cik")
    sh["avail"] = sh["period_end"] + pd.Timedelta(days=SHARES_LAG_DAYS)
    sh = sh.sort_values("avail").drop_duplicates(["symbol", "avail"], keep="last")
    sw = sh.pivot(index="avail", columns="symbol", values="shares")
    sw = sw.reindex(sw.index.union(idx)).sort_index().ffill().reindex(idx)
    shares = sw.reindex(columns=cols)

    p["amt"] = raw_vwap * raw_vol                      # dollar volume
    p["mcap"] = raw_close * shares
    p["tvr"] = raw_vol / shares
    p["raw_close"] = raw_close

    ind = meta.drop_duplicates("symbol").set_index("symbol")["industry"].reindex(cols)
    etfs = set(cols) - set(meta["symbol"])
    dvol20 = p["amt"].rolling(20, min_periods=15).median()
    U = ((raw_close >= MIN_PRICE) & (p["mcap"] >= MIN_MCAP) & (dvol20 >= MIN_DOLLAR_VOL)
         & p["close"].notna() & shares.notna())
    U.loc[:, list(etfs & set(U.columns))] = False       # ETFs excluded from the scoring pool
    U.loc[:, ind[ind.isna()].index] = False

    o = p["open"]
    p["fwd_ret"] = o.shift(-2) / o.shift(-1) - 1.0

    # memory: keep only names that are ever eligible (plus ETFs), store as float32
    keep = U.columns[U.any(axis=0)].union(pd.Index(sorted(etfs & set(U.columns))))
    p = {k: v[keep].astype("float32") for k, v in p.items()}
    U = U[keep]
    ind = ind.reindex(keep)
    return {"p": p, "U": U, "industry": ind, "etfs": sorted(etfs)}
