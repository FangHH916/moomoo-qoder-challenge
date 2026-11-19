"""Rebuild a clean trial registry from every reports/lab/daily_*.csv.

Writes reports/lab/registry_val.csv (selection-window metrics, printed) and
reports/lab/registry_holdout_SEALED.csv (holdout metrics, NOT printed).
Each trial is tagged 'contaminated' when its scores came from a LASSO/ENet fit
without the carry-forward + signal-guard fix (see lab_fix.py).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import lab_run as L


def main() -> None:
    val_rows, hold_rows = [], []
    for f in sorted(L.LAB.glob("daily_*.csv")):
        name = f.stem[len("daily_"):]
        bt = pd.read_csv(f, index_col=0, parse_dates=True)
        if "spy" not in bt:
            continue
        v = L.seg_metrics(bt, L.BT_START, L.VAL_END)
        h = L.seg_metrics(bt, L.HOLD_START, None)
        val_rows.append({"trial": name, "fixed_pipeline": bool(name.startswith("R4_")),
                         **{k: v.get(k) for k in ("sharpe", "annual_return", "max_drawdown", "annual_vol",
                                                  "avg_daily_turnover", "monthly_win_rate", "beta")}})
        hold_rows.append({"trial": name, **{f"hold_{k}": x for k, x in h.items()}})
    val = pd.DataFrame(val_rows).set_index("trial").sort_values("sharpe", ascending=False)
    val.to_csv(L.LAB / "registry_val.csv")
    pd.DataFrame(hold_rows).set_index("trial").to_csv(L.LAB / "registry_holdout_SEALED.csv")
    print(f"{len(val)} trials registered", flush=True)
    print(val.round(3).to_string(), flush=True)


if __name__ == "__main__":
    main()
