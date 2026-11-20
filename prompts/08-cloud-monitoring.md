# Prompt 08 — 云端 agent：只读风控监控 + Slack 报告（用户 11-20 定：1A；27 号收盘前全部平仓）

用户 11-20 的两个决定：
- **1A**：云端 agent 只读，不能下单。它们只做三件事：定时检查风险、往 Slack 发日报、出问题发警报并附上该下的减仓单。所有下单都在本地 Qoder 做，由用户回 `CONFIRM`。
- **最后一天平仓**（11-20 改定，取代原来的"2A 不平仓"）：比赛社群管理员 Alwin 在 WhatsApp 说，27 号收盘前要平掉所有仓位，盈利才算最终收益。所以 11-27 美东 12:15–12:45（新加坡时间 11-28 凌晨 1:15–1:45）在本地全部平仓，用户回 `CONFIRM`。−8% 回撤熔断照样全平。

用法：
- 在 prompt 07 的 Part 1 做完之后，贴进 Qoder 新对话，可以在今晚上线之后或者明天白天。
- Qoder 每做一步云端改动都会先给你看、等你回 `APPROVE`。Vault 的 OAuth 授权、PAT 这类涉及登录和密钥的步骤，由你自己在控制台完成，不要把任何 token 贴进对话。
- 目标：11-23（周一）美股开盘前，云端监控开始运行。

---

## EN（粘贴这段）

Goal: put the Qoder Cloud Agents part of the project into production as a **read-only risk-monitoring and reporting team**. Execution stays local: the Qoder Desktop chat with `live/golive.py`, after I reply `CONFIRM`.

Two decisions I made on 2026-11-20 override older specs:
1. **Cloud roles never place or cancel orders.** No cloud role gets `sim_trade_input_order`, `sim_trade_cancel_order`, or any `trading_*` / `account_*` tool. Cloud roles read the paper account and market data, check risk deterministically, and report to Slack. When a rule is breached, they post an ALERT that contains the exact proposed reduce-only order. I then execute it locally.
2. **Final exit on 2026-11-27, before the 13:00 ET early close.** On 2026-11-20 a community admin of the competition said that all positions must be closed by the close on the 27th for the result to count as the final return. So every position is closed between 12:15 and 12:45 ET on 11-27 (01:15–01:45 SGT on 11-28). This runs locally, after my `CONFIRM`, like any other order. It replaces my earlier "hold through the close" decision. The −8% drawdown halt still flattens everything.

Read first: `agents/AGENTS.md`, `agents/templates.json`, `agents/*.md`, `live/schedule-plan.json`, `live/config.pstar.json`, `rules/gate.py`, `skills/*`, `agents/package_skills.py`, `agents/evaluations.json`, `docs/GOLIVE.md`, and the Forward-mode notes in `notes/handbook-notes.md` (§4–§6 and the Qoder deck section).

### Part 0 — local changes (no cloud calls)
1. **Final exit on 11-27, run locally.**
   - Keep `final_exit_start` (12:15 ET) and `final_flatten_at` (12:45 ET) in `live/config.pstar.json`. Make `live/schedule-plan.json`, the gate and the docs agree with them. There is no rebalance on 11-27.
   - Add `golive.py plan --final-exit`:
     - close every position on 5832812 in one batch, ETFs and shorts together: SELL longs, BUY to cover shorts, full quantity from a fresh positions snapshot;
     - marketable limit orders, through the gate, after my `CONFIRM`;
     - reprice every 5 minutes;
     - at 12:45 ET, turn whatever is still open into market orders, after one more `CONFIRM`;
     - by 12:55 ET, a reconcile must show zero positions, or list exactly what is left.
   - Before the final exit, print the order budget against the 200-order competition cap: orders already submitted in the competition, plus the exit tickets, plus a reserve for reprices.
   - Tests:
     - closing quantities equal the held quantities;
     - no ticket opens a new position;
     - the budget check;
     - the 12:45 switch to market orders.
   - Add the 11-27 steps to `docs/GOLIVE.md`.
