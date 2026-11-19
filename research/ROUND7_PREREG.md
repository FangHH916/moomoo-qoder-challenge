# Round 7 pre-registration (written before any round-7 result exists)

Rounds 4–6 showed that weekly LASSO re-estimation overfits: better selection-window Sharpe,
worse holdout (R4 -0.30, R6 -0.53). Round 7 removes estimation entirely.

Model: equal-weight average of 11 cleaned, industry+size-neutralised factors, each oriented by
its sign in the published literature (all cached factors are already oriented so that higher =
higher expected return):
  value      bm, ep, sp          (Fama-French; Basu; Barbee-Mukherji-Raines)
  momentum   mom12_1             (Jegadeesh-Titman)
  low risk   ivol60              (Ang-Hodrick-Xing-Zhang)
  quality    roe, gpa            (Novy-Marx; Asness-Frazzini-Pedersen QMJ)
  investment ag  (-asset growth) (Cooper-Gulen-Schill)
  accruals   accr (-accruals)    (Sloan)
  issuance   nsi (-net issuance) (Pontiff-Woodgate)
  PEAD       ear                 (Bernard-Thomas; Chan-Jegadeesh-Lakonishok)
No weights are estimated from data. Scores are computed on the tradable universe.

Books (exactly two trials, same frozen settings as round 4: weekly rebalance, 5-day smoothing,
3x buffer, inverse-vol, vol targeting on new positions, signal guard, costs):
  R7_shortspy_n20  (primary)    R7_ls_n20 (secondary)
Primary endpoint: holdout Sharpe of R7_shortspy_n20 after uniform scaling to the 90% margin cap.
Reported whatever the outcome; no further tuning.
