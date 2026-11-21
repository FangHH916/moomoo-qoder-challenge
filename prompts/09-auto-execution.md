# Prompt 09 — 自动执行：去掉 CONFIRM 和 ARM，改成随时可暂停（用户 2026-11-21 决定）

用法：把 EN 部分贴进 Qoder（做 07 的那个对话，或新开一个对话都行）。做完告诉 Claude，Claude 审代码、跑测试。周一 11-23 开盘后，再贴文末的"周一启动"那一句。

用户 11-21 的决定：
- 去掉 `CONFIRM` 和 `ARM`。策略一开始执行，就自动规划、下单、改价、对账，中途不问人。
- 混合执行：11-23 的补仓由本地 Qoder 做；11-27 的最终平仓和风控减仓由云端 agent 定时做（下一份 prompt，改版的 08）。
- 暂停开关：在 Slack #trading-agent 发 `PAUSE` / `RESUME`。暂停 = 不再发新单、撤掉挂单、持仓不动。Slack 监听在 08 里做，这份只做本地开关。
- 周一补仓用 11-19 的目标，只在 11-23 当天把 R10 的目标时效放宽到 1 个交易日。
- 规则门里的风控规则全部保留，只去掉人工批准这一步。

---

## EN（粘贴这段）

Change the go-live flow to automatic execution. I decided this on 2026-11-21. Do not place, cancel or modify any order now, and do not pause or resume any account now.

**What I decided**
- Drop the `CONFIRM` and `ARM` steps. Once I start a run, it goes from plan to reconcile through the deterministic gate without stopping to ask me.
- Execution is hybrid:
  - this Qoder Desktop chat runs the 2026-11-23 catch-up entry;
  - a cloud agent will run the 2026-11-27 final exit and the risk-reduction orders on a schedule. That comes in the next prompt. This prompt changes local code and docs only; no cloud change.
- My control is a pause switch: `PAUSE` / `RESUME` in Slack #trading-agent.
  - Pause means: send no new order, cancel every working order of this strategy, keep the positions.
  - Resume continues from the latest positions.
  - The Slack listener comes with the next prompt. This prompt builds the local switch and the checks.
- Every risk rule in the gate stays. Only the human approval steps go.

**1. Pause switch instead of ARM**
- Replace `golive.py arm` with:
  - `golive.py pause --account <id> --source "<text>"`;
  - `golive.py resume --account <id> --source "<text>"`.
- No typed reply is needed. `--source` records where the request came from, for example `slack PAUSE <message ts>` or `user in chat`. Both commands accept only the account of the current config mode and journal the event. Keep `halt` as an alias of `pause`.
- `pause` also writes a cancel list: every working order of this strategy (client order id prefix `pstar-`). The chat agent sends those cancels right away, without asking, then reconciles.
- A paused account rejects every ticket except `FINAL_EXIT` and `EMERGENCY_EXIT` reductions, as R16 already does. The scheduled final exit must still be able to close the book.
- Account 5832812 is running now (`halted=0` since 2026-11-20 18:47 UTC). Leave it running.
- R9 (the −8% drawdown latch) is unchanged.

**2. Evidence for automatic mode**
- `permission_policy_verified`: true, with basis "automatic execution decided by the user on 2026-11-21: every order passes the deterministic gate; the user can pause at any time with Slack `PAUSE`".
- `approval`:
  - `confirmed_by` = "user-started run";
  - `trigger` = `--trigger` ("user started the run" locally, "scheduled run" in the cloud later);
  - `basis` = "automatic mode decided by the user 2026-11-21: the independent check is the deterministic golive/gate run; the human controls are the pause switch and the Slack alerts".
- Remove the `CONFIRM` expiry rule. If the approval or the quotes expire before submission, rerun `evidence` and `plan` automatically. That sends nothing.

**3. Config for the 2026-11-23 catch-up**
- R10, one day only: on 2026-11-23, targets with `signal_date` 2026-11-19 may be 1 completed session old.
  - Make it a dated config entry, e.g. `"target_age_exceptions": [{"day": "2026-11-23", "signal_date": "2026-11-19", "max_age_sessions": 1, "decided_by": "user 2026-11-21"}]`.
  - Every other day keeps `max_target_age_sessions` 0.
  - R19 needs no change, because 2026-11-19 is a rebalance session (`rebalance_due` true).
