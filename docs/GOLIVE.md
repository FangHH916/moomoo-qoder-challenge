# Go-live runbook — P\* `pstar15` (written 2026-11-20, prompt 07; automatic execution 2026-11-21, prompt 09)

Stage A smoke test on paper account **5322438** (mode `test`), then the ENTRY batch on **5832812**
(mode `competition`). US regular session opens 09:30 ET = 22:30 SGT; execution starts 09:35 ET per PRD
R7, never at the opening auction, and the gate's own window closes at 15:45 ET.

Since 2026-11-21 the run is **automatic**: the agent submits every ticket the deterministic gate passes,
in plan order, without stopping to ask. `ARM`, `CONFIRM` and `GO` are gone. What replaced them is the
pause switch below, the Slack alerts, and the fact that every order still passes R0–R21 first.

`live/golive.py` never places an order. It reads raw MCP responses from files, writes normalised
artifacts and tickets, and refuses anything it cannot prove.

## Who does what

- **Chat agent (the only MCP caller):** call the read-only paper/quote tools, save each raw response
  verbatim to a file, run `live/golive.py`, paste `plan.json` payloads into `sim_trade_input_order` /
  `sim_trade_cancel_order` **unchanged**, save each response, then feed the files back. Nothing else.
  It submits without asking, and it sends the `pause` cancel list as soon as `pause` prints it.
- **Deterministic Python:** parsing, sizing, pricing, the R0–R21 gate, duplicate protection,
  reconciliation, and the computation of every field in `evidence.json`. No order quantity or price is
  ever computed by hand or edited in a payload, and no evidence flag is ever typed.
- **The user:** `PAUSE` and `RESUME`, from Slack once the listener lands (next prompt) or in this chat
  until then. The user is not in the submit path: the rules gate decides, and a control that only asks a
  human per batch would add latency without adding a check the gate does not already make.

Tools allowed: `sim_trade_account_list`, `sim_trade_cash_info`, `sim_trade_position_list`,
`sim_trade_history_order_list`, `sim_trade_max_buy_sell`, `sim_trade_input_order`,
`sim_trade_cancel_order`, the quote tools. **Never** any `trading_*` / `account_*` tool, no market
orders, no extended hours, no options.

## Files and commands

All commands run from the repository root with `PYTHONPATH=. .venv/Scripts/python.exe -m live.golive …`.
Raw responses and artifacts for a night go in `reports/live/golive/<date>/<account>/`:
`cash.json`, `positions.json`, `accounts.json`, `orders-<hhmm>.json` (one file per history pull),
`quotes.json`, `borrow.json`, `evidence.json`, `response-<ticket>-<attempt>.json`, `alerts.json`, plus
the tool's own `snapshot-<hhmm>.json`, `plan.json`, `cancel-<hhmmss>.json`, `reconcile.json`,
`summary.md`. `--journal` defaults to `journal/`; every step appends a record there. Exit code 2 means
refused or incomplete — read the printed line, do not retry blindly. `--as-of` takes a timezone-aware
ISO timestamp (defaults to now UTC).

The commands are `resume`, `pause` (`halt` is its alias), `snapshot`, `evidence`, `plan`, `record`,
`reprice`, `reconcile`, `alert`. None of them contacts the broker, and none of them needs a `--hold`
list: positions this strategy did not open are protected by the planner (step 3 of the run below).

## The pause switch

`journal/` keeps one durable control row per account, and R9/R16 reject every strategy order while that
row says halted. A store with no row at all reads `{"halted": true, "reason": "Never resumed"}`, so a
fresh account is stopped until it is resumed.

```
python -m live.golive resume --account 5832812 --source "user in chat"
python -m live.golive pause  --account 5832812 --source "slack PAUSE 1763912345" --orders orders-1035.json
python -m live.golive halt   --account 5832812 --source "…" --orders orders-1035.json   # alias of pause
```

- Neither command accepts a typed reply, and neither needs one. `--source` is what tells an auditor that
  a Slack message — not a stray keystroke — moved the switch, so it is required and is journaled verbatim
  in the reason line (`paused at <ts>: slack PAUSE 1763912345`) and in the `GOLIVE_PAUSE` /
  `GOLIVE_RESUME` decision.
- Both accept only the current config mode's account: 5322438 while `mode` is `test`, 5832812 while it is
  `competition`. The other mode's account is refused before the store is touched, because R1 would reject
  everything that account did next anyway.
