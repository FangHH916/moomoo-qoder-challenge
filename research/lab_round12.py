"""Round 12 (ROUND12_PREREG.md): greedy factor search for the D77 composite until no single add/drop raises
the selection-window Sharpe of the whole portfolio (T9 + D77(S), minimum variance) by >= 0.02.
Holdout metrics of every evaluation go to a sealed file; only the final set and P* are opened at the end.
usage: python lab_round12.py [workers]
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

import r10_deploy as D

MIN_GAIN, MAX_STEPS, MIN_SIZE = 0.02, 8, 3
T9_FILE = D.LAB / "r12_t9_sleeve.csv"
SEALED = D.LAB / "round12_holdout_sealed.jsonl"
_W: dict = {}


def _init() -> None:
    L, F, P = D.research()
    _W.update(L=L, F=F, P=P, mask=F.tradable(), bt0=int(np.searchsorted(L.DATES, L.BT_START)),
              t9=pd.read_csv(T9_FILE, index_col=0, parse_dates=True))


def composite(members: tuple) -> pd.DataFrame:
    """Round-7 construction (lab_fix.prior_scores) with oriented members ((name, sign), ...)."""
    L = _W["L"]
    acc = None
    for k, sgn in members:
        f = np.asarray(L.factor(k), dtype=np.float32)
        v = np.where(np.isfinite(f), f, 0.0) * np.float32(sgn)
        acc = v if acc is None else acc + v
    s = np.where(_W["mask"], acc / len(members), np.nan).astype(np.float32)
    s[: _W["bt0"]] = np.nan
    return pd.DataFrame(s, index=L.DATES, columns=L.SYMS)


def portfolio(members: tuple) -> tuple[pd.Series, pd.DataFrame, pd.Series]:
    L, F, P, t9 = _W["L"], _W["F"], _W["P"], _W["t9"]
    d77, _, _ = D.d77_record(L, F, composite(members))
    idx = t9.index.intersection(d77.index)
    dl = d77.loc[idx, "long_gross"] + d77.loc[idx, "hedge"].clip(lower=0)
    ds = d77.loc[idx, "short_gross"] + (-d77.loc[idx, "hedge"]).clip(lower=0)
    R = pd.DataFrame({"T9": t9.loc[idx, "net"], "D77": d77.loc[idx, "net"]})
    MU = pd.DataFrame({"T9": 0.5 * t9.loc[idx, "long"] + 0.6 * t9.loc[idx, "short"], "D77": 0.5 * dl + 0.6 * ds})
    LU = pd.DataFrame({"T9": t9.loc[idx, "long"], "D77": dl})
    p, W = P.combine(R, MU, "MV", False, LU)
    return p, W, d77["net"]


def evaluate(members: tuple) -> dict:
    L = _W["L"]
    p, W, d = portfolio(members)
    return {"members": [f"{'-' if s < 0 else '+'}{k}" for k, s in members],
            "sel": D.metrics(p.loc[L.BT_START:L.VAL_END])["sharpe"],
            "d77_sel": D.metrics(d.loc[L.BT_START:L.VAL_END])["sharpe"], "avg_w_d77": float(W["D77"].mean()),
            "_hold": D.metrics(p.loc[L.HOLD_START:])["sharpe"], "_full": D.metrics(p)["sharpe"]}


def _seal(r: dict, fh) -> dict:
    fh.write(json.dumps({"members": r["members"], "hold": r.pop("_hold"), "full": r.pop("_full")}) + "\n")
    return r


def main() -> None:
    workers = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    t0 = time.time()
    L, F, P = D.research()
    snap = D.DATA / "research_snapshot"                     # the exact bar files round 10 used
    if not (snap / "bars_extra_etfs.parquet").exists():
        shutil.copy2(D.DATA / "bars_extra_etfs.parquet", snap / "bars_extra_etfs.parquet")
    P.DATA = snap
    t9, _ = D.t9_record(P)
    t9[["net", "long", "short"]].to_csv(T9_FILE)
    screen = pd.read_csv(D.LAB / "screen.csv", index_col=0)
    names = json.loads((L.CACHE / "factors.json").read_text())
    cands = {k: float(np.sign(screen.at[k, "IC5"])) for k in names if k not in D.PRIOR_SET}
    assert len(cands) == 54 and all(s != 0 for s in cands.values())
    S = tuple((k, 1.0) for k in D.PRIOR_SET)
    log, sel_all, rows = [], [], []
    with SEALED.open("w") as fh, ProcessPoolExecutor(workers, initializer=_init) as ex:
        cur = _seal(ex.submit(evaluate, S).result(), fh)
        sel_all.append(cur["sel"]); rows.append({"step": 0, "move": "start", **cur})
        print(f"step 0: S0 = 11 literature factors, selection Sharpe {cur['sel']:.4f}", flush=True)
        stop = "max_steps"
        for step in range(1, MAX_STEPS + 1):
            have = {k for k, _ in S}
            moves = [(f"add {'-' if cands[k] < 0 else '+'}{k}", S + ((k, cands[k]),)) for k in cands if k not in have]
            if len(S) > MIN_SIZE:
                moves += [(f"drop {k}", tuple(m for m in S if m[0] != k)) for k, _ in S]
            res = [_seal(r, fh) for r in ex.map(evaluate, [m[1] for m in moves])]
            fh.flush()
            sel_all += [r["sel"] for r in res]
            rows += [{"step": step, "move": m[0], **r} for m, r in zip(moves, res)]
            order = np.argsort([-r["sel"] for r in res])
            top = [(moves[j][0], round(res[j]["sel"], 4)) for j in order[:5]]
            best, move = res[order[0]], moves[order[0]]
            log.append({"step": step, "best_move": move[0], "sel_after": best["sel"], "sel_before": cur["sel"],
                        "top5": top, "evaluated": len(moves)})
            print(f"step {step}: {len(moves)} moves, best {move[0]} -> {best['sel']:.4f} (from {cur['sel']:.4f}); "
                  f"top5 {top}  [{time.time() - t0:.0f}s]", flush=True)
            if best["sel"] < cur["sel"] + MIN_GAIN:
                stop = "local_maximum"
                break
            S, cur = move[1], best
    pd.DataFrame(rows).assign(members=lambda d: d["members"].map(" ".join)).to_csv(D.LAB / "round12_evals.csv", index=False)

    # search finished: open the holdout for the final set and for P* only
    _init()
    out = {"stop": stop, "path": log, "n_evaluations": len(sel_all),
           "final_set": [f"{'-' if s < 0 else '+'}{k}" for k, s in S]}
    for label, members in (("final", S), ("pstar", tuple((k, 1.0) for k in D.PRIOR_SET))):
        p, W, d = portfolio(members)
        out[label] = {**D.windows(p, L), "avg_w_d77": float(W["D77"].mean()),
                      "d77_sleeve": D.windows(d, L)}
        if label == "final":
            out["deflated_sharpe_selection"] = L.v1.deflated_sharpe(p.loc[L.BT_START:L.VAL_END], np.array(sel_all))
    f, ps = out["final"], out["pstar"]
    out["replaces_pstar_if_user_approves"] = bool(f["holdout"]["sharpe"] >= ps["holdout"]["sharpe"]
                                                  and f["full"]["sharpe"] >= ps["full"]["sharpe"] + 0.10)
    (D.LAB / "ROUND12.json").write_text(json.dumps(out, indent=2, default=float))
    print(json.dumps({k: out[k] for k in ("stop", "n_evaluations", "final_set", "deflated_sharpe_selection",
                                         "replaces_pstar_if_user_approves")}, indent=2, default=float), flush=True)
    for label in ("final", "pstar"):
        print(label, {w: round(v["sharpe"], 3) for w, v in out[label].items() if w in ("selection", "holdout", "full")},
              "avg_w_d77", round(out[label]["avg_w_d77"], 3), flush=True)


if __name__ == "__main__":
    main()