- `quote_max_age_seconds` and `account_max_age_seconds`: 60 → 300.
  - Reason: moomoo `update_time` lags up to about 30 s even for QQQ, and relaying about 75 quotes takes longer than 60 s.
  - R7 still keeps every limit within 25 bps of its quote.
- `min_etf_market_value`: 500000000 → 300000000. UUP's fund size is about $483M, so the old threshold blocks a 28.9% target.
- The config checksum changes. Republish the 2026-11-19 competition targets under the new checksum, the same way 07 Part 2 did, and show me that every weight is unchanged.

**4. One automatic run (write it into `docs/GOLIVE.md` as "Automatic run")**
1. Check the pause switch. If the account is paused, send nothing and stop.
2. Borrow checks (`sim_trade_max_buy_sell`) for every planned short. They have no freshness limit.
3. Then, back to back:
   - account list, cash, positions and orders;
   - `snapshot`;
   - quotes for all names in as few calls as possible;
   - `evidence --trigger "user started the run"`;
   - **one `plan` for all names. Never plan one symbol at a time.**
4. Submit every ticket that passed, in plan order, without asking. `record` every response. Tickets that failed are not sent; list them.
5. If `plan` reports R11 or an expired approval, refresh and re-plan automatically.
6. Every 5 minutes:
   - check the pause switch;
   - fetch the orders;
   - `reprice` what has not filled, through the gate, and `record` each response.
   - At most 3 reprice rounds per name. The gate's 100-orders-a-day and competition limits stay as they are.
7. Stop at 15:45 ET, or when every ticket is filled or skipped. Cancel anything still working, `reconcile`, and post the summary here: filled, skipped and unfilled, gross long and short, margin, largest ticker, fees, orders used against both limits.
8. Order anomalies go into the summary and into `reports/live/golive/<date>/<account>/alerts.json`. The cloud monitor will post that file to Slack (next prompt). An anomaly is:
   - a broker rejection;
   - a ticket that fails the gate for a reason other than an expected skip (under one share, no borrow);
   - an order still unfilled after the last reprice, or partly filled;
   - a fill more than 25 bps from its arrival quote;
   - a position not in the targets, or any account other than 5832812;
   - orders used above 80% of the daily or the competition limit;
   - a tool error that stops the run.
- Order status codes: map every moomoo status code that appears in the order history, from the MCP documentation (4 is filled, 5 is closed with no fill). A partly filled order that was then cancelled is resolved, not unknown. It must not make `reconciled` false or block the next reprice round.
- If this Qoder Desktop chat would stop to ask me before an MCP tool call during the run, tell me which permission settings cause it. I will change them myself.

**5. Docs, tests and commit**
- `docs/GOLIVE.md`:
  - remove the `ARM` and `CONFIRM` steps;
  - add pause and resume, the automatic run, the 2026-11-23 exception and the anomaly list.
- `agents/AGENTS.md`: add a dated entry, "Automatic execution — decided by the user on 2026-11-21". It replaces the `CONFIRM` and `ARM` parts of the 2026-11-20 local-evidence entry.
- Tests (unittest):
  - `pause` and `resume` need no typed reply, and refuse the other mode's account;
  - a paused account rejects an entry ticket but passes a `FINAL_EXIT` reduction;
  - `pause` lists exactly the strategy's working orders;
  - the R10 exception passes 2026-11-19 targets on 2026-11-23 and rejects them on 2026-11-24;
  - one `plan` call produces tickets for all target names;
  - a cancelled partial fill is resolved, and `reconciled` stays true;
  - the new evidence basis text.
- Run all suites.
- Commit the code, the config, the republished targets, `journal/decisions.jsonl`, the docs, `agents/` and `prompts/` in one commit. Check `git status` first. Never commit `.env` or any credential.
- Show me the diff summary, the test results, the republished weights check and the commit hash.

---

## 中文对照

把上线流程改成自动执行。这是我 2026-11-21 定的。现在不要下单、撤单或改单，也不要暂停或恢复任何账户。

