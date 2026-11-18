"""Download split/dividend-adjusted daily bars (SIP) from Alpaca.

Symbols: data/universe.parquet (operating companies) + hedge/benchmark ETFs.
Output: data/bars/<batch>.parquet, then merged into data/bars_daily.parquet
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

from alpaca_client import DATA_URL, get

DATA = Path(__file__).resolve().parents[1] / "data"
# usage: python download_bars.py [all|raw]   (all = split+dividend adjusted, raw = unadjusted)
ADJ = sys.argv[1] if len(sys.argv) > 1 else "all"
OUT = DATA / ("bars" if ADJ == "all" else f"bars_{ADJ}")
OUT.mkdir(parents=True, exist_ok=True)
MERGED = DATA / ("bars_daily.parquet" if ADJ == "all" else f"bars_daily_{ADJ}.parquet")
START = "2019-10-01"
END = None  # up to latest available
BATCH = 100
ETFS = ["SPY", "QQQ", "IWM", "DIA", "TLT", "IEF", "SHY", "GLD", "SLV", "USO", "UUP",
        "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC", "SMH"]


def fetch_batch(symbols: list[str]) -> pd.DataFrame:
    params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": START,
              "adjustment": ADJ, "feed": "sip", "limit": 10000, "sort": "asc"}
    if END:
        params["end"] = END
    rows = []
    while True:
        r = get(f"{DATA_URL}/v2/stocks/bars", params)
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


def main() -> None:
    uni = pd.read_parquet(DATA / "universe.parquet")
    symbols = sorted(set(uni["symbol"]) | set(ETFS))
    batches = [symbols[i:i + BATCH] for i in range(0, len(symbols), BATCH)]
    print(f"{len(symbols)} symbols, {len(batches)} batches")
    for i, b in enumerate(batches):
        f = OUT / f"batch_{i:04d}.parquet"
        if f.exists():
            continue
        df = fetch_batch(b)
        df.to_parquet(f)
        print(f"batch {i + 1}/{len(batches)} rows={len(df)}", flush=True)
    allb = pd.concat([pd.read_parquet(f) for f in sorted(OUT.glob("batch_*.parquet"))], ignore_index=True)
    allb = allb.drop_duplicates(["symbol", "date"]).sort_values(["symbol", "date"])
    allb.to_parquet(MERGED)
    print("total rows:", len(allb), "symbols:", allb["symbol"].nunique(),
          "dates:", allb["date"].min().date(), "->", allb["date"].max().date())


if __name__ == "__main__":
    sys.exit(main())
