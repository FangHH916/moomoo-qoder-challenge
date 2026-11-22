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
- **The user:** `PAUSE` and `RESUME` in Slack #trading-agent, as the whole message, from their own account.
  The cloud Coordinator turns that into the shared flag and replies in the thread; this chat reads the flag
  before the first submit and before every reprice round. Until Part B lands the listener, the same words in
  this chat are the control. The user is not in the submit path: the rules gate decides, and a control that
  only asks a human per batch would add latency without adding a check the gate does not already make.
- **A pause is not a stop order.** It stops the next ticket this tool would have written; a fill that happens
  while the flag is being carried from the phone to the watchlist to the chat is already at the exchange, and
  a local run mid-batch notices the switch at its next round, up to five minutes later.

Tools allowed: `sim_trade_account_list`, `sim_trade_cash_info`, `sim_trade_position_list`,
`sim_trade_history_order_list`, `sim_trade_max_buy_sell`, `sim_trade_input_order`,
`sim_trade_cancel_order`, the quote tools, and — for the shared switch only — `quote_user_security` and
`quote_modify_user_security` on the `PSTAR-PAUSE` group. **Never** any `trading_*` / `account_*` tool, no
market orders outside the final-exit flatten window (see *Final exit*), no extended hours, no options, and
never `op: DEL` on a sentinel.

## Files and commands

All commands run from the repository root with `PYTHONPATH=. .venv/Scripts/python.exe -m live.golive …`.
Raw responses and artifacts for a night go in `reports/live/golive/<date>/<account>/`:
`cash.json`, `positions.json`, `accounts.json`, `orders-<hhmm>.json` (one file per history pull),
`quotes.json`, `borrow.json`, `evidence.json`, `response-<ticket>-<attempt>.json`, `alerts.json`, plus
the tool's own `snapshot-<hhmm>.json`, `plan.json`, `cancel-<hhmmss>.json`, `reconcile.json`,
`summary.md`. `--journal` defaults to `journal/`; every step appends a record there. Exit code 2 means
refused or incomplete — read the printed line, do not retry blindly. `--as-of` takes a timezone-aware
ISO timestamp (defaults to now UTC).

The commands are `resume`, `pause` (`halt` is its alias), `flag`, `snapshot`, `evidence`, `plan`, `record`,
`reprice`, `reconcile`, `alert`. None of them contacts the broker, and none of them needs a `--hold`
list: positions this strategy did not open are protected by the planner (step 3 of the run below).
`flag` adds `flag-write-<hhmmss>.json` (the watchlist payloads) and `switch-hold-<hhmmss>.json` (what a
blocked round leaves behind: the cancel list, the flag write, the command to run), and it caches the read
it was given in `reports/live/golive/state/switch.json`.

## The pause switch

There are two switches and they mean the same thing. The **durable local latch** is a row in `journal/`
that only this tool writes; the **shared flag** is a moomoo watchlist group both this chat and the cloud
read, because a `PAUSE` sent from a phone lands on the cloud Coordinator, and the 2026-11-23 entry runs
here. `pause`/`resume` move the latch and print the writes that move the flag; `flag` reads and writes the
flag directly.

### The durable local latch

`journal/` keeps one durable control row per account, and R9/R16 reject every strategy order while that
row says halted. A store with no row at all reads `{"halted": true, "reason": "Never resumed"}`, so a
fresh account is stopped until it is resumed.

### The shared flag: a moomoo watchlist

`live/ops.json` names the group `PSTAR-PAUSE` and the sentinel symbols it carries. What was measured on
2026-11-22, in the order the prompt asked:

1. **A watchlist flag works.** `quote_modify_user_security` accepts `ADD`/`MOVE_OUT` on an existing CUSTOM
   group and `quote_user_security` reads it back; the local Desktop chat and a cloud session reach the same
   moomoo login through the same MCP, so the write is visible on both sides and it outlives a cloud session.
   Two hard limits came out of the same probes: the MCP **cannot create a group** (`group_name not found`
   for a name that does not exist), so the user creates `PSTAR-PAUSE` once in the app; and it **cannot store
   text** (`invalid symbol` for a made-up code), so the channel carries only which symbols are in the group —
   no timestamp, no source, no expiry string. Removal must be `MOVE_OUT`: `DEL` strips the symbol from every
   group the user has, including their own.
