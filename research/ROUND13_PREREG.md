# Round 13 pre-registration — walk-forward factor search: is searching itself worth anything out of sample?

Written before any round-13 result exists. Round 12 showed an in-sample local maximum (selection Sharpe 1.35)
that did not survive the holdout. The question that decides whether P* can be beaten by factor search is
the out-of-sample Sharpe of the *search procedure*, measured on years it never saw.

Procedure (expanding window, re-run every year):
- Out-of-sample years: Y1 2022-11-18..2023-11-17, Y2 2023-11-18..2024-11-17, Y3 2024-11-18..2025-11-17,
  Y4 2025-11-18..2026-11-17.
- Before each year, using only data realised by the decision date (portfolio returns up to 3 sessions before
  the year starts; factor ICs up to 6 sessions before):
  1. orient every candidate factor by the sign of its mean daily 5-day rank IC in the training window;
  2. greedy add/drop search exactly as round 12 (start = the 11 literature factors, objective = training-window
     Sharpe of the whole portfolio T9 + D77(S), minimum variance), but with no minimum gain: keep moving while
     any move improves the training Sharpe (strict local maximum), at most 10 steps.
- Trade the selected set for the following year; the year's returns come from that set's full backtest.
  Book switches between years are not charged (D77 is 6-27% of gross exposure; noted, not modelled).

Comparison: stitched walk-forward returns Y1-Y4 vs P* over the same four years (P*'s factor set was fixed a
priori in round 7). Report Sharpe, annual return, max drawdown, per-year Sharpe, and the sets chosen each year.

Decision: if the walk-forward search beats P* by at least 0.10 Sharpe over Y1-Y4, factor search has
out-of-sample value and the set selected on all data (through 2026-11-17) is proposed to the user (no
automatic deployment). Otherwise P* is the maximum this data supports with an honest procedure and the
Sharpe search ends.
