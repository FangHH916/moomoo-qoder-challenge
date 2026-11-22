---
name: pstar-executor
description: Run the deterministic P* execution tooling in a cloud Executor session - live/golive.py snapshot, evidence, plan, record, reprice, reconcile, plus the packaged pstar-risk-check script - on account 5832812, using only raw moomoo MCP responses saved to files. Use when a schedule fires the 2026-11-27 final exit, when the Risk Auditor hands over an ALERT or KILL report, or when the user's Slack PAUSE needs its cancel list. This skill contains the code that decides every quantity and price; it never fetches data, never trades on a research opinion and never sends an order by itself.
---

# P* executor runtime

This skill is the Executor's deterministic half. The archive holds `live/golive.py`, `rules/`,
`journal/store.py`, `live/pstar-risk-check`'s script, the frozen config, `live/ops.json` and the sealed target
book, each under its repository-relative path, so the unpacked folder is a working tree:

```
cd <unpacked> && PYTHONPATH=. python -B -m live.golive --help
```

`RUNTIME.json` in the archive lists every file, its SHA-256 and the pip requirements. If the hashes in that
file are not what the run is using, the run is not the release the reviewer read: stop and report
`CHECK FAILED` rather than continuing with a modified gate.

Two things this skill never does: it does not call the broker (the Executor role's MCP tools do, and the role
passes their raw responses in as files), and it does not decide anything a rule decides. Every number printed
here is computed by the same Python the local run uses. Nothing here types an order the plan did not write.

## 1. Which account, and what may be sent

- Competition paper account `5832812` only. `golive.py` refuses any other account id, and it refuses when the
  config's `mode` and the account do not agree, because R1 rejects every account except the mode's own.
- Orders come from `plan`'s `submit` list and nowhere else: same `client_order_id`, same `text` tag, same
  quantity, same price, same side. Reformatting a payload, re-deriving a price, or sending a ticket the gate
  left out are all refusals, not optimisations.
- `plan --final-exit` and `plan --risk-action` are the only cloud batches. Entries are the local run's job
  (the 2026-11-23 catch-up entry happens in the Qoder Desktop chat under the `local-entry` lease).

## 2. The reads to save before running anything

Save each MCP response verbatim to a file - no reformatting, no re-keying, no summarising:

| file | tool | why |
|---|---|---|
| `accounts.json` | `sim_trade_account_list` | binds the snapshot to one account id |
| `cash.json` | `sim_trade_cash_info` | equity, balance, long/short gross |
| `positions.json` | `sim_trade_position_list` | what is actually held, with signs |
| `orders.json` | `sim_trade_history_order_list` | today's and the competition's order history: R12, R19, the budget |
| `quotes.json` | `quote_market_snapshot` | the whole book in as few calls as possible |
| `borrow.json` | `sim_trade_max_buy_sell` | short capacity for every name to be covered |
| `flag.json` | `quote_user_security group_name=PSTAR-PAUSE` | the shared pause switch and the lease |
| `risk.json` | produced by `risk_check.py` from the four reads above | the Auditor's verdict |

## 3. The command sequence

```
PYTHONPATH=. python -B -m live.golive snapshot  --account 5832812 --cash cash.json \
    --positions positions.json --orders orders.json --accounts accounts.json
PYTHONPATH=. python -B -m live.golive evidence  --account 5832812 --side cloud \
    --snapshot snapshot.json --orders orders.json --flag flag.json --trigger "scheduled run final-exit" \
    --auditor-session <auditor session id> --executor-session <this session id> --out evidence.json
PYTHONPATH=. python -B -m live.golive plan      --account 5832812 --snapshot snapshot.json \
    --targets live/targets/competition/2026-11-19-pstar.json --quotes quotes.json --borrow borrow.json \
    --orders orders.json --evidence evidence.json --flag flag.json --final-exit --out plan.json
```

- `--side cloud` is what makes `release_verified` come from the pinned skill versions and `lease_verified`
  from `flag.json`; see `docs/GOLIVE.md` for what each flag means and what makes it false.