2. **New skill `pstar-risk-check`**: `SKILL.md` plus a Python script that uses the standard library only. The script reads saved raw MCP responses (cash info, positions, order history) for account 5832812 and writes `risk_report.json` plus a short Slack-ready text. Checks (same formulas and constants as `rules/gate.py`):
   - **ALERT** when:
     - any ticker is above 39% of initial assets ($39,000). Propose a trim to $38,000: SELL for a long, BUY to cover for a short, quantity = ceil(excess / price).
     - margin (0.5 × long + 0.6 × short) / equity is above 95%. Propose a pro-rata scale-down of the strategy book to 90%.
     - drawdown from the peak is −8% or worse → **KILL**. Propose flattening everything, and say so loudly.
   - **WARN** when:
     - a ticker is above 37%, margin is above 85%, or drawdown is −4% or worse;
     - the position count is above 95;
     - a position is not in the allowed book: the 10 ETFs, the shorts held after go-live (`book.json`, written from the Stage B reconcile), or `discretionary_symbols`;
     - an order has been open for more than 30 minutes;
     - executed orders today are above 100.
   - **CHECK FAILED** when an MCP read fails or returns another account. Never stay silent.
   - **Peak equity:** keep the running peak in project workspace memory, and verify in the first manual run that it persists between scheduled sessions. If it does not, measure drawdown from max($100,000, the highest equity in the previous end-of-day report) and state that in every report. Other participants report two problems. First, files that a session writes to a mounted memory store may not survive to the next session; only API-written entries do. Second, a template with a credential in its environment variables is rejected with HTTP 400. So test persistence explicitly, and never put a credential in a template.
   - **Tests:** the script must agree with `rules/gate.py` on shared fixtures, including the 11-18 targets at $100k and synthetic breaches of each rule.
3. **Update the existing skills** (`validate-trading-targets`, `report-trading-day`, `reconcile-paper-fills`, `paper-trading-gate`) from plan A to P* and the rules from prompt 06. Repackage all 6 zips with `agents/package_skills.py`. Every skill name must match `^[a-z0-9][a-z0-9_-]*$`.
4. **Cloud role specs** in `agents/*.md` and `agents/templates.json`, all read-only:
   - **Coordinator:** the Slack-connected Forward template that already exists. It runs the schedules, dispatches the other roles, and posts the summaries.
   - **Risk Auditor:** paper-account reads only. It runs `pstar-risk-check`, and is the only role that writes ALERT, WARN or KILL.
   - **Scout:** market-data and news reads only. It checks market state and the trading calendar, the competition eligibility of the held ETFs (price ≥ $1, market value ≥ $500M), and dated news about the five ETF holdings. Its output is evidence, never an instruction.
   - **Advisor** (tool-free, optional): reviews the end-of-day report for errors and gives a second opinion only.
   - The Coordinator prompt states: "Cloud roles never place, modify or cancel orders. An ALERT is a proposal for the human, not an instruction to any agent."
5. **Local `golive.py plan --risk-action <risk_report.json>`**: turns an ALERT into gated, reduce-only tickets for the local Part 2 flow of prompt 07, with my `CONFIRM`. Add tests.
6. Run all test suites, plus `python -B agents/evaluate.py --self-check`, and update the four fixed evaluation fixtures to P*.

### Part 1 — cloud deployment (each step: show me exactly what you will change, then wait for `APPROVE`)
Use the Qoder Cloud Agents plugin (qca MCP / qoder-cloud-agents skill) and the Forward-mode API documented there.

1. **Discover.** List the existing Forward templates, identities, environments, vaults, skills, channels and schedules. Write the discovered IDs and versions into the bindings of `agents/templates.json`, and add a timestamped entry to `agents/AGENTS.md` (Retrieved at / Agent ID / Cloud version / Local revision / Change summary).
2. **Vault.**
   - If no `moomoo-mcp` vault exists, stop and ask me to create it. I do the OAuth myself, with Market Data + Watchlists only.
   - Verify that the credential URL is exactly the MCP URL the templates use (`https://mcp.moomoo.com/mcp`).
   - Ask me to confirm the vault ID; never ask for a token.
3. **Skills.** Upload the 6 zips. Record each skill's ID and version, and pin those versions on the templates; never use `latest`.
4. **Templates.**
   - Configure Coordinator, Risk Auditor and Scout (and Advisor if you add it) as specified.
   - Attach the vault only to the roles that read moomoo.
   - Deny the order tools (`sim_trade_input_order`, `sim_trade_cancel_order`, and every `trading_*` / `account_*` tool) with the platform's tool policy. If the platform cannot deny a tool, set it to always-ask (nobody will approve it), and record that as a limitation.
   - Enable Collaboration for child dispatch.
   - Use a light model for the 30-minute checks and a stronger one for the end-of-day report.
