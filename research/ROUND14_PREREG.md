# Round 14 pre-registration — a new factor family with literature signs, no estimation

Written before any round-14 result exists. Rounds 12-13 showed that searching the cached factor set with data
does not help out of sample. This round adds a factor family that was never used, from SEC data already on
disk (data/fund_frames.parquet), each with the sign the literature gives it, so nothing is estimated:

  cfp  cash-flow yield: operating cash flow / market cap        (+, Lakonishok-Shleifer-Vishny 1994)
  op   operating profitability: operating income / book equity  (+, Fama-French 2015)
  cop  cash profitability: operating cash flow / total assets    (+, Ball-Gerakos-Linnainmaa-Nikolaev 2016)
  sg   minus annual revenue growth                               (+ for low growth, Lakonishok-Shleifer-Vishny 1994)

Point-in-time: annual values usable from fiscal-period end + 100 days, balance-sheet values from period end
+ 75 days (same rules as round 6). Cleaning as every other factor (5x MAD, z-score, industry + log-size
neutralisation, z-score).

Single trial: P*15 = the D77 composite with the 11 literature factors plus these 4 (equal weight), everything
else frozen (T9 + D77, Ledoit-Wolf minimum variance). Compared with P* on the selection window, the holdout and
the full five years.

Decision: proposed to the user (no automatic deployment) only if its selection AND holdout Sharpe are both
higher than P*'s and its full-period Sharpe is at least 0.10 higher; otherwise P* stays.