**我的决定**
- 去掉 `CONFIRM` 和 `ARM` 两步。我启动一次之后，从规划到对账都经过确定性的规则门自动完成，中途不停下来问我。
- 混合执行：
  - 这个 Qoder Desktop 对话负责 2026-11-23 的补仓；
  - 云端 agent 按定时任务负责 2026-11-27 的最终平仓和风控减仓。这部分在下一份 prompt 里做。这份只改本地代码和文档，不改云端。
- 我的控制手段是暂停开关：在 Slack #trading-agent 发 `PAUSE` / `RESUME`。
  - 暂停 = 不再发新单，撤掉本策略所有挂单，持仓不动。
  - 恢复 = 按最新持仓接着做。
  - Slack 监听在下一份 prompt 里做。这份只做本地开关和检查。
- 规则门里的风控规则全部保留，只去掉人工批准这一步。

**1. 用暂停开关取代 ARM**
- 把 `golive.py arm` 换成：
  - `golive.py pause --account <账户> --source "<文本>"`；
  - `golive.py resume --account <账户> --source "<文本>"`。
- 不需要我打字确认。`--source` 记录请求来自哪里，比如 `slack PAUSE <消息时间戳>` 或 `用户在对话里`。两个命令都只接受当前配置模式对应的账户，并记入交易日志。保留 `halt`，作为 `pause` 的别名。
- `pause` 同时输出一份撤单清单：本策略所有还在挂着的订单（client order id 以 `pstar-` 开头）。聊天 agent 马上发出这些撤单，不用问我，然后对账。
- 暂停中的账户拒绝所有订单票，只有 `FINAL_EXIT` 和 `EMERGENCY_EXIT` 的减仓单能过，R16 现在就是这样。定时的最终平仓必须仍然能把仓位平掉。
- 账户 5832812 现在处于运行状态（2026-11-20 18:47 UTC 起 `halted=0`），保持不变。
- R9（回撤 −8% 锁死）不变。

**2. 自动模式下的证据**
- `permission_policy_verified`：true，依据写"自动执行，用户 2026-11-21 决定：每一张单都经过确定性规则门；用户可以随时在 Slack 发 `PAUSE` 暂停"。
- `approval`：
  - `confirmed_by` = "user-started run"（用户启动的运行）；
  - `trigger` = `--trigger` 的值（本地写"用户启动了运行"，以后云端写"定时运行"）；
  - `basis` 写"自动模式，用户 2026-11-21 决定：独立检查就是确定性的 golive/规则门运行；人工控制是暂停开关和 Slack 警报"。
- 删掉 `CONFIRM` 过期规则。如果提交前批准或报价过期了，就自动重新跑 `evidence` 和 `plan`。这一步不会发出任何订单。

**3. 2026-11-23 补仓的配置**
- R10，只限一天：2026-11-23 当天，`signal_date` 为 2026-11-19 的目标允许过期 1 个已完成的交易日。
  - 做成带日期的配置项，例如 `"target_age_exceptions": [{"day": "2026-11-23", "signal_date": "2026-11-19", "max_age_sessions": 1, "decided_by": "user 2026-11-21"}]`。
  - 其他日子的 `max_target_age_sessions` 仍然是 0。
  - R19 不用改，因为 2026-11-19 本身就是调仓日（`rebalance_due` 为 true）。
- `quote_max_age_seconds` 和 `account_max_age_seconds`：60 → 300。
  - 原因：moomoo 的 `update_time` 即使是 QQQ 也会滞后约 30 秒，而转发约 75 个报价要花的时间超过 60 秒。
  - R7 仍然要求每个限价都在报价 25 个基点以内。
- `min_etf_market_value`：500000000 → 300000000。UUP 的基金规模约 4.83 亿美元，旧门槛会挡掉一个 28.9% 的目标仓位。
- 配置校验和会变。按 07 Part 2 的做法，用新的校验和重新发布 2026-11-19 的比赛目标，并给我看所有权重都没变。

