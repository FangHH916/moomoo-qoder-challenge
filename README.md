# Risk-Constrained Multi-Factor Paper Trading Team

A US-only paper-trading entry built with Qoder and QCA. Start with [the PRD](docs/PRD.md) and [the agent registry](agents/AGENTS.md).

**Status: local scaffold; cloud and order execution inactive.** No real accounts, orders, schedules or cloud configuration are changed by setup or fixture commands. Research/backtest files are preserved. Fixture results are not competition returns.

The deployment default is the frozen portfolio **P\***, in the round-14 variant `pstar15`. It is computed in exactly one place, `research/r10_deploy.py`, and never re-implemented: `live/pstar.py` runs that file, verifies its output and converts it into a sealed targets contract. The previous default `frozen-short-alpha-half` (Plan A) is retired to `strategy: legacy-a` — its code, rules and tests stay, but it is not selected and cannot execute.

## Local setup

Use Python 3.14 and the pinned versions in `requirements.txt`. From this project folder in PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s rules -p "test_*.py"
.\.venv\Scripts\python.exe -m unittest discover -s live -p "test_*.py"
.\.venv\Scripts\python.exe -m unittest discover -s journal -p "test_*.py"
.\.venv\Scripts\python.exe -m unittest discover -s dashboard -p "test_*.py"
.\.venv\Scripts\python.exe -m unittest discover -s agents -p "test_*.py"
.\.venv\Scripts\python.exe -B agents\evaluate.py --self-check
.\.venv\Scripts\python.exe -B agents\package_skills.py --check
```

Test temporary/cache outputs should stay in this project. If running in an external terminal, set `TEMP` and `TMP` to the project's `.cache/tmp` and `PIP_CACHE_DIR` to `.cache/pip` first. These directories are ignored by git.

## Components

- `research/r10_deploy.py`: **the single source of truth for P\***. `run_pstar` builds both sleeves, `ticker_weights` blends them by Ledoit-Wolf minimum variance and multiplies by the frozen `K`, `apply_caps` enforces the competition caps, `simulate` prices the book, and `cmd_live` writes one JSON run file per session. `VARIANTS` holds the two frozen variants and `DEPLOYED` records the owner's choice.
- `live/config.pstar.json`: the frozen P\* parameters — `strategy: pstar`, `variant: pstar15`, both variant blocks with their factor lists and `K`, the ticker/margin/position/short caps, the 0.25% trade band, the 5-session cadence anchored to 2021-11-18, costs, and the `discretionary_symbols` whitelist, which is empty today because the Anthropic IPO trade sketched in prompts 03 and 04 was cancelled on 2026-11-20 — the discretionary path exists, counts toward the caps and is never sold by the strategy, and nothing is in it. As of 2026-11-21 MODE is `competition` (`execution_enabled: true`), the quote and snapshot ages are 300 s, the ETF fund-size screen is $300M, and one dated `target_age_exceptions` row admits the sealed 2026-11-19 book at the 2026-11-23 open; the user's 2026-11-21 *Automatic execution* decision is what switched the mode on and removed the per-batch human approval, leaving every R0–R21 rule in force. Switching to the `pstar` variant is a one-line edit here and nothing else.
- `live/config.v2.json`: the retired Plan A configuration (`strategy: legacy-a`), kept so its artifacts and tests still verify.
- `live/pstar.py`: the live entry point. Runs `r10_deploy.py live` with the account's equity and its non-strategy holdings, re-verifies the run file's provenance, diagnostics and agreement with the research record, and writes the sealed schema-3 targets contract. Any failed check publishes `HOLD` with a reason. `--no-run` re-verifies and re-converts the newest run file offline. It never places an order.
- `live/order_planner.py`: turns a READY contract plus the actual broker holdings into whole-share limit orders at the next open — zero-share names skipped without redistribution, re-sizing only outside the 0.25%-of-equity band and only on a rebalance session, entries and exits always, and between rebalances only the 39%→38% trim and the margin cut. It caps executed strategy orders at 100 per ET day and keeps the other 100 of the competition's 200 for an emergency flatten.
- `live/eligibility.py`: pre-trade screen for the ten sleeve ETFs (price ≥$1, fund size ≥$300M — the screen was ≥$500M until the user's 2026-11-21 decision, which found UUP's ~$483M fund blocking a 28.9% target). A failing ETF is skipped, its weight is not redistributed, and the deviation from the published book is recorded for the Slack report. A short that cannot be borrowed is skipped, never replaced.
- `live/risk_monitor.py`: the between-rebalance check to run after the open fills and before every order batch — the 39% trim, margin above 95% and the −8% drawdown flatten-and-halt.
- `live/contracts.py`: canonical checksums, the schema-2 Plan A contract, the schema-3 P\* contract (`validate_pstar_targets`) and the immutable artifact writer.
- `rules/gate_pstar.py`: pure R0–R21 evaluation for P\*, dispatched by `config["strategy"]`. `.\.venv\Scripts\python.exe -m rules.gate rules\example-order-pstar.json` evaluates the shipped synthetic order/context fixture (exit 0 means eligible, not executed); an eligible result is not an execution.
- `rules/gate.py` and `rules/example-order-legacy.json`: the retired Plan A gate and fixture, still tested and never selected by the deployment path. `.\.venv\Scripts\python.exe -m rules.gate rules\example-order-legacy.json --strategy legacy-a` evaluates it; without `--strategy` the deployed P\* rules HOLD it, which is the point.
- `live/daily_scores.py`: the retired Plan A scoring/fixture path; incremental Alpaca update is explicitly opt-in. Use `--help` for current arguments. Credentials remain in local `.env`; never paste them into chat or commit them.
- `live/test_pstar_replay.py`: the per-variant replay test. Re-runs `run_pstar` for `pstar` and `pstar15` over the 30 recorded sessions and asserts the capped weights match `weight_capped_100k` in `reports/lab/R10_reference_targets.csv` / `R14_reference_targets.csv` within 1e-9, every ticker ≤38%, margin ≤90%, positions ≤100, no long single stocks and ≤77 shorts. It also pins every frozen constant in `live/config.pstar.json` to the value in the research file, so a config edit cannot silently change the strategy. It skips when the research cache under `data/` is absent.
- `live/replay.py`: the retired Plan A replay over the scorer's own artifacts; `--source fixture` walks synthetic sessions and `--source cached --sessions 30 --bootstrap 6` walks the last 30 cached real trading days, each with its own carry-forward cache so a synthetic vector can never seed a real-bar replay.
- `live/transport.py`: inactive Forward Files API adapter and offline schedule-update payload builder. It does not create schedules. Cloud writes require separate authorisation and explicit code-level enablement.
- `live/relay.py`: offline inspection of actual pending MCP order arguments; not a running/authenticated event relay. Mandatory raw-tool permissions, DAY semantics and broker mappings are deployment blockers.
- `journal/store.py`: SQLite/WAL journal state, deduplication, halt controls, leases and JSONL projections. Every decision, including a rejection, is a first-class artifact, and each row records the `strategy_id` that opened the position — which is how a discretionary holding is identified and protected.
- `live/schedule-plan.json`: local, unregistered schedule specification; no scheduler is created.
- `agents/` and `skills/`: role specs, frozen-release registry, four evaluation prompts and seven reusable playbooks. Cloud IDs/versions remain unbound until verified. `agents/evaluate.py` checks specification invariants offline, including rejection of any live-namespaced (`trading_`, `account_`, `crypto_account_`) or order-mutation tool in a read allowlist, and that the Executor is the only role with order tools, the only writer of the shared pause switch, and the only role that loads `paper-trading-gate`; `agents/package_skills.py` builds deterministic one-zip-per-skill packages, which is not an upload. Since revision 0.3.5 both hash each file's bytes with CRLF folded to LF, so a fresh clone with `core.autocrlf=true` verifies the seal instead of reporting drift.
- Cloud execution (revision 0.3.6, decided by the user on 2026-11-21): the local Desktop chat runs the 2026-11-23 entry batch, a cloud Coordinator owns the six schedules and the Slack `PAUSE`/`RESUME` listener, a cloud Risk Auditor runs `skills/pstar-risk-check` and is the only writer of a risk verdict, and a cloud Executor is the only role that can place or cancel a paper order on 5832812 — and only from a `live/golive.py plan` the deterministic R0–R21 gate passed. Nothing waits on a typed `CONFIRM` or `ARM` any more; the durable `PSTAR-PAUSE` watchlist switch and the Slack alerts are the human controls. The limits are stated in `docs/PRD.md` §8: an alert is not a stop order, the book is unhedged between the 30-minute checks, and a `PAUSE` sent during a local round only takes effect at the next round. No cloud resource exists yet and no Qoder credit has been spent: every template, skill and schedule ID in `agents/release-lock.json` is still null.
- `dashboard/`: loopback-only read-only dashboard, synthetic fixture mode and truthful empty/not-connected mode.

## Demonstrations

```powershell
.\.venv\Scripts\python.exe -m live.pstar --snapshot live\example-snapshot.json --as-of 2026-11-20T13:00:00Z --no-run --out .cache\tmp\targets.json
.\.venv\Scripts\python.exe -m rules.gate rules\example-order-pstar.json
.\.venv\Scripts\python.exe -m live.daily_scores --fixture
.\.venv\Scripts\python.exe -m dashboard.server --fixture --port 8766
```

`live\example-snapshot.json` is a fictional paper-account snapshot (100k equity, one SPY leg and one position the journal does not attribute to this strategy version). Against the newest sealed run it publishes a READY `r14-pstar15` contract of 82 names — five sleeve ETF longs and 77 shorts — and reports both snapshot positions as unclassified discretionary holdings it will never sell. The pinned `--as-of` is what makes the walkthrough reproducible: at `2026-11-20T13:00:00Z` the newest sealed run is the 2026-11-19 rebalance book and the command publishes READY, while `--as-of 2026-11-19T13:00:00Z` — before that book's session closed — honestly returns HOLD (`research run session 2026-11-19 is not the latest completed session 2026-11-18`) because the newest JSON is not for the latest completed session. `live.pstar` without `--no-run` executes the research command, which refreshes bars and takes roughly 10–15 minutes; `--no-run` only verifies and re-converts the newest committed run file. Neither places an order. Re-running either form is idempotent: `live/contracts.py` writes with an exclusive create and returns untouched if the bytes it would write are already there, and refuses outright (`HOLD: Refusing to replace published target bytes`) if they differ, so a published contract can never be quietly rewritten. Open the local address printed by the dashboard server; without `--fixture`, no synthetic account should appear. No real-account or competition performance is implied by a demo.

For the durable pause switch — `pause` latches the account so the gate refuses every entry and writes the cancel list of the strategy's live orders, `resume` puts it back in run; neither sends nor cancels anything itself:

```powershell
.\.venv\Scripts\python.exe -m live.golive pause  --account 5832812 --source "<your message>"
.\.venv\Scripts\python.exe -m live.golive resume --account 5832812 --source "<your message>"
.\.venv\Scripts\python.exe -m journal.control halt --mode test --reason "Owner requested halt"   # the raw store command, halt only
```

`journal.control` has no `resume` action on purpose: the switch back is `live.golive resume`, which journals a `GOLIVE_RESUME` decision with the `--source` text that moved it and accepts only the account of the current config mode. A safe stop also requires the separate, explicitly authorised paper-only cancel/reconcile/liquidation procedure in [the go-live runbook](docs/GOLIVE.md) and the PRD.

## Release prerequisites

Run offline tests, actual cloud evaluations, a full-session shadow and separately approved small entry and reduce-only exit paper round trips on **5322438** before an owner-approved switch to **5832812**. Verify current shortability/ETB, broker margin/capacity, fees/individual fills, final-close handling, source provenance, exact Template/skill release locks and mandatory tool confirmation enforcement. Raw model-provided approval flags are not authentication.

The P\* book is at most 87 positions (77 shorts plus the ETF sleeve), so the go-live day is a single ENTRY batch — 82 target names, 75 orders once zero-share names are skipped on `pstar15` (70 on `pstar`) — inside the 100-executed-order budget, with the other 100 orders of the competition's 200 held in reserve for a flatten. Any ticker is capped at 38% of initial assets and margin usage at `0.5 × long gross + 0.6 × short gross ≤ 90%`. November 26 is closed; November 27 is exit-only with a 12:45 ET flat target (November 28 01:45 SGT), and its rebalance falls inside that schedule so it adds no exposure. Trading deadlines never waive failed gates.

Report the measured record as measured. `pstar15` has Sharpe 1.18 under ideal research assumptions and 1.17 under realistic $100k execution (selection 1.05, holdout 1.68), maximum drawdown −5.9% ideal / −6.9% realistic (−5.6% / −6.8% for `pstar`), every calendar year positive, and a deflated Sharpe of 0.79 over the 37 portfolio trials of round 14 — although `pstar15` itself is a single pre-registered trial, and the deflated Sharpe falls to 0.43 across all 348 trials. Realistic annualised return is 6.45% for `pstar` and 7.45% for `pstar15` (five-year cumulative +36.5% and +43.0%), so the six remaining sessions are mostly noise. Rounds 12–13 showed that data-driven factor search loses out of sample in every year. The retired Plan A is disclosed with a **negative holdout Sharpe (−0.30)** and deflated Sharpe 0.435, and the original v2/all21 backtest's 7.4% annual return / 0.52 Sharpe / −38% drawdown describes a different long-short book. None of these are competition performance; the PRD lists the data, cost and selection limitations.

## Submission

Follow `docs/submission-checklist.md`, with the owner-approved clarifications and status mapping in the PRD. The owner publishes judge-accessible materials and submits the form by the planned November 29 date. The official deadline is November 30, 2026, 23:59 SGT. No tokens, signed URLs or credentials belong in screenshots, logs, links or the repository.
