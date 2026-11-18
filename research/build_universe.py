"""Build the stock universe with industry (SIC -> Fama-French 12) and
quarterly shares-outstanding history from SEC public data.

Outputs (data/):
  assets.parquet        Alpaca tradable US equities on major exchanges
  sec_meta.parquet      ticker, cik, sic, industry
  shares_q.parquet      cik, period_end, shares_outstanding (quarterly frames)
  universe.parquet      assets joined with SEC meta (operating companies only)
"""
from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import requests

from alpaca_client import trading_get

DATA = Path(__file__).resolve().parents[1] / "data"
DATA.mkdir(exist_ok=True)
SEC_HEADERS = {"User-Agent": "factor-research-script research@example.com"}
MAJOR_EXCH = {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS"}


def sec_get(url: str) -> requests.Response:
    for attempt in range(5):
        r = requests.get(url, headers=SEC_HEADERS, timeout=60)
        if r.status_code in (429, 503):
            time.sleep(2 + 2 ** attempt)
            continue
        time.sleep(0.12)  # SEC fair-access: <= 10 req/s
        return r
    return r


# Fama-French 12 industries from 4-digit SIC (standard definition, abridged ranges)
FF12 = [
    ("NoDur", [(100, 999), (2000, 2399), (2700, 2749), (2770, 2799), (3100, 3199), (3940, 3989)]),
    ("Durbl", [(2500, 2519), (2590, 2599), (3630, 3659), (3710, 3711), (3714, 3714), (3716, 3716),
               (3750, 3751), (3792, 3792), (3900, 3939), (3990, 3999)]),
    ("Manuf", [(2520, 2589), (2600, 2699), (2750, 2769), (3000, 3099), (3200, 3569), (3580, 3629),
               (3700, 3709), (3712, 3713), (3715, 3715), (3717, 3749), (3752, 3791), (3793, 3799),
               (3830, 3839), (3860, 3899)]),
    ("Enrgy", [(1200, 1399), (2900, 2999)]),
    ("Chems", [(2800, 2829), (2840, 2899)]),
    ("BusEq", [(3570, 3579), (3660, 3692), (3694, 3699), (3810, 3829), (7370, 7379)]),
    ("Telcm", [(4800, 4899)]),
    ("Utils", [(4900, 4949)]),
    ("Shops", [(5000, 5999), (7200, 7299), (7600, 7699)]),
    ("Hlth", [(2830, 2839), (3693, 3693), (3840, 3859), (8000, 8099)]),
    ("Money", [(6000, 6999)]),
]


def sic_to_ff12(sic: int | None) -> str | None:
    if sic is None or pd.isna(sic):
        return None
    sic = int(sic)
    for name, ranges in FF12:
        if any(lo <= sic <= hi for lo, hi in ranges):
            return name
    return "Other"


def main() -> None:
    # 1. Alpaca assets
    r = trading_get("/v2/assets", {"status": "active", "asset_class": "us_equity"})
    r.raise_for_status()
    assets = pd.DataFrame(r.json())
    assets = assets[assets["tradable"] & assets["exchange"].isin(MAJOR_EXCH)]
    assets = assets[["symbol", "name", "exchange", "shortable", "easy_to_borrow", "fractionable"]]
    assets.to_parquet(DATA / "assets.parquet")
    print("alpaca tradable on major exchanges:", len(assets))

    # 2. SEC ticker -> CIK
    j = sec_get("https://www.sec.gov/files/company_tickers_exchange.json").json()
    tick = pd.DataFrame(j["data"], columns=j["fields"])
    tick["ticker"] = tick["ticker"].str.upper().str.replace("-", ".", regex=False)
    tick = tick.drop_duplicates("ticker")
    m = assets.merge(tick[["ticker", "cik"]], left_on="symbol", right_on="ticker", how="inner")
    print("matched to SEC CIK:", len(m))

    # 3. SIC per CIK (submissions API, one call per company, cached)
    cache = DATA / "sic_cache.parquet"
    sic = pd.read_parquet(cache) if cache.exists() else pd.DataFrame(columns=["cik", "sic"])
    todo = sorted(set(m["cik"]) - set(sic["cik"]))
    print("sic to fetch:", len(todo), flush=True)

    import threading
    from concurrent.futures import ThreadPoolExecutor
    lock, last = threading.Lock(), [0.0]

    def fetch_sic(cik):
        with lock:                                   # global pacing: <= ~8 req/s
            wait = last[0] + 0.125 - time.time()
            if wait > 0:
                time.sleep(wait)
            last[0] = time.time()
        for attempt in range(4):
            rr = requests.get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json",
                              headers=SEC_HEADERS, timeout=30)
            if rr.status_code in (429, 503):
                time.sleep(2 + 2 ** attempt)
                continue
            break
        val = None
        if rr.status_code == 200:
            s = rr.json().get("sic")
            val = int(s) if s not in (None, "") else None
        return {"cik": cik, "sic": val}

    rows = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for i, row in enumerate(ex.map(fetch_sic, todo)):
            rows.append(row)
            if (i + 1) % 500 == 0 or i + 1 == len(todo):
                print(f"  sic {i + 1}/{len(todo)}", flush=True)
                sic = pd.concat([sic, pd.DataFrame(rows)], ignore_index=True)
                sic.to_parquet(cache)
                rows = []

    meta = m.merge(sic, on="cik", how="left")
    meta["industry"] = meta["sic"].map(sic_to_ff12)
    meta = meta.dropna(subset=["sic"])
    # drop funds / trusts / blank checks (SIC 6726 investment offices, 6770 blank checks, 6221 commodity)
    meta = meta[~meta["sic"].isin([6221, 6726, 6770])]
    meta[["symbol", "cik", "sic", "industry"]].to_parquet(DATA / "sec_meta.parquet")
    print("operating companies with SIC:", len(meta))
    print(meta["industry"].value_counts().to_string())

    # 4. Quarterly shares outstanding frames (one call per quarter for all filers)
    frames = []
    for year in range(2019, 2027):
        for q in range(1, 5):
            url = (f"https://data.sec.gov/api/xbrl/frames/dei/EntityCommonStockSharesOutstanding/"
                   f"shares/CY{year}Q{q}I.json")
            rr = sec_get(url)
            if rr.status_code != 200:
                continue
            d = pd.DataFrame(rr.json()["data"])
            d["frame"] = f"CY{year}Q{q}I"
            frames.append(d[["cik", "end", "val", "frame"]])
    shares = pd.concat(frames, ignore_index=True).rename(columns={"end": "period_end", "val": "shares"})
    shares["period_end"] = pd.to_datetime(shares["period_end"])
    shares.to_parquet(DATA / "shares_q.parquet")
    print("shares frames rows:", len(shares), "quarters:", shares["frame"].nunique())

    meta.to_parquet(DATA / "universe.parquet")


if __name__ == "__main__":
    main()