- `pause` with `--orders` also writes `cancel-<hhmmss>.json`: every still-live order whose client order id
  starts `pstar-`, as complete `sim_trade_cancel_order` payloads (`acc_id`, `market`, `order_id`). The
  agent sends those immediately, without asking, and then reconciles. Filled, cancelled and
  non-`pstar-` rows are not in the list; the history file is checked for the account id first, so a file
  from the other account cannot pause the wrong book.
- A paused account rejects every ENTRY ticket (R9 `FLATTEN_HALT` plus R16 `HALT`) and still passes a
  `FINAL_EXIT` or `EMERGENCY_EXIT` **reduction**. That is deliberate: the scheduled final exit on
  2026-11-27 closes the book even if the user paused the strategy the day before.
- Nothing in this prompt touches a switch: account 5832812 has been running since 2026-11-20T18:47Z
  (`halted=0`; its reason line still carries prompt 07's `armed by the user in chat` wording, and nothing
  rewrites a durable record) and stays running; 5322438 is `halted: true / "Never resumed"` and holds the
  user's own NVDA put, which the strategy must never trade.
- What the 2026-11-20 run left at the broker, so the 2026-11-23 run does not start from a guess: one
  strategy order is on record — **BUY 30 IWM at 259.64**, `client_order_id
  pstar-2026-11-19-IWM-BUY-30-adbd263ed38d`, `order_id 3075765`, submitted 2026-11-20T18:48:47Z and
  `status 4` (filled) in both the 18:48 and 18:50 history pulls, with `traded_quantity` and price returned
  as null. A second single-ticket plan, BUY 10 QQQ at 620.71, has no `response-*.json` beside it, so
  nothing here records it as submitted; only today's history can say. The rest of the book was planned in
  parts and never put in front of a single `plan` for all names, which is exactly what step 3 now forbids.
  The run re-reads the history first, so R12 decides from the broker's answer and not from this paragraph:
  a held position simply drops out of the diff, and an order that turns out to be still working is an
  anomaly to report, not something to duplicate.

## Automatic run

1. `resume`/`pause` state first: read the control row (or run any command and look at what R16 says). If
   the account is paused, send nothing and stop — a paused run is the switch working, not an error.
2. Borrow checks for every planned short: `sim_trade_max_buy_sell`. No freshness limit, so do them first.
3. Then, back to back: `sim_trade_account_list` + `sim_trade_cash_info` + `sim_trade_position_list` +
   `sim_trade_history_order_list` → `snapshot`; `quote_market_snapshot` for the whole book in as few
   calls as possible; `evidence --trigger "user started the run"`; **one `plan` for all names**. Never
   plan one symbol at a time: a single call is what makes the order budget, the position cap and the
   gross/margin projections describe the batch the gate actually approved.
4. Submit every ticket in `plan.json`'s `submit` list, in plan order, without asking; `record` each
   response file. Anything the gate left out of `submit` is reported as unsent with its rule ids.
5. If `plan` reports R11 or an expired approval, refresh and re-plan automatically: re-run `snapshot`,
   `evidence` and `plan`. Those commands send nothing, so a refresh costs time, not safety.
6. Every 5 minutes while anything is working: check the pause switch, fetch today's order history, and run
   `reprice` for what has not filled — it re-gates the remaining quantity through the same rules. At most
   3 reprice rounds per name (`MAX_REPRICE_ROUNDS`), then the ticket becomes `skipped_unfilled`. The
   per-day and competition order caps are unchanged by any of this.
7. Stop at 15:45 ET or when every ticket is filled or skipped, then cancel anything still working,
   `reconcile`, and post the summary in this chat: filled, skipped, unfilled, gross long and short,
   margin, largest ticker, fees, and orders used against both the 100/day strategy budget and the 200
   competition cap.
8. Anomalies go into that summary **and** into `reports/live/golive/<date>/<account>/alerts.json` — see
   the list below. `reconcile` writes the ones it can see; `alert` records the ones only the agent sees.

### Stage A, and why the 2026-11-23 run is not it

The four-ticket smoke plan (`plan --smoke` on 5322438 in mode `test`: BUY 1 IWM, SELL 1 short, then the
two closes) still exists and still gates exactly like a real ticket. It is not part of the 2026-11-23 run:
the deployed config is on `competition`, 5322438 holds the user's own discretionary NVDA position that the
strategy must never touch, and flipping `mode` back would republish the targets contract for a third time.
Run it only if the user asks for it, and then in mode `test` with the same submit-without-asking rules.

### Why this chat may stop before an MCP call, and what controls it

