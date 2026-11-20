# Go-live runbook — P\* `pstar15`, first rebalance (written 2026-11-20, prompt 07 Parts 1 and 2)

Stage A smoke test on paper account **5322438** (mode `test`), then the ENTRY batch on **5832812**
(mode `competition`). US regular session opens 09:30 ET = 22:30 SGT on 2026-11-20; execution starts
09:35 ET per PRD R7, never at the opening auction.

`live/golive.py` never places an order. It reads raw MCP responses from files, writes normalised
artifacts and tickets, and refuses anything it cannot prove.

## Who does what

- **Chat agent (the only MCP caller):** call the read-only paper/quote tools, save each raw response
  verbatim to a file, run `live/golive.py`, paste `plan.json` payloads into `sim_trade_input_order` /
  `sim_trade_cancel_order` **unchanged**, save each response, then feed the files back. Nothing else.
- **Deterministic Python:** parsing, sizing, pricing, the R0–R21 gate, duplicate protection,
  reconciliation, and the computation of every field in `evidence.json`. No order quantity or price is
  ever computed by hand or edited in a payload, and no evidence flag is ever typed.
- **The user:** `ARM <account>` before the kill switch releases the account, `CONFIRM` before each submit
  batch, and `GO` before Stage B. The agent never arms on its own initiative: it asks the user to type the
  reply, waits, and passes that exact text to `arm`.

Tools allowed: `sim_trade_account_list`, `sim_trade_cash_info`, `sim_trade_position_list`,
`sim_trade_history_order_list`, `sim_trade_max_buy_sell`, `sim_trade_input_order`,
`sim_trade_cancel_order`, the quote tools. **Never** any `trading_*` / `account_*` tool, no market
orders, no extended hours, no options.

## Files

All commands run from the repository root with `PYTHONPATH=. .venv/Scripts/python.exe -m live.golive …`.
Raw responses and artifacts for a night go in `reports/live/golive/<date>/<account>/`:
`cash.json`, `positions.json`, `accounts.json`, `orders-<hhmm>.json` (one file per history pull),
`quotes.json`, `borrow.json`, `evidence.json`, `response-<ticket>-<attempt>.json`, plus the tool's
own `snapshot-<hhmm>.json`, `plan.json`, `reconcile.json`, `summary.md`. `--journal` defaults to
`journal/`; every step appends a record there. Exit code 2 means refused or incomplete — read the
printed line, do not retry blindly. `--as-of` takes a timezone-aware ISO timestamp (defaults to now UTC).

The commands are `arm`, `halt`, `snapshot`, `evidence`, `plan`, `record`, `reprice`, `reconcile`. None of
them contacts the broker, and none of them needs a `--hold` list: positions this strategy did not open are
protected by the planner (Stage B step 3).

## Arming the kill switch

`journal/` keeps one durable control row per account and R16 rejects every strategy order while that row
says halted. A fresh store has no row, which reads as `{"halted": true, "reason": "Not armed"}`.

```
python -m live.golive arm  --account 5322438 --reply "ARM 5322438"
python -m live.golive halt --account 5322438 --reason "why the halt is latched"
```

`arm` accepts only the account of the current config mode (5322438 in `test`, 5832812 in `competition`)
and only a `--reply` that is exactly `ARM <account>` — the text the user types in this chat. A wrong
reply, an unknown account or the other mode's account is refused before the store is touched and nothing
is journaled. A good one writes `halted = 0` with the reason `armed by the user in chat at <ts>` and
appends a `GOLIVE_ARM` decision. `halt` re-latches through the store's own method and appends
`GOLIVE_HALT`. Neither cancels an order nor sends anything: cancelling and flattening stay separate,
confirmed submissions.

## Order of calls, and the 60-second clock

R11 refuses a plan whose account snapshot or quote is more than 60 s old, so the freshness-limited calls
run back to back and the slow ones go first:

1. Borrow checks: `sim_trade_max_buy_sell` for every short. These have no freshness limit, so do them
   first.
2. Then, without pausing: `sim_trade_account_list` + `sim_trade_cash_info` + `sim_trade_position_list` +
   `sim_trade_history_order_list` → `snapshot`; `quote_market_snapshot` for every name in the book, in as
   few calls as possible; `evidence`; `plan`.
