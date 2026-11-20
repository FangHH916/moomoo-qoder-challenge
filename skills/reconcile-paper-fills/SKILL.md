---
name: reconcile-paper-fills
description: Reconcile paper order submissions, partial fills, cancellations and actual fees before retries or PnL reporting; expose missing fill identifiers or API coverage as blockers.
---

# Reconcile paper fills

Treat this skill as instructions, not permissions. Use verified read-only account tools in the calling role's isolated Session; never place, cancel or replace orders here. Send all mutations back to Guard's paper-trading-gate. Keep release activation BLOCKED while fill API semantics, pagination, stable IDs or fee coverage are unverified.

1. Verify release/config hashes, mode/account, run/session and the relay's lease. Match orders by broker order ID and account, not symbol alone. Treat an order ID as submission evidence, not a fill. If a submission timed out, search authoritative order state before any retry.
2. Obtain validated open-order, detail and fill history reads with complete pagination/time coverage. Distinguish cumulative order quantities from incremental fills. Reconcile requested, filled, cancelled and remaining quantities; unknown terminal state blocks replacement. Partial fills leave a residual exposure and may invalidate the audited plan.
3. Deduplicate fills by account_id/order_id/fill_id. Do not synthesize an authoritative fill ID or count partial-fill events as multiple executed orders. If stable identity is unavailable, retain unresolved observations and block complete-reconciliation claims. Resolve event-ID conflicts through the relay, never overwrite history.
4. Record event_id, timestamp, mode, account_id, run_id, session_id, order_id, fill_id, symbol, side, quantity, price, commission, platform_fee, regulatory_fee, slippage_bps and arrival_price. Mark every unverified value null, never zero; include unverified_fields. Associate per-order fees once, not once per partial fill. Keep fee estimates separate from verified actual charges and disclose financing/borrow accrual separately.
5. Compute signed adverse slippage only with verified arrival price and side: buys use (fill / arrival - 1) * 10000; sells use (arrival - fill) / arrival * 10000. Otherwise retain null. Derive PnL only with reconciled cash, positions, fills and complete cost coverage; label partial/estimated results clearly.
6. Submit idempotent structured observations to the trusted relay, the sole durable append writer of journal/fills.jsonl and journal/decisions.jsonl. Require flush/fsync acknowledgement before treating an event as durable. Journal failures halt new mutations.
7. Return RECONCILED, PARTIAL or UNKNOWN plus residuals, evidence references and missing capabilities. Require a new plan/audit/approval when residual state changes intended orders. Never hide stale state by blind retry, market order, Bash/network fallback or trading CLI.

Use `agents/templates.json` for the full journal contract. Preserve the distinction between observed fills, estimates and unverified fields in every report.
