# Round 6 pre-registration (written before any round-6 result exists)

Context: the pre-registered round-4/5 pick (R4_E11cf_shortspy_n20: LASSO on 56 price/volume
factors, tradable-universe training, carry-forward, signal guard, short 20 + SPY hedge) had
selection-window Sharpe 0.70 but holdout Sharpe -0.30 (DSR 0.435). Conclusion then: price/volume
data alone carries no robust alpha; new information is needed.

Single change tested in this round: add 9 factors built from NEW information —
SEC fundamentals (bm, ep, sp, gpa, roe, ag, accr), SEC share-count issuance (nsi) and 8-K Item 2.02
earnings-announcement abnormal returns (ear). Point-in-time lags as in lab_factors2.py.

Everything else is frozen to the round-4 pick: LassoCV (TimeSeriesSplit 5, 30 alphas), 252-day
training window, weekly retrain, 5-day residual target, tradable universe, carry-forward of the
last non-zero fit, signal guard (< 50 distinct scores -> hold), weekly rebalance, 5-day smoothing,
3x buffer, inverse-vol weights, vol targeting on new positions, 5 bp slippage, $0.99/order, 6% borrow.

Trials (exactly two):
  R6_shortspy_n20  (primary)   short 20 lowest scores + beta-matched SPY long
  R6_ls_n20        (secondary) 20 long / 20 short, SPY beta overlay

Primary endpoint: holdout (2025-11-18 -> 2026-11-17) Sharpe of R6_shortspy_n20 after uniform
scaling to the 90% margin cap, compared with the round-4 pick's -0.30. Both selection-window and
holdout metrics are reported whatever they are. No further tuning after seeing them.