5. **Test in a new session.**
   - Ask the cloud agent: "List my paper accounts and confirm you can access the challenge account."
   - Run one read-only risk check on 5832812 and post it to Slack.
   - Run the four fixed evaluation prompts against the cloud roles, and show me the behavioural diff against the local results.
   - Prove that an order tool is denied: ask the Risk Auditor to place an order and show me that it is refused.
6. **Schedules.**
   - Timezone `America/New_York`; start 2026-11-23, stop 2026-11-27 13:30; `new_session`; `max_concurrent_runs: 1`. Instructions must be self-contained, because each run starts without chat history.
   - Jobs:
     - post-open check `45 9 * * 1-5` (Coordinator → Scout + Risk Auditor; full report);
     - risk check `0,30 10-15 * * 1-5` (Risk Auditor; post only on WARN, ALERT, KILL or CHECK FAILED, plus a one-line heartbeat at 12:00);
     - end-of-day report `10 16 * * 1-5`;
     - final-exit reminder once at `2026-11-27T12:05:00-05:00`: post to Slack that the local final exit opens at 12:15 ET, with the current positions and the order budget;
     - position check once at `2026-11-27T12:50:00-05:00`: ALERT if any position is still open, listing each one;
     - final report once at `2026-11-27T13:10:00-05:00`, which must state whether the account was flat at the close.
   - Every run checks the exchange calendar first: skip 11-26; on 11-27, skip everything after 13:00 except the final report.
   - Create the schedules so that results land in Slack `#trading-agent`. Prefer creating them from the Slack channel; draft the exact messages for me to send if that is the documented way. Otherwise use the API with a Slack sink.
   - Trigger each job once manually and show me the output.
   - Record the schedule IDs, the rules and the next trigger times.
7. **Document.** Update `agents/AGENTS.md`, `README.md` and `docs/PRD.md`: who does what (local research and execution with human `CONFIRM`; cloud read-only monitoring), the limits ("an alert is not a stop order"; the gap between checks), and the credits used. If time allows, add a cloud panel to the dashboard (sessions and schedule runs, read with the PAT from `.env` on the server side only).

Never place, modify or cancel orders. Never give a cloud role an order tool. Never write a token, PAT or OAuth secret into a file, a prompt, a log or a commit. Show me the diff summary, the test results and the cloud resource list when done.

---

## 中文对照

目标：把项目里 Qoder Cloud Agents 的部分正式上线，做成一个**只读的风控监控和报告团队**。下单仍然在本地：Qoder Desktop 对话加 `live/golive.py`，在我回复 `CONFIRM` 之后执行。

我在 2026-11-20 做的两个决定，优先于以前的规格：
1. **云端角色从不下单、也不撤单。** 任何云端角色都不给 `sim_trade_input_order`、`sim_trade_cancel_order`，也不给任何 `trading_*` / `account_*` 工具。云端角色只读模拟账户和行情，按确定性规则检查风险，并报告到 Slack。规则被触发时，它们发一条 ALERT，里面写明该下的、只减不增的单。然后由我在本地执行。
2. **2026-11-27 在 13:00 ET 提前收盘之前全部平仓。** 2026-11-20，比赛社群的一位管理员说，27 号收盘前必须平掉所有仓位，成绩才算最终收益。所以 11-27 美东 12:15 到 12:45（新加坡时间 11-28 凌晨 1:15–1:45）之间平掉所有仓位。平仓在本地执行，和其他订单一样要等我回 `CONFIRM`。这一条取代我之前"持有到收盘"的决定。−8% 回撤熔断仍然全部平仓。

先读：`agents/AGENTS.md`、`agents/templates.json`、`agents/*.md`、`live/schedule-plan.json`、`live/config.pstar.json`、`rules/gate.py`、`skills/*`、`agents/package_skills.py`、`agents/evaluations.json`、`docs/GOLIVE.md`，以及 `notes/handbook-notes.md` 里关于 Forward 模式的笔记（§4–§6 和 Qoder deck 部分）。