2. **Qoder shared workspace memory / project wiki is not proven.** It is the mechanism the handbook section
   describes, but nothing in this repository shows a cloud Forward session writing a memory this chat then
   read, so it cannot be the control a live order depends on. Part B tests it; until it passes it is not used.
3. **A git file is impossible as delivered.** `git remote -v` is empty: there is nothing for two machines to
   pull. It becomes available only if the user adds a remote and approves both sides pulling it.

So the flag carries enumeration only, and the encoding is:

| sentinel | meaning |
| --- | --- |
| `US.SQQQ` | `paused` — send no new order |
| `US.SOXL` | `running` |
| `US.XLE` | the `local-entry` lease: this Desktop chat trades now |
| `US.XLF` | the `cloud-executor` lease: the cloud Executor trades now |
| one of 12 sector ETFs | the day the lease was taken (Eastern day-of-year mod 12) — the only expiry the channel can hold |

Time, source and reason are not in the flag: they are in the `GOLIVE_FLAG` / `GOLIVE_PAUSE` journal row and
the Slack thread. Both state sentinels present, neither present, two lease tokens, or a lease day token with
no holder all read as `unknown`, and `unknown` blocks entries (it never blocks a `FINAL_EXIT` or
`EMERGENCY_EXIT` reduction). A flag read older than `account_max_age_seconds` (300 s, the same window as the
account and quote freshness rules) is `unknown` too, so a run that fetched the flag at 09:00 cannot use it at
10:30. The aliasing limit of the day ring is stated rather than hidden: an unreleased lease token from
exactly 12 days ago is indistinguishable from today's, which is why releasing a lease removes *all* ring
codes and why the ring is only ever a coarse self-expiry.

```
python -m live.golive flag --account 5832812 --read flag-1035.json
python -m live.golive flag --account 5832812 --read flag-1035.json --set paused  --lease cloud-executor \
       --source "slack PAUSE 1763912345"
python -m live.golive flag --account 5832812 --read flag-1600.json --set running --lease none \
       --source "slack RESUME 1763912999"
```

`--set` never writes the watchlist: it prints the `quote_modify_user_security` payloads and writes them to
`flag-write-<hhmmss>.json`, and the agent sends them and re-reads the group. The re-read must return exactly
`expected_codes`; if it does not, the flag was not shared and the round does not proceed.

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

1. Both switches first: read the local control row (or run any command and look at what R16 says), then
   fetch the watchlist group (`quote_user_security group_name=PSTAR-PAUSE`), save it, and run
   `flag --read flag-<hhmm>.json`. If either says stopped, send nothing: run `pause` with today's order
   history so `cancel-<hhmmss>.json` is written, send the cancels, `reconcile`, and stop. A run that stops
   here is the switch working, not an error.
2. Borrow checks for every planned short: `sim_trade_max_buy_sell`. No freshness limit, so do them first.
3. Take the lease, then plan. `flag --read <fresh read> --set running --lease local-entry --source "user
   started the 11-23 entry"` and send the writes; a `cloud-executor` lease that has not expired is the
   reason not to (the flag refuses the second trader on the same book). Then, back to back:
   `sim_trade_account_list` + `sim_trade_cash_info` + `sim_trade_position_list` +
   `sim_trade_history_order_list` → `snapshot`; `quote_market_snapshot` for the whole book in as few
   calls as possible; `evidence --trigger "user started the run"`; **one `plan --flag flag-<hhmm>.json` for
   all names**. Never plan one symbol at a time: a single call is what makes the order budget, the position
   cap and the gross/margin projections describe the batch the gate actually approved. `plan` refuses before
   it writes a ticket when the flag says `paused` or `unknown`, and prints the `pause` command to run.
