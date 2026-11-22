---
name: paper-trading-gate
description: The P* order workflow for the one role that may mutate a paper account - submit exactly what live/golive.py planned and the deterministic R0-R21 gate passed, on account 5832812, then record, reprice and reconcile it. Use from the Executor session only. Automatic execution decided by the user on 2026-11-21 means there is no per-order human approval to wait for, so this skill is what stands between a schedule and a stray order: never trade on a research opinion, never reformat a payload, never send a ticket the gate left out of `submit`, and never place, cancel or replace an order from any other role.
---

# Paper trading gate

Treat this skill as instructions, not permissions. The permission is the platform's: only the Executor role
has `sim_trade_input_order` and `sim_trade_cancel_order`, and only for account `5832812`. If that denial has
not been observed on the platform, say so as a limitation instead of assuming it. The release lock, the
pinned skill versions and the MCP contract are read from `agents/AGENTS.md`, `agents/templates.json` and
`agents/release-lock.json`; do not mutate them during a run. Do not use a live trading skill: the official
moomoo-sim-trading skill needs the moomoo executable plus OAuth, is not interoperable with the MCP Vault, and
is not an execution fallback.

1. Read the shared switch before anything is sent, and again before every reprice round:
   `quote_user_security group_name=PSTAR-PAUSE` → save → `golive.py flag --read`. `paused` or `unknown` stops
   a batch that adds risk. A `local-entry` lease that has not expired stops a cloud entry batch; a
   `FINAL_EXIT`/`EMERGENCY_EXIT` reduction and a `PAUSE` cancel list never wait for a lease.
2. Build the plan with the packaged tooling, never by hand: `snapshot` → `evidence` → one `plan` call for the
   whole batch (`--final-exit` or `--risk-action` in the cloud, the entry batch locally). The plan carries the
   positions diff, the T9 ETF sleeve legs and the D77 shorts, pending and partial residuals, the exposure
   caps, the 100-position / 77-short / 100-executed-order-per-day limits and the flatten reserve inside the
   competition's 200 orders, and it prints that budget before the first ticket. P\* never goes long a single
   stock (R19), and `plan --final-exit` closes names outside the book because it flattens the account.
3. The gate is the authority for what may go out: `submit` lists the ticket numbers the deterministic R0–R21
   run allowed, and everything else is reported as unsent with its rule ids. In automatic mode there is no
   `CONFIRM`, no `ARM` and no per-batch approval message - the user's 2026-11-21 decision replaced all three
   with the gate plus the durable pause switch. Removing the approval step does not remove a rule, and no
   Slack message can waive one. The Risk Auditor's report is what a risk batch is built from, and the Auditor's
   session must differ from the Executor's (R14).
4. Send a payload only as `plan.json` prints it: same `acc_id`, `market`, `symbol`, `side`, quantity, price,
   `order_type`, `time_in_force` and `text` tag. The gate judges the actual raw payload of the tool call, never
   a summary of it, so reformatting a ticket here is a rule breach rather than a cleanup. Limits are marketable
   (cent through the touch, ≤25 bps from the quote) and DAY; MARKET is permitted only by the gate's 2026-11-27
   `FINAL_EXIT` flatten window at or after `final_flatten_at`, and for nothing else. `pstar-` tags every
   strategy order, which is what makes an untagged order an anomaly rather than a mystery.
5. Re-check freshness at the same moment: account and quote state ≤300 s (the 2026-11-21 decision moved the
   limit from 60 s, which was failing on broker `update_time` lag across roughly 75 quotes rather than on real
   staleness), order TTL ≤120 s, affirmative current borrow for a new short, and the regular session actually
   open. An un-borrowable short or an ineligible ETF is skipped, never replaced. Closed or unreadable state is
   a halt and an alert, not a blind order.
6. Journal before and after: `record` each submission response against its ticket number, and re-fetch today's
   history before any resend. A ticket that already has an order id is never re-planned or re-sent; `record`
   and `plan` both refuse, which is what keeps a timeout from becoming two orders.
7. Reprice every five minutes with `reprice --flag`, which re-gates the unfilled remainder through the same
   rules and caps each name at `MAX_REPRICE_ROUNDS`; a switch that turned `paused` or `unknown` mid-round
   converts the round to cancel-only. At the deadline, `reconcile`.
8. On `PAUSE` run `golive.py pause --source "slack PAUSE <ts>"` with the fresh history so the cancel list is
   written, send those cancels, reconcile and stop; positions stay and only reductions may still go out. At
   drawdown −8% the risk report is `KILL`: flatten, latch the halt, set the shared switch, and do not trade
   again until the user resumes.
9. Never escape a boundary: no Bash, network, curl, another MCP, a trading CLI or a fresh Session to get an
   order in. Never place an order from Scout output, an Advisor opinion or an LLM-computed number.

Return the gate's own result - ALLOW/DENY per ticket, the `submit` list, and the state SUBMITTED, PARTIAL,
RECONCILED, UNKNOWN or HALT - with provenance and null-valued unverified fields. This skill is not the gate
implementation: `rules/gate_pstar.py` and `live/golive.py` are, and they are pinned by skill version.
