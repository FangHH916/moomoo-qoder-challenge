# Round 11 pre-registration — can a third sleeve raise the Sharpe of P*?

Written before any round-11 result exists. P* (round-10 pick: T9 + D77, Ledoit-Wolf minimum variance)
is deployed and stays deployed during the competition whatever this round finds; a switch would
need the user's approval and is only considered under the decision rule below.

Candidate sleeves (literature specifications, nothing tuned; same timing and cost model as T9:
signal at close t, trade at open t+1, 2 bp slippage, $0.99 per order, every ETF <= 38%):
- SEC3  industry momentum (Moskowitz-Grinblatt 1999) with the 11 SPDR sector ETFs
        XLK XLF XLE XLV XLI XLY XLP XLU XLB XLRE XLC: every 5 sessions (same anchor as P*) hold the
        3 sectors with the highest 12-1 month return (close t-21 / close t-252 - 1), each at
        (0.10 / 60-day vol) / 3, long only.
- TOM   turn of the month (Ariel 1987; Lakonishok-Smidt 1988; McConnell-Xu 2008): hold SPY at
        0.10 / 60-day vol from the open of the last session of each month to the open of the
        4th session of the next month; flat otherwise (decided daily).

Sets (combination method frozen to the round-10 winner: long-only Ledoit-Wolf minimum variance,
252-day window, min 126, 2-session lag, weekly; each set scaled so its peak margin is 90%;
portfolio-level financing above 100% long):
  B0 {T9, D77}  (= P*, recomputed as the control)   B1 {T9, D77, SEC3}
  B2 {T9, D77, TOM}                                   B3 {T9, D77, SEC3, TOM}

Report for every set: selection (2021-11-18 -> 2025-11-17), holdout (2025-11-18 -> 2026-11-17) and
full Sharpe, annual return, max drawdown, worst calendar year, average sleeve weights; the deflated
Sharpe of the best set over all trials ever run (registry + round 10 + round 11).

Decision rule: a set replaces P* only if (i) its full-period Sharpe is at least 0.10 higher than B0,
(ii) its selection AND holdout Sharpe are both higher than B0's, and (iii) the realistic $100k
simulation (whole shares, portfolio-level fees, competition caps) confirms an improvement of at
least 0.10. Otherwise P* stays and the search ends here.