4. Submit every ticket in `plan.json`'s `submit` list, in plan order, without asking; `record` each
   response file. Anything the gate left out of `submit` is reported as unsent with its rule ids.
5. If `plan` reports R11 or an expired approval, refresh and re-plan automatically: re-run `snapshot`,
   `evidence` and `plan`. Those commands send nothing, so a refresh costs time, not safety.
6. Every 5 minutes while anything is working: re-read the shared flag and save it, fetch today's order
   history, and run `reprice --flag flag-<hhmm>.json` for what has not filled — it re-gates the remaining
   quantity through the same rules, and a flag that turned `paused` or `unknown` between rounds converts
   every due ticket into cancel-only: the parent is cancelled, no child is issued, and
   `switch-hold-<hhmmss>.json` says what to run. At most 3 reprice rounds per name
   (`MAX_REPRICE_ROUNDS`), then the ticket becomes `skipped_unfilled`. The per-day and competition order
   caps are unchanged by any of this.
7. Stop at 15:45 ET or when every ticket is filled or skipped, then cancel anything still working,
   `reconcile`, release the lease (`flag --read <fresh> --set running --lease none --source "…"`), and post
   the summary in this chat: filled, skipped, unfilled, gross long and short, margin, largest ticker, fees,
   orders used against both the 100/day strategy budget and the 200 competition cap, and what the switch
   read at the last round.
8. Anomalies go into that summary **and** into `reports/live/golive/<date>/<account>/alerts.json` — see
   the list below. `reconcile` writes the ones it can see; `alert` records the ones only the agent sees.

### The scheduled cloud run

The 2026-11-27 final exit and the risk reductions run in the cloud Executor session, on the same commands
with `--side cloud`, and the same gate. What differs is only where the flags come from:

1. Read `PSTAR-PAUSE` (`quote_user_security`) and save it. Paused or unknown does not stop a reduce-only
   batch — `FINAL_EXIT` and `EMERGENCY_EXIT` pass whatever the flag says — but it does stop anything that
   would add risk, and the read is what the evidence file records either way.
2. Risk Auditor only: the three raw reads → `skills/pstar-risk-check/scripts/risk_check.py` →
   `risk-<hhmm>.json`, posted to Slack when the level is not `OK`.
3. `snapshot`, then `evidence --side cloud --flag … --auditor-session … --executor-session …` (see below),
   then one `plan --final-exit` / `plan --risk-action …` for the whole batch. A `local-entry` lease that has
   not expired stops an entry-shaped cloud batch here; it never stops the flatten or the cancels.
4. Submit what `submit` lists, `record` each response, reprice every 5 minutes, and at 12:45 ET the plan
   switches the remainder to MARKET. Every round re-reads the flag and re-runs `evidence`, so a `PAUSE`
   that lands mid-exit converts the next round to cancel-only for everything that is not a reduction.
5. By 12:55 ET `reconcile` must show zero positions; if it does not, the leftover is listed in the post and
   in `alerts.json`, never rounded away.

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

Python computes or fills every field; the chat agent does not write this file by hand. In the default
`--side local` the run is this Qoder Desktop chat: the independent check is the deterministic
golive/gate run, and the human controls are the pause switch and the Slack alerts.

