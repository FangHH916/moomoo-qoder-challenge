# Research summary — multi-factor short-alpha book (US equities, daily data)

## Setup
- Data: Alpaca SIP daily bars (split/dividend adjusted + raw), SEC SIC industries (Fama-French 12) and quarterly shares outstanding (60-day reporting lag). 2019-10 → 2026-11.
- Universe per day: competition-eligible US common stocks (price ≥ $1, market cap ≥ $500M) with 20-day median dollar volume ≥ $5M (~1,800 names). Shorts only in shortable, easy-to-borrow names ≥ $1B.
- Factors: 56 daily price/volume factors — 16 ported from my A-share library (DC1–4 approximated with trade counts), 40 from the US literature (momentum variants, residual momentum, frog-in-the-pan, reversal, overnight/intraday, volatility, MAX, BAB, turnover, Amihud, seasonality, 52-week high...).
- Cleaning: 5×MAD winsorise → z-score → OLS-neutralise on industry + log market cap → z-score, every day.
- Timing: signal after close t, trade at open t+1; costs = 5 bp slippage, $0.99/order, 6% p.a. short borrow (competition rate), 4.8% financing above 100% long.
- Discipline: selection window 2021-11-18 → 2025-11-17; holdout 2025-11-18 → 2026-11-17 sealed until a pre-registered rule picked the final strategy (rule committed in git before the last round; one amendment — uniform scaling to the 90% margin cap — added before opening the holdout).

## What we learned
1. **Composite signal is real but asymmetric.** 5-day IC ≈ 0.025 (t ≈ 2.5) on the selection window, but the long tail adds nothing (top 5% ≈ median); the short tail carries the signal, and most of it sits in names that cannot be borrowed.
2. **Book design matters more than the model.** Stock long/short books had Sharpe ≈ 0 on the selection window. Training on the tradable universe and replacing single-stock longs with a beta-matched SPY long ("short alpha + SPY") raised it to 0.5–0.7.
3. **A tie-break artefact inflated earlier results.** LassoCV returned all-zero coefficients in 35–68% of retrains; constant scores then picked names alphabetically. Fix: carry forward the last non-zero coefficients and never trade on a score cross-section with < 50 distinct values. All earlier LASSO/ENet numbers were discarded.
4. **Final pre-registered pick: LASSO on all 56 factors, tradable universe, carry-forward, short 20 + SPY hedge.** Selection window: Sharpe 0.70, 8.9% p.a., max drawdown −17.9% (scaled to the margin cap). **Holdout: Sharpe −0.30, −4.0% p.a., max drawdown −10.7%.** Gross alpha fell from +14.8% to −0.4% p.a.; the 6% borrow rate (~3% p.a. drag) and costs did the rest.
5. **Deflated Sharpe (47 trials) = 0.435; rankings flipped in the holdout** (models that were weak in selection were best in holdout). Conclusion: with daily price/volume data and realistic shorting constraints, no out-of-sample-robust alpha was found. More in-sample tuning would only overfit further; new information (fundamentals, short interest, earnings events) is needed.

## Rounds 6–8 (each pre-registered in git before running; one primary endpoint each)
6. **New information** — SEC fundamentals (bm, ep, sp, gpa, roe, asset growth, accruals), net share issuance and 8-K earnings-announcement returns (PEAD) added to the frozen round-4 LASSO pipeline. Selection Sharpe rose to 0.78, **holdout fell to −0.53**: more fitted inputs, more overfitting.
7. **No estimation** — equal-weight literature-prior composite of 11 factors (value, momentum, low risk, quality, investment, accruals, issuance, PEAD). Best and least-fitted signal (5-day IC 0.018, t 2.87, monotone deciles), but 20-name tail books were dominated by a few high-volatility names (selection max drawdown −59% before scaling).
8. **Breadth** (fundamental law: IR ≈ IC·√breadth) — same composite, 100 names per side. Primary endpoint (long/short) holdout Sharpe +1.13 but selection −0.17; the short-alpha version is the only design positive in both windows: **selection +0.13, holdout +0.94, full five years +0.35 (2.5% p.a., vol 7.9%, max drawdown −17.9%)**; the deployable 95-name variant gives the same numbers.

## Round 9 — strategy portfolio (pre-registered; result decides deployment)
- **T: ETF time-series momentum** (Moskowitz-Ooi-Pedersen): SPY, QQQ, IWM, TLT, IEF, GLD, SLV, USO, UUP; long when the
  252-day return is positive, flat otherwise (no shorting, so no 6% borrow); inverse-volatility weights; weekly.
  Sharpe 0.90 selection / 1.05 holdout / **0.93 full**, max drawdown −5.6%.
- **P = T + D**, weekly risk parity on trailing 126-day volatility (strict 2-day lag), scaled to the 90% margin cap.
  Correlation(T, D) = −0.29. **Sharpe 0.82 selection / 1.75 holdout / 1.02 full five years**, 5.2% p.a. at 5.1% vol,
  max drawdown −6.6%, no losing calendar year. Deployable variant (88 shorts so that shorts + 9 ETFs + 1 discretionary
  position ≤ 100 positions): **0.78 / 1.70 / 0.98**, max drawdown −7.5%.

## Conclusion
Across 53 single-strategy trials, estimated models (LASSO, ENet, Ridge, ICIR) do not hold up out of sample; the most
robust single design is the no-estimation, broadly diversified short-alpha book (five-year Sharpe ~0.35, regime-dependent).
The Sharpe of the *strategy portfolio* is maximised not by more fitting but by diversification: adding a literature-spec
ETF trend sleeve that is negatively correlated with the short book lifts the five-year Sharpe to ~1.0, positive in both
the selection window and the holdout and in every calendar year. For the competition: SPY (and any ETF) ≤ 38% of initial
assets (40% rule) with the remaining hedge in QQQ, and total positions ≤ 100.

## Files
- `reports/lab/registry_val.csv` — every trial, selection-window metrics.
- `reports/lab/FINAL.json` — the pre-registered pick, validation vs. holdout, deflated Sharpe.
- `research/lab_*.py` — factor library, cache, experiment runner, fixed pipeline, final selection.
