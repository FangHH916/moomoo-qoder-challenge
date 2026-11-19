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

## Round 10 — systematic portfolio-level search (pre-registered, 32 combinations)
Sleeves T9, T21 (21 cross-asset ETFs, 1/3/12-month trend), RP (static risk parity), D77 (77-short book); sets
{T9,D77}, {T21,D77}, {T21,D77,RP}, {T21,RP}; methods inverse-vol, Ledoit-Wolf minimum variance, ERC, shrunk max-Sharpe,
each with/without a volatility-managed overlay. Weights always from trailing data (2-day lag); portfolio-level financing
at 4.8% on long exposure above 100% charged.
- **Pick: T9 + D77, minimum variance — Sharpe 0.98 selection / 1.29 holdout / 1.04 full five years**, 6.0% p.a. at 5.7%
  vol, max drawdown −5.6%, every calendar year positive. Deflated Sharpe 0.78 (32 combinations) / 0.65 (all trials).
- Broader trend universe (T21), static risk parity and the vol overlay all lowered Sharpe.
- Competition cap: peak SPY-equivalent weight 0.76 → either scale the whole book by ~0.5 (Sharpe unchanged, ~3% p.a.)
  or route exposure above 38% to DIA/QQQ.

## Deployment check (2026-11-19) — P* adopted
- `research/r10_deploy.py reference` re-runs the pick while recording every ticker weight and reproduces
  `daily_R10_pick.csv` exactly (max abs difference 0 for sleeve returns and weights, 3e-18 for portfolio returns).
- **Realistic $100k execution** (`r10_deploy.py realistic`): whole shares at the next open, $0.99 per order charged
  once at portfolio level (the ideal backtest scaled fixed fees with sleeve weights and charged nothing for
  re-weighting), 5/2 bp slippage, borrow, financing, competition caps with SPY overflow routed to DIA then QQQ.
  Execution rules fixed in advance, not tuned: re-size a position only when it is off target by > 0.25% of equity;
  between rebalances trim any ticker above 39% of initial assets back to 38% and cut margin above 95% to 90%.
  **Sharpe 0.93 selection / 1.55 holdout / 1.05 full five years, 6.5% p.a., max drawdown −6.8%**, every calendar
  year positive; fees $380–560 a year; at most 84 orders a day and 87 positions; SPY never above 39.1% of initial
  assets. Without the trade band (re-size everything at every rebalance) fees triple and Sharpe falls to 0.97.
- Today's book is mostly ETF trend (minimum-variance weight on D77 is 11%): about UUP 29%, SPY 22%, IWM 8%, QQQ 6%,
  USO 3%, and 77 shorts totalling 6% (≈$80 each), margin ≈ 38%.

## Round 11 — third-sleeve candidates (pre-registered; P* stays deployed regardless)
- Candidates on top of P* with the frozen minimum-variance method: SEC3 (industry momentum, top 3 of 11 SPDR
  sector ETFs by 12-1 month return, Moskowitz-Grinblatt) and TOM (turn-of-the-month SPY). B0 = P* reproduced
  exactly as the control.
- Full / selection / holdout Sharpe: **B0 1.04 / 0.98 / 1.29**; +SEC3 1.03 / 0.92 / 1.45; +TOM 0.80 / 0.53 / 2.26;
  both 0.81 / 0.53 / 2.27. SEC3 alone is decent (0.79) but highly correlated with T9's equity legs, so it only adds
  noise. TOM has little variance (in the market ~20% of days), so minimum variance over-weights it, and its weak
  selection-window return (Sharpe 0.04) drags the portfolio down.
- No candidate meets the decision rule (+0.10 full Sharpe and better in both windows). P* stays; the search ends.
  Deflated Sharpe of P* over all trials ever run (including rounds 10–11): 0.60.

## Round 12 — factor search for the short book to a local maximum (pre-registered)
- Greedy stepwise search over the D77 composite's factor set (start: the 11 literature factors; candidates: the other
  54 cached factors, oriented by their selection-window IC). Objective: selection-window Sharpe of the whole
  portfolio (T9 + D77, minimum variance, everything else frozen); a move is taken only if it adds ≥ 0.02.
  Holdout metrics were sealed until the search stopped.
- 258 evaluations, 4 steps (drop roe; add −pv11, turnover variability; add −log_price, share price), then no single
  add/drop helped by ≥ 0.02: a **local maximum at selection Sharpe 1.35** (from 0.98). Deflated Sharpe of that
  selection-window result over the 258 evaluations: 0.98.
- **Holdout: 1.18 vs P*'s 1.29** (full five years 1.31 vs 1.04, inflated by the search window). The D77 sleeve alone
  went from −0.00 to 0.61 in selection but from 0.85 down to 0.70 in the holdout. The in-sample gain did not carry
  out of sample, and the two added signals have no strong prior (share price also tilts shorts toward high-priced
  names, which a $100k account cannot short in whole shares at ~$80 per name).
- Pre-registered rule (holdout ≥ P*, full +0.10, realistic check, user approval): not met. P* stays deployed.

## Conclusion
Across 53 single-strategy trials, estimated models (LASSO, ENet, Ridge, ICIR) do not hold up out of sample; the most
robust single design is the no-estimation, broadly diversified short-alpha book (five-year Sharpe ~0.35, regime-dependent).
Rounds 11–12 searched beyond P* (extra sleeves; a factor search run to a local maximum of the in-sample
Sharpe, 1.35) and neither improved the holdout, so the out-of-sample evidence puts the attainable Sharpe for
this data at about 1.0–1.3, with P* the most robust choice.
The Sharpe of the *strategy portfolio* is maximised not by more fitting but by diversification: adding a literature-spec
ETF trend sleeve that is negatively correlated with the short book lifts the five-year Sharpe to ~1.0, positive in both
the selection window and the holdout and in every calendar year. For the competition: SPY (and any ETF) ≤ 38% of initial
assets (40% rule) with the remaining hedge in QQQ, and total positions ≤ 100.

## Files
- `reports/lab/registry_val.csv` — every trial, selection-window metrics.
- `reports/lab/FINAL.json` — the pre-registered pick, validation vs. holdout, deflated Sharpe.
- `research/lab_*.py` — factor library, cache, experiment runner, fixed pipeline, final selection.
- `research/r10_deploy.py` — deployed strategy P* (reference replay, realistic $100k simulation, live targets);
  `reports/lab/R10_DEPLOY.json`, `R10_REALISTIC.json`, `R10_reference_targets.csv` (replay-test fixture).