### 第 0 部分 — 本地改动（不调用云端）
1. **11-27 最后平仓，在本地执行。**
   - 保留 `live/config.pstar.json` 里的 `final_exit_start`（12:15 ET）和 `final_flatten_at`（12:45 ET）。让 `live/schedule-plan.json`、规则门和文档都与它们一致。11-27 不调仓。
   - 新增 `golive.py plan --final-exit`：
     - 一次性平掉 5832812 上的所有仓位，ETF 和空头一起：多头卖出，空头买回，数量取最新持仓快照里的全部数量；
     - 用能立即成交的限价单，过规则门，等我回 `CONFIRM`；
     - 每 5 分钟改价一次；
     - 到 12:45 ET 还没成交的，改成市价单，需要我再回一次 `CONFIRM`；
     - 12:55 ET 之前对账必须显示零持仓，否则逐条列出还剩什么。
   - 平仓之前，先打印订单额度，和比赛 200 笔的上限对比：比赛中已经提交的订单数，加上平仓单，再加上为改价预留的笔数。
   - 测试：
     - 平仓数量等于持有数量；
     - 没有任何一张票会开新仓；
     - 订单额度检查；
     - 12:45 改成市价单的切换。
   - 把 11-27 的步骤写进 `docs/GOLIVE.md`。
2. **新 skill `pstar-risk-check`**：`SKILL.md` 加一个只用标准库的 Python 脚本。脚本读取账户 5832812 已保存的 MCP 原始返回（现金、持仓、订单历史），输出 `risk_report.json` 和一段可以直接发 Slack 的短文。检查项（公式和常数与 `rules/gate.py` 相同）：
   - **ALERT**（警报）：
     - 任一代码超过初始资产的 39%（$39,000）。建议减到 $38,000：多头卖出，空头买回，数量 = 超出金额 ÷ 价格，向上取整。
     - 保证金（0.5 × 多头 + 0.6 × 空头）÷ 权益超过 95%。建议把策略仓位按比例缩到 90%。
     - 从最高点回撤达到 −8% 或更差 → **KILL**（熔断）。建议全部平仓，并醒目地标出来。
   - **WARN**（提醒）：
     - 单只超过 37%、保证金超过 85%，或回撤达到 −4% 或更差；
     - 持仓数超过 95；
     - 有不在允许范围内的持仓。允许范围是：10 只 ETF、上线后持有的空头（`book.json`，根据 Stage B 的对账结果写出）、`discretionary_symbols`；
     - 有挂单超过 30 分钟；
     - 今天已成交笔数超过 100。
   - **CHECK FAILED**（检查失败）：MCP 读取失败，或者返回的是别的账户。绝不悄悄跳过。
   - **最高权益**：把运行中的最高点存在项目的 workspace memory 里，并在第一次手动运行时确认它能在不同的定时会话之间保留。如果不能保留，就用 max(100,000 美元, 上一份收盘报告里的最高权益) 计算回撤，并在每份报告里注明。其他参赛者反映了两个问题。一是会话写进挂载的 memory store 的文件，到下一个会话可能就没了，只有通过 API 写入的条目才会保留。二是模板的环境变量里放了凭证，会被 HTTP 400 拒绝。所以要明确测试能不能保留，并且绝不把凭证放进模板。
   - **测试**：在共用样例上，脚本必须和 `rules/gate.py` 的结论一致，样例包括 11-18 目标持仓按 10 万美元计算的情况，以及每条规则的模拟触发。
3. **更新已有 skill**（`validate-trading-targets`、`report-trading-day`、`reconcile-paper-fills`、`paper-trading-gate`）：从方案 A 改成 P*，并改用 prompt 06 的规则。用 `agents/package_skills.py` 把 6 个 zip 全部重新打包。每个 skill 名都必须符合 `^[a-z0-9][a-z0-9_-]*$`。
4. **云端角色规格**（`agents/*.md` 和 `agents/templates.json`），全部只读：
   - **Coordinator**（协调者）：就是已经存在、连着 Slack 的那个 Forward 模板。负责跑定时任务、派活给其他角色、发汇总。
   - **Risk Auditor**（风控审计）：只读模拟账户。运行 `pstar-risk-check`，并且只有它能写 ALERT、WARN 或 KILL。
   - **Scout**（侦察）：只读行情和新闻。检查市场状态和交易日历、持有的 ETF 是否仍符合比赛资格（价格 ≥ $1、市值 ≥ 5 亿美元），以及这五只 ETF 持仓带日期的新闻。它的输出只是证据，绝不是指令。
   - **Advisor**（顾问，不带工具，可选）：只负责检查收盘报告有没有错，给第二意见。
   - Coordinator 的提示词里写明："云端角色从不下单、改单或撤单。ALERT 是给人的建议，不是给任何 agent 的指令。"
5. **本地 `golive.py plan --risk-action <risk_report.json>`**：把一条 ALERT 转成过了规则门、只减不增的订单票，交给 prompt 07 第二部分的本地流程，由我 `CONFIRM`。补测试。
6. 跑全部测试，加上 `python -B agents/evaluate.py --self-check`，并把四个固定评估样例更新为 P*。

