"""Download SEC XBRL 'frames' (all filers per concept per period) for a small set of
fundamentals, plus 8-K Item 2.02 (earnings release) dates per company.

Outputs (data/):
  fund_frames.parquet   concept, cik, end, val, period ('annual' | 'q_instant')
  earnings_dates.parquet cik, date  (8-K filings whose items include 2.02)
usage: python fundamentals.py frames | events
"""
from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import requests

DATA = Path(__file__).resolve().parents[1] / "data"
HDR = {"User-Agent": "factor-research-script research@example.com"}

ANNUAL = ["NetIncomeLoss", "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "GrossProfit",
          "OperatingIncomeLoss", "NetCashProvidedByUsedInOperatingActivities"]
INSTANT = ["Assets", "StockholdersEquity"]

_lock, _last = threading.Lock(), [0.0]


def get(url: str) -> requests.Response | None:
    for attempt in range(5):
        with _lock:                                   # <= ~8 requests/s overall (SEC fair access)
            wait = _last[0] + 0.125 - time.time()
            if wait > 0:
                time.sleep(wait)
            _last[0] = time.time()
        try:
            r = requests.get(url, headers=HDR, timeout=60)
        except requests.RequestException:
            time.sleep(2 + 2 ** attempt)
            continue
        if r.status_code in (429, 503):
            time.sleep(2 + 2 ** attempt)
            continue
        return r
    return None


def frames() -> None:
    jobs = [(c, f"CY{y}", "annual") for c in ANNUAL for y in range(2017, 2026)]
    jobs += [(c, f"CY{y}Q{q}I", "q_instant") for c in INSTANT for y in range(2018, 2027) for q in range(1, 5)]
    rows = []
    for i, (concept, per, kind) in enumerate(jobs, 1):
        r = get(f"https://data.sec.gov/api/xbrl/frames/us-gaap/{concept}/USD/{per}.json")
        if r is not None and r.status_code == 200:
            d = pd.DataFrame(r.json()["data"])[["cik", "end", "val"]]
            d["concept"], d["period"], d["frame"] = concept, kind, per
            rows.append(d)
        print(f"frames {i}/{len(jobs)} {concept} {per} -> {0 if r is None else r.status_code}", flush=True)
    out = pd.concat(rows, ignore_index=True)
    out["end"] = pd.to_datetime(out["end"])
    out.to_parquet(DATA / "fund_frames.parquet")
    print("rows", len(out), "concepts", out["concept"].nunique(), flush=True)


def events() -> None:
    uni = pd.read_parquet(DATA / "universe.parquet")
    ciks = sorted(set(uni["cik"].astype(int)))
    out_rows: list[dict] = []

    def one(cik: int) -> list[dict]:
        r = get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
        if r is None or r.status_code != 200:
            return []
        rec = r.json().get("filings", {}).get("recent", {})
        res = []
        for form, date, items in zip(rec.get("form", []), rec.get("filingDate", []), rec.get("items", [])):
            if form in ("8-K", "8-K/A") and "2.02" in (items or ""):
                res.append({"cik": cik, "date": date})
        return res

    with ThreadPoolExecutor(max_workers=8) as ex:
        for i, rows in enumerate(ex.map(one, ciks), 1):
            out_rows.extend(rows)
            if i % 250 == 0 or i == len(ciks):
                print(f"events {i}/{len(ciks)} rows={len(out_rows)}", flush=True)
    d = pd.DataFrame(out_rows)
    d["date"] = pd.to_datetime(d["date"])
    d.drop_duplicates().to_parquet(DATA / "earnings_dates.parquet")
    print("earnings events", len(d), "companies", d["cik"].nunique(), flush=True)


if __name__ == "__main__":
    {"frames": frames, "events": events}[sys.argv[1]]()
