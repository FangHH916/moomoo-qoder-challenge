"""Build the research cache once: every candidate factor, cleaned and
industry+size neutralised, saved as float32 .npy (memory-mappable), plus the
targets and helpers the experiments need.

Run: python lab_cache.py      (writes data/cache/*)
"""
from __future__ import annotations

import gc
import json
from pathlib import Path

import numpy as np
import pandas as pd

import pipeline as v1
from factors import compute_all, market_beta
from lab_factors import library
from panel import DATA, build_panel
from pipeline_v3 import extra_factors, residual_target, H

import os  # noqa: E402

# QR_CACHE_DIR lets the live job build its own cache (data/cache_live) without touching the research cache
CACHE = Path(os.environ.get("QR_CACHE_DIR") or DATA / "cache")
CACHE.mkdir(exist_ok=True)
BATCH = 8


def save_batch(raw: dict, U, ind, mcap, done: list) -> None:
    names = list(raw)
    for i in range(0, len(names), BATCH):
        part = {k: raw[k] for k in names[i:i + BATCH]}
        nf = v1.preprocess(part, U, ind, mcap)
        for k, df in nf.items():
            np.save(CACHE / f"f_{k}.npy", df.to_numpy(np.float32))
            done.append(k)
        print(f"saved {len(done)} factors", flush=True)
        del nf, part; gc.collect()


def main() -> None:
    P = build_panel()
    p, U, ind = P["p"], P["U"], P["industry"]
    spy = p["close"]["SPY"].pct_change()
    beta = market_beta(p["close"], spy)
    mcap = p["mcap"]
    o = p["open"].astype(float)
    fwd1 = p["fwd_ret"].astype(float)
    fwd5 = o.shift(-(H + 1)) / o.shift(-1) - 1.0
    vol60 = p["close"].pct_change().rolling(60, min_periods=40).std()
    assets = pd.read_parquet(DATA / "assets.parquet").set_index("symbol")
    shortable = (assets["shortable"] & assets["easy_to_borrow"]).reindex(U.columns).fillna(False)

    meta = {"dates": [str(d.date()) for d in U.index], "symbols": list(U.columns),
            "industry": ind.fillna("NA").tolist()}
    (CACHE / "meta.json").write_text(json.dumps(meta))
    for name, arr in (("U", U.to_numpy()), ("fwd1", fwd1.to_numpy(np.float32)), ("fwd5", fwd5.to_numpy(np.float32)),
                      ("beta", beta.to_numpy(np.float32)), ("vol60", vol60.to_numpy(np.float32)),
                      ("mcap", mcap.to_numpy(np.float32)), ("spy_fwd", fwd1["SPY"].to_numpy(np.float32)),
                      ("shortable", shortable.to_numpy())):
        np.save(CACHE / f"{name}.npy", arr)
    print("residual 5-day target...", flush=True)
    y = residual_target(fwd5.where(U), U, ind, mcap, beta)
    np.save(CACHE / "y_resid5.npy", y.to_numpy(np.float32))
    del y, o, fwd5, vol60; gc.collect()

    done: list = []
    print("group 1: v3 factors", flush=True)
    raw = {k: v.astype("float32") for k, v in compute_all(p).items()}
    raw.update({k: v.astype("float32") for k, v in extra_factors(p["close"], spy).items()})
    save_batch(raw, U, ind, mcap, done)
    del raw; gc.collect()
    print("group 2: extended library", flush=True)
    raw = {k: v.astype("float32") for k, v in library(p, ind, spy, beta).items()}
    save_batch(raw, U, ind, mcap, done)
    (CACHE / "factors.json").write_text(json.dumps(done))
    print(f"done: {len(done)} factors cached", flush=True)


if __name__ == "__main__":
    main()
