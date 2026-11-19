# Round 16 pre-registration — classic price-based anomalies on top of P*15, single trial

Written before any round-16 result exists. Rounds 14-15 tested two fundamental families (one helped, one did
not). This round tests the canonical price-based anomaly family, using factors already in the cache with the
literature sign they are coded with (checked in lab_factors.py / factors.py / pipeline_v3.py), nothing estimated:

  rev21     short-term reversal, minus the 21-day return            (Jegadeesh 1990)
  high52    price / 52-week high                                    (George-Hwang 2004)
  max21     minus the largest daily return over 21 days (lottery)  (Bali-Cakici-Whitelaw 2011)
  bab       minus 60-day beta (betting against beta)                (Frazzini-Pedersen 2014)
  ind_mom6  industry momentum                                       (Moskowitz-Grinblatt 1999)

Single trial: P*20 = the P*15 composite (15 factors) plus these 5, equal weight; everything else frozen.
Base = P*15. Decision: proposed only if selection AND holdout Sharpe are both higher than P*15's, the full-period
Sharpe is at least 0.10 higher, and the realistic $100k check confirms it. Otherwise P*15 remains the best.