**4. 一次自动运行（写进 `docs/GOLIVE.md`，标题 "Automatic run"）**
1. 检查暂停开关。如果账户在暂停中，什么都不发，直接停止。
2. 对所有计划中的空头做借券检查（`sim_trade_max_buy_sell`）。这一步没有时效要求。
3. 然后一口气做完：
   - 账户列表、现金、持仓、订单；
   - `snapshot`；
   - 用尽量少的调用拿到所有代码的报价；
   - `evidence --trigger "user started the run"`；
   - **对所有代码只跑一次 `plan`。绝不一只一只地规划。**
4. 按计划顺序提交所有通过规则门的订单票，不用问我。每个返回都要 `record`。没通过的不发，列出来。
5. 如果 `plan` 报 R11 或批准过期，自动刷新并重新规划。
6. 每 5 分钟：
   - 检查暂停开关；
   - 拉取订单；
   - 对没成交的做 `reprice`，经过规则门，每个返回都 `record`。
   - 每只最多改价 3 轮。规则门里每天 100 单和比赛总单数的上限保持不变。
7. 到 15:45 ET，或者所有订单票都已成交或跳过时停止。撤掉还挂着的单，`reconcile`，然后在这里发汇总：成交、跳过、未成交的数量，多头和空头总额，保证金，最大单只仓位，手续费，以及已用订单数和两个上限的对比。
8. 订单异常写进汇总，同时写进 `reports/live/golive/<日期>/<账户>/alerts.json`。云端监控会把这个文件发到 Slack（下一份 prompt）。以下算异常：
   - 券商拒单；
   - 订单票没过规则门，而且不是预期内的跳过（不足 1 股、借不到券）；
   - 最后一轮改价后仍未成交，或只部分成交；
   - 成交价偏离到达时报价超过 25 个基点；
   - 出现不在目标里的持仓，或出现 5832812 以外的账户；
   - 已用订单数超过每日上限或比赛上限的 80%；
   - 工具报错导致运行中断。
- 订单状态码：按 MCP 文档，把订单历史里出现的每个 moomoo 状态码都映射清楚（4 是全部成交，5 是关闭且未成交）。部分成交后被撤掉的订单算已了结，不算未知。它不能让 `reconciled` 变成 false，也不能挡住下一轮改价。
- 运行中，如果这个 Qoder Desktop 对话会在调用 MCP 工具前停下来问我，告诉我是哪些权限设置造成的。我自己去改。

**5. 文档、测试和提交**
- `docs/GOLIVE.md`：
  - 删掉 `ARM` 和 `CONFIRM` 步骤；
  - 加上暂停和恢复、自动运行、2026-11-23 例外和异常清单。
- `agents/AGENTS.md`：加一条带日期的记录，"自动执行——由用户于 2026-11-21 决定"。它取代 2026-11-20 本地证据记录里关于 `CONFIRM` 和 `ARM` 的部分。
- 测试（unittest）：
  - `pause` 和 `resume` 不需要打字确认，并且拒绝另一个模式的账户；
  - 暂停中的账户拒绝开仓单，但放行 `FINAL_EXIT` 减仓单；
  - `pause` 列出的正好是本策略的挂单；
  - R10 例外在 2026-11-23 放行 2026-11-19 的目标，在 2026-11-24 拒绝；
  - 一次 `plan` 调用就为所有目标代码生成订单票；
  - 部分成交后被撤掉的订单算已了结，`reconciled` 仍为 true；
  - 新的证据依据文字。
- 跑全部测试。
- 然后把代码、配置、重新发布的目标、`journal/decisions.jsonl`、文档、`agents/` 和 `prompts/` 一起提交成一个 commit。先看 `git status`；绝不提交 `.env` 或任何凭证。
- 给我看改动摘要、测试结果、重新发布后的权重核对和 commit 哈希。

---

## 周一启动（11-23 22:35 SGT 之后贴这一句）

EN：
> Start the 2026-11-23 catch-up entry on 5832812, following "Automatic run" in `docs/GOLIVE.md`. Do not ask me to confirm anything. Run it to the end and post the summary.

中文对照：
> 按 `docs/GOLIVE.md` 里的 "Automatic run"，在 5832812 上开始 2026-11-23 的补仓。任何事都不用问我确认。一直做到结束，然后发汇总。
