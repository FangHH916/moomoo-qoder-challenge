# Round 12 pre-registration — factor search for the short book until no single change helps

Written before any round-12 result exists. Round 11 stopped by rule; this round instead searches the
factor set of the D77 composite until it reaches a local maximum of the portfolio Sharpe, so that
"no further factor change improves P*" is demonstrated rather than assumed.

Search space: the equal-weight composite behind D77 (round-7 construction: cleaned, industry +
size neutralised factors, missing -> 0, tradable universe). Start S0 = the 11 literature factors
(bm ep sp mom12_1 ivol60 roe gpa ag accr nsi ear). Candidates = the other 54 cached price/volume
factors, each oriented by the sign of its selection-window 5-day rank IC (reports/lab/screen.csv,
computed on 2021-11-18 -> 2025-11-17 only).

Objective: selection-window Sharpe (2021-11-18 -> 2025-11-17) of the whole portfolio, i.e. T9 + D77(S)
combined exactly like P* (Ledoit-Wolf minimum variance, weekly, 2-session lag, scaled to a 90% peak
margin, portfolio financing). Everything except the factor set is frozen.

Procedure: greedy stepwise search. At each step evaluate every single move (add one candidate, or
drop one member while at least 3 remain); take the best move only if it raises the selection Sharpe
by at least 0.02; stop when no move does (local maximum) or after 8 steps. Holdout metrics
(2025-11-18 -> 2026-11-17) of every evaluation are written to a sealed file and not looked at until
the search has stopped; then they are reported for the final set and for P* only.

Report: the path of moves, the final set, selection / holdout / full Sharpe of the final portfolio and
of P*, and the deflated Sharpe of the final selection-window result over all evaluations.

Deployment: P* stays deployed for the competition. The final set would replace P* only if its
holdout Sharpe is at least P*'s (1.29), its full-period Sharpe is at least 0.10 higher, the realistic
$100k check confirms it, and the user approves.
