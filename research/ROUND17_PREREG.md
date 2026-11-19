# Round 17 pre-registration — earnings-announcement premium (event family) on top of P*15, single trial

Written before any round-17 result exists. The last factor family that can be built from data already on disk
(8-K Item 2.02 dates, data/earnings_dates.parquet), with its literature sign and nothing estimated:

  eap  1 if the stock's next earnings announcement, predicted as the one-year anniversary of an announcement
       made in the past 364 days, falls within the next 30 calendar days, else 0
       (Frazzini-Lamont 2007; Barber-De George-Lehavy-Trueman 2013: stocks earn a premium in expected
       announcement months, so they should not be shorted then). Point-in-time: only announcements dated
       on or before t are used. Cleaned like every other factor.

Single trial: P*16 = the P*15 composite plus eap, equal weight; everything else frozen. Base = P*15.
Decision: proposed only if selection AND holdout Sharpe are both higher than P*15's, the full-period Sharpe is at
least 0.10 higher, and the realistic $100k check confirms it. Otherwise P*15 remains the best.
