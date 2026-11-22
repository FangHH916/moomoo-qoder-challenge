---
name: pstar-risk-check
description: Measure the deployed pstar paper book on account 5832812 from raw moomoo reads and classify it as OK, WARN, ALERT, KILL or CHECK_FAILED, writing risk_report.json plus the exact text to post to Slack. Use when a scheduled 30-minute check runs, when a reviewer asks whether the book is inside the frozen risk limits, or before any risk reduction is handed to the Executor. Read-only: this skill never places, cancels or replaces an order and never sizes a ticket.
---

# pstar risk check

Run this as the Risk Auditor. The role holds paper-account read tools and nothing else: no order tool exists
in it, and the only role allowed to trade is the Executor. This skill measures and reports; `golive.py` plans
and the Executor executes. An alert is not a stop order - between checks the book is unhedged, and that
sentence belongs in every Slack post this skill produces.

## 1. Read the account raw, then hand the files to the script

Three reads, one account, nothing else. Use the moomoo MCP tools for the competition paper account
`5832812` and save each response to a file exactly as returned - do not reformat, re-key, summarise or
retry into a "cleaner" shape:

| read | tool | file |
| --- | --- | --- |
| cash / equity | `sim_trade_cash_info` | `cash.json` |
| positions | `sim_trade_position_list` | `positions.json` |
| today's and window orders | `sim_trade_history_order_list` | `orders.json` |

Also read `sim_trade_account_list` when it is available and pass it as `--accounts`: it proves the account
exists in this workspace rather than only that a payload carried the number. The script refuses any payload
that names an account other than the one it was given, so a file copied from the 5322438 test book cannot be
read as evidence about the competition book.

Order history must cover the whole competition window, not just today: the 200-order competition cap and the
daily 100-order budget are both measured from it, and a truncated page under-counts the spend.

## 2. Run the check

```sh
python scripts/risk_check.py \
  --cash cash.json --positions positions.json --orders orders.json \
  --account 5832812 --accounts accounts.json \
  --config live/config.pstar.json --ops live/ops.json \
  --targets live/targets/competition/<sealed-book>.json \
  --peak-file reports/live/golive/state/risk_peak_equity.json \
  --out risk_report.json --slack-out slack.txt
```

Standard library only - no install, no network, nothing imported from the repository. `--config`, `--ops` and
`--targets` default to the checkout copies and are also looked for in the current directory, so an unpacked
zip works; pass them explicitly when the workspace root is known. `--as-of` pins the clock for a replay.
`--eod-dir ""` and `--peak-file ""` disable those peak sources, which is how a fallback basis is proven.

Exit codes: `0` clean, `1` a WARN/ALERT/KILL or any anomaly, `2` the check itself failed. A non-zero exit is
a result, not an error to swallow - post what the script printed.

## 3. What the thresholds are, and that they are not negotiable

Everything is read from `live/config.pstar.json` and `live/ops.json`; the script holds no numbers of its own
and refuses to run against a config whose `strategy` is not `pstar`. The frozen rules, in the order the
backtest applied them:

- **KILL** - drawdown at or past **-8%** of the peak: flatten every strategy position and halt.
- **ALERT** - a name above **39%** of initial assets is trimmed back to **38%**, rounded down so the cap
  holds; margin `(0.5 x long gross + 0.6 x short gross) / equity` above **95%** scales the book to **90%**.
- **WARN** - the early-warning lines in `live/ops.json`: a name above **37%**, margin above **85%**, drawdown
  past **-4%**, more than **95** positions, or order spend past **80%** of either limit. A WARN posts to
  Slack and places nothing, ever.
- **OK** - inside every line above.

`live/test_risk_check.py` proves against `live/risk_monitor.py` that the script's margins, drawdowns, warning
lines and share deltas are the same arithmetic, so a number posted here is the number the planner will act
on. If the two ever disagree, the planner's own recomputation wins: `golive.py plan --risk-action` re-measures
from a fresh snapshot and refuses a report whose level is harsher than the book now shows.

## 4. Order anomalies

Any of these is reported, with the rule it relates to named in the line:

- `broker_rejection` - a rejected or failed status, whatever the symbol.
- `order_working_too_long` - still working past `order_working_minutes_alert` (30 minutes).
- `partial_fill` - filled between zero and the requested quantity, so the residual is not the planned ticket.
- `unexpected_symbol` - an order or a position in anything outside the target book plus the 10 ETFs and
  `discretionary_symbols`. A pause-switch sentinel (`US.SQQQ`, `US.SOXL`, `US.XLE`, `US.XLF` and the day ring)
  is called out as the traded flag itself, which is a reconciliation problem, not a holding.
- `untagged_order` - no `pstar-` tag and not a discretionary name, so this strategy did not place it.
- `order_budget` - spend past 80% of the 100-order daily limit or the 200-order competition cap, stating what
  is left and that a batch still needs its reprice rounds and the 100-order emergency reserve.
- `check_failed` - see below.

## 5. Peak equity, which does not survive a session on its own

The Auditor and the Executor run on different machines, so the high-water mark has to be written down:

1. `--peak-file`: a day-keyed state file this script appends to and reads back. One row per ET day; a
   re-run the same day replaces its own row instead of stacking. The peak is the maximum of the stored days,
   the initial assets and today's equity, and a bad day never lowers it.
2. The highest equity in a previous end-of-day report under `--eod-dir` (`reports/live/*/*/summary*.json`),
   for the same account only.
3. `max(initial assets, today's equity)`, which understates a drawdown the account has already survived.

Whichever was used is written to `peak_basis` and printed as `peak basis: ...` in the Slack text, and
`peak_equity` plus the day-by-day `peak_history` go into the report so the Executor can adopt a peak its own
snapshot never saw. A claimed peak with no history behind it is refused by `golive.py`, so never hand-edit
`peak_equity`.

## 6. A failed check is a report, never silence

If a read is missing, is not JSON, returns a non-zero `ret_code`, names the other account, or contains a
holding with no quantity or no price, the script writes a `CHECK_FAILED` report, prints it and exits `2`.
Post it to Slack and stop: `a CHECK FAILED report is never fed to golive.py plan --risk-action`. Nothing was
measured, so nothing is trueable - and "no risk report today" must never be readable as "no risk".

## 7. What to do with the output

- Post the `slack_text` block verbatim. Do not paraphrase, reorder, add advice, or convert an ALERT into a
  recommendation to sell.
- For **KILL** or **ALERT**, hand the report file to the Coordinator so the Executor can run
  `python live/golive.py plan --account 5832812 --snapshot ... --risk-action risk_report.json`. Reduce-only
  tickets come out of that command through the deterministic gate; this skill does not size, submit, cancel
  or approve an order. A KILL also sets the shared pause switch inside that same command.
- For **WARN** or **ORDER ANOMALY**, the run is a Slack post only. Nothing trades between rebalances on a
  warning.
- The Auditor is the only role that writes these verdicts, and its approval is the only one the Executor
  acts on. Keep the two sessions distinct: an auditor cannot approve its own execution.

## 8. Limits to state in every report

`an alert is not a stop order: between checks the book is unhedged.` The script measures the account as the
broker reported it, prices included; it cannot see a fill that has not settled, a fee the API omits, or a
position the account holds outside this strategy (those come back as `protected` or `unexpected_symbol`).
Positions the strategy did not open are measured and reported but never sized for trading - protection is
attributed from the order history given, and `golive.py` re-derives the same split from the journal, which is
the authoritative version.

These steps are instructions, not permissions: the skill gives the Auditor no order tool and no account it may
name itself, and a verdict is only as good as the raw reads it was handed.