Prompt 09 asked, in case this Desktop chat would stop to ask before an MCP tool call during the run, which
permission settings cause it. Recorded here so the 2026-11-23 run does not discover it 75 quotes deep:

- **The session permission mode is the whole gate.** A run makes roughly a dozen MCP calls, and in the
  default (ask) mode every one of them stops until it is approved. The mode is chosen per session in the
  chat's own controls, not in a file in this repository. Switching it to an auto-approve mode, or to the
  mode that bypasses permission prompts, is the user's decision and their keystroke; nothing in the
  strategy can or should change it.
- **Nothing is pre-allowed by configuration today.** As of 2026-11-21 the user settings file
  (`~/.qoder/settings.json`) carries only `enabledPlugins` and `mcpServers` — no permission rules — and
  there is no `.qoder/` directory and no hook configuration in this project, so no rule grants
  `mcp__moomoo__sim_trade_input_order` in advance and no `PreToolUse` hook can intercept one either. If a
  rule is added later it will most likely be an "always allow" choice made on the prompt itself, which
  persists for that tool.
- **A mode that auto-approves still does not make an order safe.** The deterministic gate, the account
  check, the caps and the pause switch are what bound the run; the permission mode only decides who types
  the word "yes". This is exactly the trade the 2026-11-21 decision accepted, which is why `docs/GOLIVE.md`
  keeps the pause switch and the alerts as the human controls.
- **What stops the run regardless of permissions** is the stop-and-report list below: an account mismatch,
  a whole-batch gate failure, stale targets, three consecutive MCP errors, a drawdown halt, or anything
  unexpected. Those are not permission prompts and approval mode does not clear them.

## Order of calls, and the 300-second clock

R11 refuses a plan whose account snapshot or quote is more than `account_max_age_seconds` /
`quote_max_age_seconds` old. Both were 60 s and are 300 s as of 2026-11-21, because moomoo's
`update_time` lags roughly 30 s on its own and relaying ~75 quote rows takes longer than a minute: the
old limit failed the batch on plumbing rather than on staleness. R7 still keeps every limit price inside
25 bps of the quote it was made from, so a slightly older quote cannot quietly produce a bad price.

`evidence` stamps its approval with a 240 s window (R14's frozen TTL is 300 s, so R14 stays the
authority). When an approval or a quote expires, re-run `evidence` and `plan` — neither sends anything.
There is no expiry rule that waits for a human reply.

## `evidence.json` — computed, never typed

```
python -m live.golive evidence --account 5832812 --snapshot snapshot-093600.json \
    --orders orders-093600.json --trigger "user started the run" --out evidence.json [--working <code>]
```

Python computes or fills every field; the chat agent does not write this file by hand. The cloud agents
have no order tools and are not part of it: in automatic local mode the independent check is this
deterministic golive/gate run, and the human controls are the pause switch and the Slack alerts.

| field | how the value is obtained |
|---|---|
| `release_verified` | git: no uncommitted `.py` under `live/`, `rules/` or `journal/` at the checked-out commit, whose hash is recorded in `release.commit`. Edits to the config's `mode` and `execution_enabled` deliberately do not count, because R10 already ties the targets to the config checksum. No usable git answer means false, never a pass. |
| `reconciled` | the fresh history has no `WORKING`, `PARTIAL`, `UNKNOWN` or `UNREADABLE` row for this account; otherwise false, with those rows listed in `reconcile.unresolved`. A filled order, a cancelled-without-fill order and a partial fill that was then cancelled are facts, not unknowns, so they never hold the next round. |
| `snapshot_verified_by_relay` | the file has the shape only `snapshot` writes, the relay line is intact, each raw response it names still hashes to what was recorded, and `sim_trade_account_list` asserts this account (i.e. it was captured with `--accounts`) |
| `lease_verified` | true, on the recorded basis: local mode, decided by the user 2026-11-20 — this Qoder Desktop chat is the only order sender and no cloud agent exists or has order tools |
| `permission_policy_verified` | true, on the recorded basis: automatic execution decided by the user on 2026-11-21 — every order passes the deterministic gate, and the user can pause at any time with Slack `PAUSE` |
| `approval` | `approval_id` `golive-<date>-<account>-<hhmmss>`, `issued_at` now, `expires_at` now + 240 s, `verified_by_relay` true, auditor session `golive-deterministic-checks`, guard session `qoder-desktop-chat`, `confirmed_by` `user-started run`, `trigger` from `--trigger`, basis "the independent check is the deterministic golive/gate run; the human controls are the pause switch and the Slack alerts" |
| `borrow.easy_to_borrow_proxy` | `sim_trade_max_buy_sell.max_sell_short_qty` — the accepted proxy: a short passes R5 only when that quantity is at least its planned size, and an un-borrowable name is skipped, never replaced |