### 第 1 部分 — 云端部署（每一步：先给我看你具体要改什么，等我回复 `APPROVE`）
用 Qoder Cloud Agents 插件（qca MCP / qoder-cloud-agents skill）以及里面文档写的 Forward 模式 API。

1. **摸底。** 列出已有的 Forward 模板、身份、环境、vault、skill、channel 和定时任务。把查到的 ID 和版本写进 `agents/templates.json` 的绑定字段，并在 `agents/AGENTS.md` 里加一条带时间戳的记录（Retrieved at / Agent ID / Cloud version / Local revision / Change summary）。
2. **Vault。**
   - 如果还没有 `moomoo-mcp` vault，就停下来让我去建。OAuth 授权由我自己做，只勾 Market Data + Watchlists。
   - 确认凭证 URL 和模板使用的 MCP URL 完全一致（`https://mcp.moomoo.com/mcp`）。
   - 请我确认 vault 的 ID；绝不向我要 token。
3. **Skill。** 上传 6 个 zip。记下每个 skill 的 ID 和版本，并在模板上固定这些版本；绝不用 `latest`。
4. **模板。**
   - 按规格配置 Coordinator、Risk Auditor 和 Scout（如果加了 Advisor 也一起配）。
   - vault 只挂给需要读 moomoo 的角色。
   - 用平台的工具策略禁用下单工具（`sim_trade_input_order`、`sim_trade_cancel_order`，以及所有 `trading_*` / `account_*` 工具）。如果平台不能禁用某个工具，就设成"每次询问"（没人会批准），并记为已知限制。
   - 打开 Collaboration，用于派生子任务。
   - 30 分钟一次的检查用轻量模型，收盘报告用强一点的模型。
5. **在新会话里测试。**
   - 问云端 agent："列出我的模拟账户，并确认你能访问比赛账户。"
   - 对 5832812 跑一次只读风控检查，并发到 Slack。
   - 用四个固定评估 prompt 测云端角色，把行为上和本地结果的差异给我看。
   - 证明下单工具确实被禁用：让 Risk Auditor 下一笔单，给我看它被拒绝。
6. **定时任务。**
   - 时区 `America/New_York`；从 2026-11-23 开始，到 2026-11-27 13:30 结束；`new_session`；`max_concurrent_runs: 1`。指令必须自成一体，因为每次运行都不带聊天记录。
   - 任务：
     - 开盘后检查 `45 9 * * 1-5`（Coordinator → Scout + Risk Auditor；完整报告）；
     - 风控检查 `0,30 10-15 * * 1-5`（Risk Auditor；只在 WARN、ALERT、KILL 或 CHECK FAILED 时发消息，另外 12:00 发一行心跳）；
     - 收盘报告 `10 16 * * 1-5`；
     - 平仓提醒，在 `2026-11-27T12:05:00-05:00` 运行一次：往 Slack 发消息，说明本地最后平仓 12:15 ET 开始，附上当前持仓和订单额度；
     - 持仓检查，在 `2026-11-27T12:50:00-05:00` 运行一次：如果还有任何未平仓位，发 ALERT，逐条列出；
     - 最终报告，在 `2026-11-27T13:10:00-05:00` 运行一次，必须写明收盘时账户是否已经全部平仓。
   - 每次运行先查交易日历：11-26 跳过；11-27 在 13:00 之后，除最终报告外全部跳过。
   - 建定时任务时要让结果发到 Slack `#trading-agent`。优先从 Slack 频道里创建；如果文档写的就是这种方式，把要发的消息原文给我，我去发。否则用 API 并配 Slack sink。
   - 每个任务手动触发一次，把输出给我看。
   - 记下定时任务的 ID、规则和下次触发时间。
7. **文档。** 更新 `agents/AGENTS.md`、`README.md` 和 `docs/PRD.md`：谁做什么（本地负责研究和执行，执行需要人工 `CONFIRM`；云端只读监控）；限制（"警报不是止损单"、两次检查之间有空档）；以及用掉的 credits。如果时间允许，在 dashboard 加一个云端面板（会话和定时任务运行情况，只在服务端用 `.env` 里的 PAT 读取）。

绝不下单、改单或撤单。绝不给云端角色下单工具。绝不把任何 token、PAT 或 OAuth 密钥写进文件、提示词、日志或提交记录。做完把改动摘要、测试结果和云端资源清单给我看。