3. If `plan` reports R11, refetch cash, positions and quotes and re-run `snapshot`, `evidence` and `plan`.
   That sends nothing.

`evidence` stamps its approval with a 240 s window (R14's frozen TTL is 300 s, so R14 stays the
authority). If the user's `CONFIRM` arrives after `expires_at`, do **not** submit: refresh the snapshot,
quotes and evidence, re-plan, and show the summary again.

## Stage A — smoke test (5322438, mode `test`)

0. `quote_market_snapshot`/market-state check first: if the US regular session is not open, stop.
1. Ask the user to type `ARM 5322438`, pass that exact reply to `arm`, and report what it printed.
2. `sim_trade_account_list` + cash + positions + today's history → `snapshot --account 5322438 --cash …
   --positions … --orders … --accounts …`. Report equity and whether the account is flat. Any account
   mismatch is a stop, not a retry.
3. `quote_market_snapshot` for IWM and the target shorts, `sim_trade_max_buy_sell` for the same names,
   then `evidence --account 5322438 --snapshot <file> --orders <fresh history> --trigger "user pasted
   07 Part 2" --out evidence.json`, then `plan --account 5322438 --snapshot <file> --targets <sealed
   contract> --quotes quotes.json --borrow borrow.json --evidence evidence.json --smoke --out
   plan-smoke.json`. It makes exactly 4 tickets: BUY 1 IWM, SELL 1 share of one executable short priced
   under $50, then the two closes. Show tickets 1–2 and **wait for `CONFIRM`**.
4. Submit tickets 1–2 only, one MCP call each, saving `response-1-1.json` / `response-2-1.json`, then
   `record --plan plan-smoke.json --ticket 1 --response response-1-1.json` (and 2). Poll
   `sim_trade_history_order_list` every 30 s, max 5 minutes; re-run `record` for a new attempt only with
   `--orders <fresh history>`.
5. Check positions: `+1 IWM` and a real `-1` short. If the short is rejected or does not create a short
   position — **STOP**, report the exact error text, and do not open Stage B. Record the fee from the
   cash difference.
6. Submit tickets 3–4 (closes need no extra confirmation), `record` both, re-`snapshot` and confirm flat,
   then `reconcile --plan plan-smoke.json --snapshot <new snapshot> --orders <history>`. Show the order /
   status / fill price / fee / seconds table, and report what the status fields and DAY/TIF behaviour
   showed.

## Stage B — ENTRY batch (5832812, mode `competition`, only after Stage A passed and `GO`)

1. Edit `live/config.pstar.json`: `"mode": "competition"`, `"execution_enabled": true`.
2. Ask the user to type `ARM 5832812`, pass that exact reply to `arm --account 5832812`, and report what
   it printed. R16 holds the whole batch until it does.
3. Accounts/cash/positions/history for 5832812 → `snapshot --account 5832812 …`. Positions the strategy
   did not open are protected automatically: `live/pstar.py::classify` splits the book into strategy,
   discretionary and unclassified, and the planner refuses to sell or re-size anything but strategy
   positions (*"not opened by this strategy: never sold or re-sized"*), so nothing the journal does not
   attribute to `r10-pstar` is traded and no `--hold` list is needed.
4. Publish the contract for this mode — the config edit changes `config_checksum`, so the test-mode file
   is refused: `python -m live.pstar --snapshot <the new snapshot file> --no-run` →
   `live/targets/competition/2026-11-19-pstar.json`. It must print `READY`; a `HOLD` is a stop.
5. `quote_market_snapshot` for all 82 target names and `sim_trade_max_buy_sell` for the shorts, then
   `evidence --account 5832812 --snapshot <file> --orders <fresh history> --trigger "user replied GO"
   --out evidence.json`, then `plan --account 5832812 --snapshot … --targets … --quotes … --borrow …
   --evidence evidence.json --orders <fresh history>`. The ETF fund-size and borrow screens run inside
   `plan`. Show the summary (counts by side, gross long/short in $, margin after, largest ticker vs
   $38,000, positions after, skipped names with reasons, estimated fees at $0.99/order) plus the file
   path — not the full list — and **wait for `CONFIRM`**. If the `CONFIRM` arrives after the approval's
   `expires_at`, refresh and re-plan instead of submitting.
6. Submit ETF tickets first, then the shorts, one at a time, `record` after every response. A short
   rejected as not borrowable is skipped and logged, never replaced.
7. Poll every 60 s. After 5 minutes run `reprice --plan plan.json --orders <history> --quotes <new
   quotes> --snapshot <snapshot> --evidence <fresh evidence file>`; it emits the cancel payload and a new
   limit ticket for the remaining quantity. Max 3 rounds, then the ticket becomes `skipped_unfilled`.
   Cancel anything still open afterwards — a DAY order cannot be trusted to expire by itself.
8. `reconcile --plan plan.json --snapshot <post-trade snapshot> --orders <history>`, then the risk
   monitor report: filled/skipped/rejected counts, gross long/short, margin, positions, largest ticker
   vs $38,000, fees and average slippage, deviations from target, executed orders today against the
   100-order budget (200 cap, 100 reserved for an emergency flatten).
9. Set `"execution_enabled": false` (and `"mode": "test"`), commit `journal/` and `reports/live/golive/`,
   add the timestamped entry to `agents/AGENTS.md`. `halt` is available any time the user asks for it.

## `evidence.json` — computed, never typed

```
python -m live.golive evidence --account 5322438 --snapshot snapshot-223500.json \
    --orders orders-223500.json --trigger "user pasted 07 Part 2" --out evidence.json [--working <code>]
