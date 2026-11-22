---
name: report-trading-day
description: Produce the P* end-of-day paper report for account 5832812 from reconciled artifacts and the journal - risk breaches, order anomalies, the pause switch and lease state, missing fees, order budget and honest release status - and post it to Slack. Use at 16:10 ET on a trading day, and at 13:10 ET on 2026-11-27 after the scheduled final exit. Every number is read from a file the tools wrote; nothing is estimated in prose, and a gap between two checks is reported as a gap.
---

# Report trading day

Treat this skill as instructions, not permissions. Work from the artifacts already in
`reports/live/golive/<ET-day>/<account>/` (snapshot, plan, evidence, alerts, cancel lists, this report) and the
journal. The Coordinator posts it; the Advisor may give a second opinion on it; neither may change it.

1. Verify mode/account, date, run/session, source/config/target hashes and journal completeness. Separate test
   `5322438` from competition `5832812`, and refuse to describe a test return as competition performance.
2. Summarise equity, daily return, drawdown against the running peak, the T9 ETF sleeve and the D77 short gross
   separately, positions with the short count out of the 77-short cap, margin from
   `0.5 * long gross + 0.6 * short gross`, open orders, and executed-order count against both the 100/day
   strategy budget and the 200-order competition cap - including what the exit rounds spent and what the
   flatten reserve still holds. Show skipped and rejected tickets with their rule ids, expired approvals,
   lease decisions, and what `PSTAR-PAUSE` read at each round. Report each deviation with its cause: an ETF
   skipped by the eligibility screen, a short skipped as un-borrowable, a name that rounded below one whole
   share, and a position the journal does not attribute to this strategy version. Past the 38%-of-initial
   single-name cap or 90% margin between rebalance anchors, report drift under the only two permitted
   reductions (the 39%→38% trim and the >95%→90% margin cut), never as a reason to add exposure.
3. Report submitted, partial, filled and cancelled orders distinctly, from `reconcile`, and list the order
   anomalies the risk check found (rejected order, working >30 min, partial fill, unexpected symbol, untagged
   order, budget past 80%). Unresolved fill ids, fees, slippage and arrival prices stay null with
   `unverified_fields`, never zero. Distinguish estimated from actual costs; do not claim final PnL or Sharpe
   when coverage or sample length is inadequate, and label any short-sample statistic and its convention.
4. Include the drawdown −8% liquidation and latched halt, the pause/resume history with its Slack source line,
   and unresolved final-day exposure. P\* has no daily-loss entry halt, so never report a −2% stop as
   protection. Say plainly that an alert is not a stop order: between the 30-minute checks the book is
   unhedged, and a pause sent during a local round takes effect at the next round.
5. Use America/New_York. Ordinary report 16:10; 2026-11-26 no trading; 2026-11-27 no rebalance, the flatten
   starting 12:15 ET with reprices every 5 minutes, MARKET for anything still open at 12:45 ET
   (= 28 Nov 01:45 SGT), the flat check at 12:50 ET, and this report at 13:10 instead of 16:10. If the account
   is not flat, list every remaining position by symbol, side and quantity rather than calling the exit done.
6. Post what the tools printed, including the 12:00 ET one-line heartbeat and the `CHECK FAILED` text when a
   read failed - a failed check is reported, never left silent. Name the peak-equity basis the report used
   (the file that survives between sessions, the newest end-of-day report, or `max($100,000, today)`).
7. Explain what worked, failed and changed with artifact references, not fabricated outcomes, and separate
   offline specification tests, gate unit tests, cloud prompt evaluations, shadow evidence and observed paper
   roundtrips. Keep unverified cloud evidence UNVERIFIED and release activation BLOCKED until Part B binds it,
   and state the credits/sessions the schedules actually used.
8. Disclose the unchanged P\* limitations: survivorship inside the 77-name universe, static borrow and
   fund-size assumptions, the SEC-filing proxy, whole-share fills at the next open, and that the deployed
   `pstar15` holdout (realistic 1.68) is one pre-registered trial rather than a search result, with deflated
   Sharpe 0.79 over 37 portfolio trials and only 0.43 over all 348. Plan A (`frozen-short-alpha-half`) is
   retired for its −0.30 holdout (deflated Sharpe 0.435) and neither its halved deployment book nor its
   negative holdout may be presented as P\* evidence. A realistic annualised return of 6.45% (`pstar`) or
   7.45% (`pstar15`) makes the remaining six sessions noise. Mark checklist B4/B7 partial: no new
   losing-streak or per-name take-profit overlay. Keep options disabled.

Return a concise report with evidence references and unresolved items. Only the tooling persists the
role-owned artifact and appends journal events; never rewrite historical evidence or the submission checklist.
