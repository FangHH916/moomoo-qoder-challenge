# Round 9 pre-registration (written before any round-9 result exists)

Goal restated by the user: maximise the Sharpe ratio of the *strategy portfolio*. Rounds 4–8 found the
single-strategy ceiling (most robust: D = no-estimation broad short book, 5-year Sharpe 0.35).
Combining return streams with low correlation is the remaining principled lever (portfolio Sharpe
of N uncorrelated equal-Sharpe streams ~ sqrt(N) x single Sharpe).

Sleeves (no parameter is tuned; all come from the literature or earlier frozen rounds):
- D: round-8 deployable book — literature-prior composite (scores_R7), short 95, beta-matched index
  hedge, weekly rebalance (daily file: daily_R8_shortspy_n95_deployable.csv, unscaled).
- T: ETF time-series momentum (Moskowitz-Ooi-Pedersen 2012), long/flat (no shorting → no 6% borrow):
  universe SPY, QQQ, IWM, TLT, IEF, GLD, SLV, USO, UUP; signal = sign of 252-day total return
  (adjusted prices) at close t; weight_i = 1{signal>0} x (0.10 / sigma_i) / 9, sigma_i = 60-day
  annualised volatility; each ETF <= 38% of equity, total long <= 100%; rebalance every 5 trading
  days; trade at open t+1; costs 2 bp slippage, $0.99/order, 4.8% financing above 100% long.
- Combination P: weekly risk parity — sleeve weights proportional to 1 / trailing 126-day realised
  volatility of each sleeve's daily net return (data up to t-1 only); then one uniform scale so the
  combined peak margin (0.5 x long gross + 0.6 x short gross, D's hedge counted) is 90% of equity.

Report (whatever the outcome): Sharpe, return, drawdown, vol for T, D and P on the selection window,
the holdout and the full five years, plus the D–T correlation. Primary endpoint: full-period Sharpe
of P versus D alone. No tuning after seeing results.