Exit code 0 means all five assertions came out true; 2 means at least one is false and `plan` will be held
by R11/R12/R14/R17. `bases` and the `release` / `reconcile` / `snapshot` blocks say which one and why, in
the same words the printed line uses.

## Price policy

Marketable limits, as instructed: buy = ask + 0.05%, sell = bid − 0.05%, rounded to the cent **away from
mid** and at least one cent through the touch; `order_type` limit, `order_trade_time_type = 1` (regular
session only), DAY. The prompt-06 planner supplies no limit price of its own (it sizes quantities and
leaves pricing to the gate), so this marketable-limit policy is the one used, and every ticket carries it
in `price_basis`. `arrival_price` is the same-side touch the limit was made from and is what slippage is
measured against.

## Reprice rounds, approvals and duplicates

- A ticket that already carries an `order_id` is never planned or sent again: `plan` refuses to overwrite
  such a `plan.json` (use `--out`), `record` refuses a second response for the same ticket, and a resend
  requires `--orders <fresh history>` and refuses if a matching order already exists.
- Every reprice round needs its own approval: re-run `evidence` (new `issued_at`/`expires_at`, and it
  re-reads the fresh history) before `reprice`. R14/R15 spend the approval per order, so reusing the
  previous file fails the gate rather than silently resending. In automatic mode the agent does that
  refresh itself — it is the step that replaced `CONFIRM`.
- Tickets rejected by the gate stay out of `submit` and are listed with their rule ids in
  `summary.rejected_tickets`; the plan's exit code is 2 when nothing passes.

## Order status codes

`sim_trade_history_order_list` returns an integer `order_status`. The 1–7 enumeration below is
documentation-derived knowledge, carried in the 2026-11-21 instruction; it is not machine-verified in this
delivery. The MCP metadata for that tool publishes only its request fields (`acc_id`, `market`, `next_key`,
`page_size`, `time_begin`, `time_end`) and no status enum, so nothing reachable from here confirms the wording
of a code. What local evidence does reach is narrower: codes 4 and 5 appear in the 2026-11-20 history of
5322438, there with `cum_qty == qty` and `cum_qty == 0` respectively; 1, 2, 3, 6 and 7 have never been seen
on a real book. `live/golive.py` maps each one exactly once:

| code | moomoo's wording | `order_state` | effect |
|---|---|---|---|
| 1 | pending new | `WORKING` | holds the batch (R12), reprices later, appears in a `pause` cancel list |
| 2 | submitted | `WORKING` | as above |
| 3 | partially filled | `PARTIAL` | as above, with `remaining` as the reprice quantity |
| 4 | filled | `FILLED` | resolved; a 4 with less than the ordered quantity is contradictory evidence and stays `UNKNOWN` |
| 5 | cancelled | `CLOSED_NO_FILL` | resolved when nothing filled; a cancelled order with fills is settled by the quantity columns |
| 6 | rejected | `CLOSED_NO_FILL` | resolved, and alerted as `broker_rejection` |
| 7 | partially filled then cancelled | `CANCELLED_PARTIAL` (0 filled ⇒ `CLOSED_NO_FILL`) | resolved: it does not make `reconciled` false, does not block the next reprice round and needs no second cancel |

`cum_qty >= qty` outranks the code, so a complete fill is a fill whatever the status says. A code outside
the 1–7 enumeration — `0` appears in real history rows but is in no table anyone has shown us — is reported as
`UNKNOWN`: golive will not guess, `unresolved_status_codes` lists it, R12 holds the batch and the command
exits 2. If, and only if, the operator confirms from the app that such a code means a live working order,
pass it explicitly (e.g. `reconcile … --working 0`), which reclassifies those rows as `WORKING`. Reconcile
then reports them under `open_orders` — still live, so the batch is not `complete_reconciliation`.

## The 2026-11-23 catch-up

The 2026-11-20 session was the armed-but-not-run first day, so the sealed 2026-11-19 rebalance book is
run at the 2026-11-23 open instead of being thrown away. `live/config.pstar.json` carries that as one
dated exception, decided by the user on 2026-11-21:

```json
"max_target_age_sessions": 0,
"target_age_exceptions": [{"day": "2026-11-23", "signal_date": "2026-11-19", "max_age_sessions": 1,
                           "decided_by": "user 2026-11-21", "reason": "…"}]
```