| field | how the value is obtained |
|---|---|
| `release_verified` | local: git — no uncommitted `.py` under `live/`, `rules/` or `journal/` at the checked-out commit, whose hash is recorded in `release.commit`. Edits to the config's `mode` and `execution_enabled` deliberately do not count, because R10 already ties the targets to the config checksum. No usable git answer means false, never a pass. |
| `reconciled` | the fresh history has no `WORKING`, `PARTIAL`, `UNKNOWN` or `UNREADABLE` row for this account; otherwise false, with those rows listed in `reconcile.unresolved`. A filled order, a cancelled-without-fill order and a partial fill that was then cancelled are facts, not unknowns, so they never hold the next round. |
| `snapshot_verified_by_relay` | the file has the shape only `snapshot` writes, the relay line is intact, each raw response it names still hashes to what was recorded, and `sim_trade_account_list` asserts this account (i.e. it was captured with `--accounts`) |
| `lease_verified` | local: true, on the recorded basis — local mode, decided by the user 2026-11-20: this Qoder Desktop chat takes the `local-entry` lease at the start of the run and releases it at the end |
| `permission_policy_verified` | local: true, on the recorded basis — automatic execution decided by the user on 2026-11-21: every order passes the deterministic gate, and the user can pause at any time with Slack `PAUSE` |
| `approval` | `approval_id` `golive-<date>-<account>-<hhmmss>`, `issued_at` now, `expires_at` now + 240 s, `verified_by_relay` true, auditor session `golive-deterministic-checks`, guard session `qoder-desktop-chat`, `confirmed_by` `user-started run`, `trigger` from `--trigger`, basis "the independent check is the deterministic golive/gate run; the human controls are the pause switch and the Slack alerts" |
| `borrow.easy_to_borrow_proxy` | `sim_trade_max_buy_sell.max_sell_short_qty` — the accepted proxy: a short passes R5 only when that quantity is at least its planned size, and an un-borrowable name is skipped, never replaced |

Exit code 0 means all five assertions came out true; 2 means at least one is false and `plan` will be held
by R11/R12/R14/R17. `bases` and the `release` / `reconcile` / `snapshot` blocks say which one and why, in
the same words the printed line uses.

### `evidence --side cloud` — the scheduled Executor run

```
python -m live.golive evidence --account 5832812 --side cloud \
    --snapshot snapshot-121500.json --orders orders-121500.json --flag flag-121500.json \
    --trigger "scheduled run final-exit" --risk-report risk.json \
    --auditor-session <risk-auditor session id> --executor-session <executor session id> \
    --out evidence.json
```

A cloud session has no git checkout, nobody typing `GO`, and no claim on the local lease, so the same three
flags are earned from the evidence a cloud session actually has. `plan` reads `side` back out of this file
and asks for the `cloud-executor` lease instead of `local-entry`.

| field | cloud evidence |
|---|---|
| `trigger` | only one of the three shapes that may start a cloud run: `scheduled run [job]`, `slack PAUSE <ts>` / `slack RESUME <ts>`, `risk KILL\|ALERT <report>`. Anything else raises — free text is how a chat message asks for a favour, and in the cloud it would be a human approval wearing a costume. |
| `release_verified` | every skill pinned on a role in `agents/templates.json` names the exact cloud skill id and version that `agents/release-lock.json` sealed (`release.pinned` lists `role/skill → id@version`). A role on `latest`, a pin that differs, or a pin nobody sealed is false. Git is never consulted, because there is no checkout there to consult. |
| `lease_verified` | the raw `quote_user_security` read of `PSTAR-PAUSE` passed as `--flag`: true when the `cloud-executor` lease is held, or when no unexpired lease is held so it can be taken. A live `local-entry` lease is false — one order stream per book. No `--flag` file is false, not "assume nobody else is trading". The two exemptions are the batches that only take risk off: a `slack PAUSE` cancel list and a `KILL` flatten (`--risk-report` level), which is why a pause still works while the local entry is mid-round. |
| `permission_policy_verified` | `agents/templates.json` records `tool_policy.platform_denial_verified` with an `observed_at`, and the Executor is the only role listing order tools. Until someone has watched the platform refuse an order tool outside the Executor, this is false with a basis prefixed `limitation: ` — the refusal is recorded as a known gap rather than assumed, and `plan` then fails R17 instead of trading on an unproven policy. |
| `approval` | `auditor_session_id` = the Risk Auditor session that ran `pstar-risk-check`, `guard_session_id` = the Executor session that will send, and the two must differ or the command raises (R14 rejects an approval whose auditor is its executor, and a cloud role has no human on either side). `confirmed_by` says the deterministic gate decided it. |

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
