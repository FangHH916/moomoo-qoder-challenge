"""Round 13 (ROUND13_PREREG.md): walk-forward factor search. Before each out-of-sample year the round-12 greedy
search is re-run on data realised by then (strict local maximum, factor signs from training-window IC); the chosen
set trades the next year. Stitched out-of-sample Sharpe is compared with P* over the same four years.
usage: python lab_round13.py [workers]
"""
from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

import lab_round12 as R12
import r10_deploy as D

YEARS = [("2022-11-18", "2023-11-17"), ("2023-11-18", "2024-11-17"), ("2024-11-18", "2025-11-17"),
         ("2025-11-18", "2026-11-17")]
MAX_STEPS, EPS, MIN_SIZE = 10, 1e-4, 3


def _init() -> None:
    R12._init()
    L = R12._W["L"]
    R12._W["fwd5"] = L.df("fwd5").where(L.df("U").astype(bool))


def ic_series(k: str) -> pd.Series:
    L = R12._W["L"]
    f = pd.DataFrame(np.asarray(L.factor(k)), index=L.DATES, columns=L.SYMS)
    return L.v1.rank_ic(f, R12._W["fwd5"])


def port(members: tuple, end=None) -> pd.Series:
    """R12.portfolio with the scores cut at `end` (decision date), so nothing after it is used."""
    W = R12._W
    L, F, P, t9 = W["L"], W["F"], W["P"], W["t9"]
    sc = R12.composite(members)
    d77, _, _ = D.d77_record(L, F, sc if end is None else sc.loc[:end])
    idx = t9.index.intersection(d77.index)
    dl = d77.loc[idx, "long_gross"] + d77.loc[idx, "hedge"].clip(lower=0)
    ds = d77.loc[idx, "short_gross"] + (-d77.loc[idx, "hedge"]).clip(lower=0)
    R = pd.DataFrame({"T9": t9.loc[idx, "net"], "D77": d77.loc[idx, "net"]})
    MU = pd.DataFrame({"T9": 0.5 * t9.loc[idx, "long"] + 0.6 * t9.loc[idx, "short"], "D77": 0.5 * dl + 0.6 * ds})
    LU = pd.DataFrame({"T9": t9.loc[idx, "long"], "D77": dl})
    p, _ = P.combine(R, MU, "MV", False, LU)
    return p


def train_sharpe(args) -> float:
    members, end, last = args
    p = port(members, end)
    return D.metrics(p.loc[R12._W["L"].BT_START:last])["sharpe"]


def fmt(S) -> list[str]:
    return [f"{'-' if s < 0 else '+'}{k}" for k, s in S]


def greedy(ex, orient: dict, end, last, log_prefix: str, t0: float):
    S = tuple((k, 1.0) for k in D.PRIOR_SET)
    cur = ex.submit(train_sharpe, (S, end, last)).result()
    start, path = cur, []
    for step in range(1, MAX_STEPS + 1):
        have = {k for k, _ in S}
        moves = [(f"add {'-' if orient[k] < 0 else '+'}{k}", S + ((k, orient[k]),)) for k in orient if k not in have]
        if len(S) > MIN_SIZE:
            moves += [(f"drop {k}", tuple(m for m in S if m[0] != k)) for k, _ in S]
        res = list(ex.map(train_sharpe, [(m[1], end, last) for m in moves]))
        j = int(np.argmax(res))
        if res[j] <= cur + EPS:
            break
        S, cur = moves[j][1], res[j]
        path.append({"step": step, "move": moves[j][0], "train_sharpe": cur})
        print(f"{log_prefix} step {step}: {moves[j][0]} -> {cur:.4f}  [{time.time() - t0:.0f}s]", flush=True)
    return S, start, cur, path


def main() -> None:
    workers = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    t0 = time.time()
    L, F, P = D.research()
    names = json.loads((L.CACHE / "factors.json").read_text())
    cands = [k for k in names if k not in D.PRIOR_SET]
    chosen = []
    with ProcessPoolExecutor(workers, initializer=_init) as ex:
        IC = pd.DataFrame(dict(zip(cands, ex.map(ic_series, cands))))
        print(f"daily ICs for {len(cands)} candidates  [{time.time() - t0:.0f}s]", flush=True)
        for a, b in YEARS:
            i0 = int(np.searchsorted(L.DATES, pd.Timestamp(a)))
            end, last, ic_last = L.DATES[i0 - 1], L.DATES[i0 - 3], L.DATES[i0 - 7]
            sign = np.sign(IC.loc[L.BT_START:ic_last].mean())
            orient = {k: float(s) for k, s in sign.items() if s != 0}
            S, start, cur, path = greedy(ex, orient, end, last, f"[{a[:4]}]", t0)
            chosen.append({"oos": f"{a}..{b}", "decision_close": str(end.date()), "train_rows_until": str(last.date()),
                           "train_sharpe_start": start, "train_sharpe_final": cur, "set": fmt(S), "path": path, "_S": S})
            print(f"[{a[:4]}] chosen {fmt(S)} (training Sharpe {start:.3f} -> {cur:.3f})", flush=True)
        runs = list(ex.map(port, [c["_S"] for c in chosen]))
        pstar = ex.submit(port, tuple((k, 1.0) for k in D.PRIOR_SET)).result()
    seg = pd.concat([r.loc[a:b] for r, (a, b) in zip(runs, YEARS)])
    ref = pstar.loc[YEARS[0][0]:YEARS[-1][1]]
    yearly = {f"{a}..{b}": {"walk_forward": D.metrics(r.loc[a:b])["sharpe"], "pstar": D.metrics(pstar.loc[a:b])["sharpe"]}
              for r, (a, b) in zip(runs, YEARS)}
    wf, ps = D.metrics(seg), D.metrics(ref)
    out = {"walk_forward_oos": wf, "pstar_same_years": ps, "per_year_sharpe": yearly,
           "chosen": [{k: v for k, v in c.items() if k != "_S"} for c in chosen],
           "search_beats_pstar_by_0.10": bool(wf["sharpe"] >= ps["sharpe"] + 0.10),
           "note": "book switches between years not charged"}
    (D.LAB / "ROUND13.json").write_text(json.dumps(out, indent=2, default=float))
    print(json.dumps({k: out[k] for k in ("walk_forward_oos", "pstar_same_years", "per_year_sharpe",
                                         "search_beats_pstar_by_0.10")}, indent=2, default=float), flush=True)
    print(f"done [{time.time() - t0:.0f}s]", flush=True)


if __name__ == "__main__":
    main()
