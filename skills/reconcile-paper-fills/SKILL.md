---
name: reconcile-paper-fills
description: Reconcile P* paper submissions, partial fills, cancellations and actual fees for account 5832812 before any retry, cancel or end-of-day report, and turn what does not reconcile into a Slack order anomaly. Use from the Executor session after each round and from the Risk Auditor's 30-minute check. Read-only: never place, cancel or replace an order here - mutations go back to the Executor's gate-passed plan. Expose missing fill identifiers, unresolved status codes and fee coverage as blockers rather than assuming them.
---

# Reconcile paper fills

Treat this skill as instructions, not permissions. Use the verified read-only account tools of the calling
role's own session, on the raw `sim_trade_history_order_list`, `sim_trade_position_list` and
`sim_trade_cash_info` responses saved to files. `live/golive.py record`, `reprice` and `reconcile` are the
deterministic half of this work and are pinned inside the `pstar-executor` skill; this file is what the role
around them has to understand.

1. Bind first: verify mode/account, date, run/session, the release and config hashes, and which lease held the
   switch. Match orders by broker `order_id` and account, never by symbol alone, and never merge test
   `5322438` with competition `5832812`. An order id is submission evidence, not a fill.
2. Resolve every row against the status codes the broker reports. Codes 4 (filled) and 5 (cancelled, no fill)
   are observed; 1/2 working, 3 partial and 6 rejected are documentation-derived, and any code the run cannot
   resolve stays `UNKNOWN`. Unknown terminal state blocks a replacement: `plan` and `record` refuse rather than
   risking a duplicate. A filled order, a cancelled-without-fill order and a partial that was then cancelled
   are finished facts, so they never hold the next round.
3. Reconcile requested, filled, cancelled and remaining quantities, and keep cumulative order quantities apart
   from incremental fills. Partial fills leave a residual exposure and invalidate the audited plan: report the
   residual, do not net it away or round it to zero.
4. Deduplicate by `account_id`/`order_id`/`fill_id`. Do not invent an authoritative fill id, and do not count
   one partial-fill event as several executed orders. Where stable identity is unavailable, keep the unresolved
   observation and refuse a "fully reconciled" claim.
5. Record `event_id`, timestamp, mode, account, run/session, `order_id`, `fill_id`, symbol, side, quantity,
   price, commission, platform and regulatory fees, `slippage_bps` and `arrival_price`. Every unverified value
   stays null and is listed in `unverified_fields`; associate a per-order fee once, not once per partial fill,
   and keep financing/borrow accrual separate from verified charges.
6. Slippage only with a verified arrival price and side: buys `(fill / arrival - 1) * 10000`, sells
   `(arrival - fill) / arrival * 10000`, otherwise null. Derive PnL only from reconciled cash, positions, fills
   and complete cost coverage, and label partial or estimated results as such.
7. Report as an order anomaly, in the same breath as the numbers, anything on this list: a broker
   rejected/failed order, an order working more than 30 minutes, a partial fill, an order or position in a
   symbol outside the allowed book, an order without the `pstar-` tag that is not discretionary, and orders
   used above 80% of the daily or competition limit. `skills/pstar-risk-check/scripts/risk_check.py` computes
   the same list from the same raw reads, and `reconcile` writes what it can see into
   `reports/live/golive/<date>/<account>/alerts.json`. An anomaly is posted to Slack; it is never quietly
   retried away.
8. Durability: the journal is append-only and written by the tooling (`journal/store.py`), with flush/fsync
   before an event counts. A journal failure stops new mutations. Never rewrite historical evidence.
9. Return RECONCILED, PARTIAL or UNKNOWN with residuals, artifact references and the missing capabilities, and
   require a fresh `evidence` + `plan` before anything else goes out. A schedule gap is not a licence to
   resend blind, and a stale read is not a reason to use a market order.