```

Python computes or fills every field; the chat agent does not write this file by hand. The cloud agents
have no order tools and are not part of it: in local mode the independent check is this deterministic
golive/gate run, and the human control is the user's `CONFIRM` before any submission.

| field | how the value is obtained |
|---|---|
| `release_verified` | git: no uncommitted `.py` under `live/`, `rules/` or `journal/` at the checked-out commit, whose hash is recorded in `release.commit`. Edits to the config's `mode` and `execution_enabled` deliberately do not count, because R10 already ties the targets to the config checksum. No usable git answer means false, never a pass. |
| `reconciled` | the fresh history has no `WORKING`, `PARTIAL`, `UNKNOWN` or `UNREADABLE` row for this account; otherwise false, with those rows listed in `reconcile.unresolved` |
| `snapshot_verified_by_relay` | the file has the shape only `snapshot` writes, the relay line is intact, each raw response it names still hashes to what was recorded, and `sim_trade_account_list` asserts this account (i.e. it was captured with `--accounts`) |
| `lease_verified` | true, on the recorded basis: local mode, decided by the user 2026-11-20 — this Qoder Desktop chat is the only order sender and no cloud agent exists or has order tools |
| `permission_policy_verified` | true, on the recorded basis: every submit batch needs the user's `CONFIRM` in this chat |
| `approval` | `approval_id` `golive-<date>-<account>-<hhmmss>`, `issued_at` now, `expires_at` now + 240 s, `verified_by_relay` true, auditor session `golive-deterministic-checks`, guard session `qoder-desktop-chat`, `confirmed_by` user, `trigger` from `--trigger` |
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
  previous file fails the gate rather than silently resending.
- Tickets rejected by the gate stay out of `submit` and are listed with their rule ids in
  `summary.rejected_tickets`; the plan's exit code is 2 when nothing passes.

## `--working` (unmapped order statuses)

The paper API returns an integer `order_status` with no published code table. Without proof, golive
reports such a row as `UNKNOWN`, lists the code in `unresolved_status_codes`, holds the batch under R12
and exits 2. Quantity columns can settle it (`cum_qty == qty` ⇒ filled). If, and only if, the operator
confirms from the app that a code means a live working order, pass it explicitly, e.g.
`reconcile … --working 5`, which reclassifies those rows as `WORKING`. Reconcile then reports them under
`open_orders` — still live, so the batch is not `complete_reconciliation`.

## Stop conditions (report, do not improvise)

Account mismatch · whole-batch gate failure · stale or non-`READY` targets, or a config/targets checksum
mismatch · 3 consecutive MCP errors · a drawdown halt · the R16 kill switch latched · a rejected short
sale in Stage A · any unexpected response shape.

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

Tests: `PYTHONPATH=. .venv/Scripts/python.exe -m unittest live.test_golive`.