R10 reads a row only when the running session day, the book's `signal_date`, a whole non-negative
`max_age_sessions` and a non-empty `decided_by` all match, so only 2026-11-23 gets the one-day-old book;
2026-11-24 and every other day keeps `max_target_age_sessions: 0`. Two rows covering the same day and
signal date raise instead of picking whichever comes first in the file. R19 (the rebalance cadence rule)
needed no change: the 2026-11-19 run *is* the scheduled rebalance.

Same decision, same file: `min_etf_market_value` 500,000,000 → 300,000,000, because UUP at ~$483M
otherwise blocks the largest leg of the book (28.92% of equity), and the two age limits 60 s → 300 s.
`quote_max_age_seconds` and `account_max_age_seconds` change `config_checksum`, so
`live/targets/competition/2026-11-19-pstar.json` was republished against the new config the way prompt 07
Part 2 published it:

```
python -m live.pstar --snapshot reports/live/golive/2026-11-20/5832812/snapshot-183422.json \
    --no-run --as-of 2026-11-20T18:34:22+00:00 --out live/targets/competition/2026-11-19-pstar.json
```

It printed `READY`, and the only fields that differ from the superseded contract are `config_checksum`
(`c177846f…` → `97a92b4e…`), the contract's own `checksum` and `generated_at`. All 82 weights, dollars,
sleeves, sleeve weights, diagnostics and consistency records are identical — the book did not change, the
configuration it is bound to did. `write_targets` refuses to replace published bytes, so the superseded
file was moved aside rather than overwritten.

## Anomalies

Everything on this list goes into the run summary and into `alerts.json` for the Slack feed; none of them
stops the batch on its own, and none of them is a reason to improvise an order.

- a broker rejection (status 6, or a response the tool could not accept);
- a ticket that fails the gate for any reason other than an expected skip — under one whole share, or no
  borrow — so an R9/R16 rejection while paused is the switch working and is not re-alerted per ticket;
- an order still unfilled after the last reprice round, or filled only in part;
- a fill more than 25 bps from the quote its ticket arrived on;
- a position in the account that is not in the targets, or a position in any account other than 5832812;
- orders used above 80% of either the 100/day strategy budget or the 200 competition cap;
- a tool error that stops the run.

`reconcile` writes the ones it can prove from the files it was given (`alerts.json` gains a row per
anomaly, deduplicated by kind + symbol + order id, and `summary.md` carries the same lines). The agent
records what only it can see:

```
python -m live.golive alert --account 5832812 --kind tool_error --symbol IWM \
    --detail "quote_market_snapshot timed out twice" --file response-12-2.json
```

`--kind` is one of `broker_rejection`, `gate_rejection`, `unfilled`, `partial_fill`, `slippage`,
`unexpected_position`, `order_budget`, `tool_error`; the row keeps the hash of the raw file that proves
it, and each alert appends a `GOLIVE_ALERT` decision.

## Stop conditions (report, do not improvise)

Account mismatch · whole-batch gate failure · stale or non-`READY` targets, or a config/targets checksum
mismatch · 3 consecutive MCP errors · a drawdown halt · the pause switch latched (that one is a stop with
nothing to report beyond "paused") · a rejected short sale in Stage A · any unexpected response shape.

## Known gaps in this release

- **Sub-$4 tickets are rejected by R7.** The marketable limit that is one cent through the touch is more
  than 25 bps from the reference quote below roughly $4.00; the frozen band wins, the ticket is gated out
  and counted in `summary.price_band_rejections`.
- **Fees are never read from an order row.** The paper order row has no fee field, so `actual_fees`,
  `commission` and per-fill identity stay `null`; only estimated fees ($0.99/order) and the cash-delta
  observation are reported.
- **`sim_trade_cash_info` / `sim_trade_position_list` echo no account id**, so account binding is
  asserted from `sim_trade_account_list` (`--accounts`); without it the snapshot says the binding is
  unverified. Unknown broker fields stay `null` and are listed in `unverified_fields`, never zero.
- **The Slack listener is not in this release.** `pause`/`resume` and `alerts.json` are the local half of
  the control loop; the message handler that turns a `PAUSE` into the command, and the cloud agent that
  runs the 2026-11-27 final exit on a schedule, arrive in the next prompt. Until then `--source` records
  whatever text the user gave in this chat.

Tests: `PYTHONPATH=. .venv/Scripts/python.exe -m unittest live.test_golive`.
