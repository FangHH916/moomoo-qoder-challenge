# Round 10 pre-registration — systematic portfolio-level Sharpe maximisation

Written before any round-10 result exists. All sleeve parameters come from the literature or from
earlier frozen rounds; combination weights are always estimated on trailing data only (252-day
window, min 126, 2-day lag, weekly rebalance), so every reported return is out of sample in time.
The only selection bias is the choice among the 32 combinations below; it is measured with the
deflated Sharpe ratio (Bailey & Lopez de Prado) over all 32 and, more conservatively, over all trials ever run.

Sleeves (daily net returns after costs: 2 bp ETF slippage, 5 bp stock slippage, $0.99/order, 6% borrow,
4.8% financing above 100% long; every ETF <= 38% of equity):
- T9   ETF trend, 9 ETFs (SPY QQQ IWM TLT IEF GLD SLV USO UUP), long/flat on the 252-day return sign (round 9).
- T21  ETF trend, 21 ETFs (T9 + EFA EEM EWJ FXI HYG LQD TIP AGG DBC DBA UNG VNQ), position fraction =
       mean of 1{return > 0} over 21/63/252-day lookbacks (AQR trend convention), long/flat, inverse-vol.
- RP   static risk parity SPY TLT GLD IEF (weights proportional to 1/60-day vol, 100% gross), weekly.
- D77  round-8 no-estimation short book with 77 shorts (so 77 + 21 ETFs + 2 spare <= 100 positions).

Sleeve sets: S1 {T9, D77}, S2 {T21, D77}, S3 {T21, D77, RP}, S4 {T21, RP}.
Combination methods: IV inverse volatility; MV long-only minimum variance (Ledoit-Wolf covariance);
ERC equal risk contribution (full covariance); MS max-Sharpe with means shrunk 50% toward their average,
long-only, each sleeve <= 60%. Each method with and without a volatility-managed overlay
(Moreira-Muir: exposure = clip(trailing-252d vol / trailing-21d vol, 0.5, 1.5)).
Every combination is scaled uniformly so its peak margin (0.5 x long + 0.6 x short) equals 90% of equity.

Pick: the highest full-period (2021-11-18 -> 2026-11-17) Sharpe. Report for every combination: selection,
holdout and full Sharpe, annual return, max drawdown, vol, worst calendar year; and the deflated Sharpe of the pick.