- `--trigger` accepts only `scheduled run [job]`, `slack PAUSE|RESUME <ts>` and `risk KILL|ALERT <report>`.
  Free text is refused: nobody is typing an approval into a scheduled run.
- `--auditor-session` and `--executor-session` must be two different sessions. One session measuring its own
  work is not an independent check (R14), and the command raises rather than letting the gate decide later.
- `--templates` / `--release-lock` default to `agents/` in this tree. In a cloud session, pass the template
  export the Coordinator fetched from the platform, not the copy inside this archive, or the pin check would
  be comparing a file with itself.
- `evidence` stamps a 240 s approval window and re-reads the history, so every reprice round re-runs
  `evidence` and `plan`/`reprice` before it sends anything. Those commands never submit.

## 4. What `plan --final-exit` produces

Closes every position on the account in one batch: SELL each long, BUY to cover each short, full quantity from
the fresh snapshot, intent `FINAL_EXIT` so it passes a paused switch and R10/R13/R19. It prints the order
budget against the 200-order competition cap **before** the first ticket, so a flatten that cannot fit is
known before anything is sent rather than after 190 orders. Limits are marketable and repriced every five
minutes from `final_exit_start` (12:15 ET); anything still open at `final_flatten_at` (12:45 ET) goes out as
MARKET, which the gate permits only for a `FINAL_EXIT` reduction on 2026-11-27 at or after that moment.
Everything else is still LIMIT DAY.

## 5. What `plan --risk-action risk.json` produces

Reduce-only tickets from the Auditor's report, after re-measuring it against the fresh snapshot: a `>39%`
name trimmed back to 38%, the book scaled to 90% margin when margin passes 95%, and on `KILL` a full
`EMERGENCY_EXIT` that latches the halt and sets the shared switch. A report the fresh book does not confirm
places nothing - `golive.py` refuses a plan built on numbers it cannot reproduce, and it never lets the
report be harsher or softer than the measurement.

## 6. Sending, recording and stopping

1. Send every ticket in `submit`, in order, without asking; that is the user's 2026-11-21 decision.
2. `record --plan plan.json --ticket <n> --response <file>` after each submission response, and re-fetch
   `orders.json` before any resend. `record` refuses a second response for a ticket that already has an
   order id, which is what keeps a timeout from becoming two orders.
3. Every 5 minutes: fresh `flag.json`, fresh `orders.json`, `evidence`, then `reprice`. A switch that turned
   `paused` or `unknown` between rounds converts the round to cancel-only.
4. On `slack PAUSE <ts>`: run `pause` with the fresh order history so `cancel-<hhmm>.json` is written, send
   those cancels, `reconcile`, and stop. Positions stay; only `FINAL_EXIT`/`EMERGENCY_EXIT` reductions
   continue, and neither waits for a lease.
5. `reconcile` at the end. If the account is not flat, post exactly what remains - symbol, side, quantity -
   and never describe a partial result as complete.

## 7. Reporting

Post what the tools printed, not a paraphrase: ticket payloads, the budget line, the rule ids of anything
rejected, and the risk verdict text. If a command refuses, the refusal is the message. Never write a token,
PAT or OAuth secret into a file, a prompt, a log or a commit; the MCP connection is the credential and this
skill never reads it.

## 8. Limits of this runtime

- It is Python and a rule table, not a market watch: it acts when a scheduled run fires, so between two runs
  the book is unhedged and an alert is not a stop order.
- `easy_to_borrow` has no broker field in the paper tools; the accepted proxy is
  `sim_trade_max_buy_sell.max_sell_short_qty`, and an un-borrowable name is skipped, never replaced.
- Order status codes: only 4 (filled) and 5 (cancelled, no fill) have been observed. Anything the history
  reports in a code the run cannot resolve stays `UNKNOWN`, which blocks a resend rather than authorising one.
- The archive carries no network client. If `plan` cannot read a file it was handed, it exits 2; it does not
  go and fetch a substitute.

These steps are instructions, not permissions: unpacking this archive grants nothing. Which role may call
`sim_trade_input_order` is the platform tool policy recorded in `agents/templates.json`, and every ticket this
runtime can send still has to pass the deterministic gate in `rules/gate_pstar.py`.
