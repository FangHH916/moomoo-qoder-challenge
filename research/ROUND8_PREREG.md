# Round 8 pre-registration (written before any round-8 result exists)

Motivation (fundamental law of active management, IR ~ IC x sqrt(breadth)): the round-7
literature-prior composite has the strongest and least-fitted signal (5-day IC 0.018, NW t 2.87,
roughly monotonic deciles on the selection window), but 20-name tail books are dominated by a
handful of high-volatility names. Daily rebalancing made broad books unaffordable in v1; with
weekly rebalancing and a 3x buffer, a broad book is now affordable.

Single change vs. round 7: 100 names per side instead of 20. Scores = round-7 composite
(no estimation). Everything else frozen (weekly rebalance, 5-day smoothing, 3x buffer,
inverse-vol weights capped at 3x equal, vol targeting on new positions, signal guard, SPY beta
overlay, 5 bp slippage, $0.99/order, 6% borrow).

Trials (exactly two): R8_ls_n100 (primary), R8_shortspy_n100 (secondary).
Primary endpoint: holdout Sharpe of R8_ls_n100 after uniform scaling to the 90% margin cap.
Reported whatever the outcome; this is the last research round before deployment.
