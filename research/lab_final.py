"""Pre-registered final selection, then the one-time holdout opening.

Rule (fixed before round-5 results were seen):
  1. candidates = trials run on the corrected pipeline (names R4_* / R5_*);
  2. feasible = on every day, margin = 0.5 * long gross + 0.6 * short gross <= 90% of equity
     (competition margin: 50% initial on longs, 60% on shorts; SPY overlay counted);
  3. pick the highest selection-window Sharpe among feasible candidates;
  4. only then compute holdout metrics (2025-11-18 onward) and the deflated Sharpe
     against ALL trials ever run (contaminated ones included, as they were tried).
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

import lab_run as L

MARGIN_CAP = 0.90


def margin_ok(bt: pd.DataFrame) -> tuple[bool, float]:
    longs = bt["long_gross"] + bt["hedge"].clip(lower=0)
    shorts = bt["short_gross"] + (-bt["hedge"]).clip(lower=0)
    m = 0.5 * longs + 0.6 * shorts
    return bool(m.max() <= MARGIN_CAP), float(m.max())


def main() -> None:
    reg = pd.read_csv(L.LAB / "registry_val.csv", index_col=0)
    cands = []
    for name in reg.index:
        if not (name.startswith("R4_") or name.startswith("R5_")):
            continue
        bt = pd.read_csv(L.LAB / f"daily_{name}.csv", index_col=0, parse_dates=True)
        ok, mx = margin_ok(bt)
        cands.append({"trial": name, "val_sharpe": reg.loc[name, "sharpe"], "max_margin": mx, "feasible": ok})
    c = pd.DataFrame(cands).sort_values("val_sharpe", ascending=False)
    print(c.round(3).to_string(index=False), flush=True)
    best = c[c.feasible].iloc[0]["trial"]
    print(f"\nSELECTED (pre-registered rule): {best}\n", flush=True)

    # ---- one-time holdout opening
    bt = pd.read_csv(L.LAB / f"daily_{best}.csv", index_col=0, parse_dates=True)
    val = L.seg_metrics(bt, L.BT_START, L.VAL_END)
    hold = L.seg_metrics(bt, L.HOLD_START, None)
    all_sh = reg["sharpe"].dropna().to_numpy()
    dsr = L.v1.deflated_sharpe(bt.loc[L.BT_START:L.VAL_END, "net"], all_sh)
    out = {"selected": best, "n_trials_total": int(len(all_sh)), "deflated_sharpe_prob_val": dsr,
           "validation": val, "holdout": hold}
    (L.LAB / "FINAL.json").write_text(json.dumps(out, indent=2, default=float))
    keys = ("sharpe", "annual_return", "monthly_return_mean", "max_drawdown", "annual_vol", "sortino", "calmar",
            "avg_daily_turnover", "monthly_win_rate", "beta", "days")
    print(pd.DataFrame({"validation 2021-11..2025-11": {k: val.get(k) for k in keys},
                        "HOLDOUT 2025-11..2026-11": {k: hold.get(k) for k in keys}}).round(4).to_string(), flush=True)
    print(f"\ndeflated Sharpe probability (validation, {len(all_sh)} trials): {dsr:.3f}", flush=True)
    # holdout of the other corrected candidates, for transparency only (selection is already fixed)
    rows = []
    for name in c["trial"]:
        b = pd.read_csv(L.LAB / f"daily_{name}.csv", index_col=0, parse_dates=True)
        h = L.seg_metrics(b, L.HOLD_START, None)
        rows.append({"trial": name, "hold_sharpe": h.get("sharpe"), "hold_ann": h.get("annual_return"),
                     "hold_mdd": h.get("max_drawdown")})
    print("\nholdout of all corrected candidates (for transparency):")
    print(pd.DataFrame(rows).round(3).to_string(index=False))


if __name__ == "__main__":
    main()
