# Prompt 11（11-22）— 08 上线后更新报告，重新发布到同一网址

用法：
- **等 08 的 Qoder 对话做完 B 部分**（云端部署、测试、定时任务、文档都做完）再贴。贴之前把 08 对话的最终汇报转给 Claude 看一眼。
- 贴到**做报告（prompt 10）的那个 Qoder 对话**里；那个对话关了的话，新开一个也行，它会从仓库读现状。
- 只贴下面 EN 部分（从 `Goal:` 到 `...the published URL when done.`）。
- Qoder 提交后，由 Claude 推到 GitHub。

---

## EN（粘贴这段）

Goal: refresh the submission deck now that the cloud part of prompt 08 is live, and republish it to the same URL.

**What changed since the deck was built**
- On 2026-11-21 I made execution automatic: no `CONFIRM`, no `ARM`. Prompt 08 then put this into the cloud:
  - a cloud Executor runs the 2026-11-27 final exit and the risk reductions on schedule;
  - a Slack `PAUSE` / `RESUME` from my own account controls both the local run and the cloud;
  - order anomalies are posted to Slack.
- Guard was renamed Executor.
- On 2026-11-22 the form answer Q7 in `docs/submission/report-content.md` was replaced with a version without performance figures (commit 5598503). Keep it as it is.
- The code is now public at https://github.com/FangHH916/moomoo-qoder-challenge (MIT license).

**Before you start**
1. If `docs/submission/` has uncommitted edits of yours (`build_report.py`, `site/index.html`), commit them or discard them, and tell me which.
2. Read the current state, not the old prompts:
   - `agents/AGENTS.md`, newest dated entries first;
   - `agents/templates.json`, `agents/release-lock.json`, `agents/*.md`;
   - `live/ops.json`, `live/schedule-plan.json`;
   - `docs/GOLIVE.md`, `README.md`, `docs/PRD.md`.

**Changes**
1. Update the slides that describe the cloud: the TL;DR; Agent architecture; Automation, and the control left to the human; What worked, what failed, what next; How we used Qoder; and the judging-criteria appendix.
   - Remove every statement that the cloud is read-only, has `can_execute: false`, or is "being deployed" when the repository now records it as live. Rename Guard to Executor everywhere.
   - The roles:
     - **Coordinator**: Slack and the schedules. It accepts `PAUSE` / `RESUME` only from my account, and only as the whole message.
     - **Risk Auditor**: runs `pstar-risk-check`. It is the only role that writes KILL, ALERT, WARN or ORDER ANOMALY, and the only one that approves the Executor.
     - **Executor**: the only role with order tools, for account 5832812 only. It sends only gate-passed golive plans: the final exit, risk actions after an Auditor ALERT or KILL, and pause cancels.
     - **Scout**: read-only market data and news.
     - **Advisor**: tool-free. Include it only if it is deployed.
   - The pause path:
     1. Slack `PAUSE` reaches the Coordinator.
     2. The Coordinator sets the shared `PSTAR-PAUSE` watchlist flag, which carries the `local-entry` / `cloud-executor` lease.
     3. The local run and the Executor both stop sending new orders and cancel their working `pstar-` orders.
     4. Positions stay. `FINAL_EXIT` and `EMERGENCY_EXIT` still pass.
   - The schedule (America/New_York; 11-26 is skipped):
     - post-open check at 09:45;
     - risk and order check every 30 minutes, 10:00–15:30;
     - end-of-day report at 16:10;
     - final exit on 2026-11-27 from 12:15. Anything still open at 12:45 goes out as a MARKET order, the only MARKET orders allowed;
     - flat check at 12:50;
     - final report at 13:10.
   - State the limits plainly:
     - an alert is not a stop order;
     - there can be up to 30 minutes between checks;
     - a pause reaches a local run at its next round.
2. **Evidence.** Every cloud claim must come from a file in the repository: an `AGENTS.md` entry, a binding in `templates.json`, or a test log.
   - If the repository does not prove a step, label it "not yet verified" on the slide instead of claiming it. Examples: the PAUSE round trip, the denial tests, the final-exit dry run.
   - Give me the list of cloud claims, with the file each one comes from.
3. Add the GitHub link to the title slide and to the "Reproduce it" slide.
4. Update `docs/submission/report-content.md` §5 and §6 to match: these are the statements the prompt 08 chat listed as out of date. Do not change the Q5 or Q7 answers.
5. **No live results.** Add nothing from the competition account: no returns, no P&L, no equity, no position values. The backtest slides stay as they are.
6. **Build checks.** In `build_report.py`, add a check that fails the build if the deck still calls the cloud read-only, says `can_execute: false`, or names Guard. Keep every existing check. Rebuild and show me the output of the checks.
7. **Privacy, as before.** No name, email, phone number, token, vault ID, session ID or schedule ID. The GitHub link is allowed.
8. **Republish** to the same site, https://trading-agent-deck-w89b43az0ul.qoder.website/, still visible to "anyone with the link". Do not create a new site. Then open it without logging in, and confirm that the URL is unchanged and the new content is there.
9. **Commit** only `docs/submission/` and `prompts/`. Do not push; I push to GitHub myself.

When done, show me the slide-by-slide summary of changes, the check output, and the published URL.

---

## 中文对照

目标：08 的云端部分已经上线，更新提交用的报告，并重新发布到同一个网址。

**报告做完之后变了什么**
- 2026-11-21 我把执行改成全自动：没有 `CONFIRM`，也没有 `ARM`。之后 prompt 08 把这一点落实到了云端：
  - 云端 Executor 按定时任务执行 2026-11-27 的最终平仓和风控减仓；
  - 我本人在 Slack 发的 `PAUSE` / `RESUME` 同时控制本地运行和云端；
  - 异常订单会发到 Slack。
- Guard 改名为 Executor。
- 2026-11-22，`docs/submission/report-content.md` 里的表单答案 Q7 换成了不带收益数字的版本（提交 5598503）。保持原样，不要改。
- 代码已经公开在 https://github.com/FangHH916/moomoo-qoder-challenge（MIT 许可证）。

**开始之前**
1. 如果 `docs/submission/` 里有你之前没提交的改动（`build_report.py`、`site/index.html`），先提交或丢弃，并告诉我选了哪个。
2. 读仓库的现状，不要按旧 prompt：
   - `agents/AGENTS.md`，先看最新的带日期条目；
   - `agents/templates.json`、`agents/release-lock.json`、`agents/*.md`；
   - `live/ops.json`、`live/schedule-plan.json`；
   - `docs/GOLIVE.md`、`README.md`、`docs/PRD.md`。

**要改的**
1. 更新讲云端的几页：TL;DR、Agent architecture、Automation, and the control left to the human、What worked, what failed, what next、How we used Qoder，以及评分对照附录。
   - 删掉所有"云端只读"、`can_execute: false` 的说法；仓库里已经记录为上线的部分，也不要再写"being deployed"。所有 Guard 都改成 Executor。
   - 各角色：
     - **Coordinator**：负责 Slack 和定时任务。`PAUSE` / `RESUME` 只认我的账号，而且必须是整条消息。
     - **Risk Auditor**：运行 `pstar-risk-check`。只有它能写 KILL、ALERT、WARN、ORDER ANOMALY，也只有它能批准 Executor。
     - **Executor**：唯一有下单工具的角色，只能用于账户 5832812。它只发通过闸门的 golive 计划：最终平仓、Auditor 发出 ALERT 或 KILL 后的风控单、暂停撤单。
     - **Scout**：只读行情和新闻。
     - **Advisor**：没有任何工具，真的部署了才写。
   - 暂停路径：
     1. Slack 的 `PAUSE` 先到 Coordinator。
     2. Coordinator 设置共享的 `PSTAR-PAUSE` 自选股开关，开关上带着 `local-entry` / `cloud-executor` 租约。
     3. 本地运行和 Executor 都停止发新单，并撤掉自己挂着的 `pstar-` 订单。
     4. 持仓不动。`FINAL_EXIT` 和 `EMERGENCY_EXIT` 照样放行。
   - 定时任务（美东时间，11-26 跳过）：
     - 开盘后检查 09:45；
     - 10:00–15:30 每 30 分钟检查一次风险和订单；
     - 收盘报告 16:10；
     - 2026-11-27 12:15 开始最终平仓，12:45 还没成交的发市价单，这是唯一允许的市价单；
     - 12:50 检查是否已全部平仓；
     - 13:10 发最终报告。
   - 直白写出局限：
     - 警报不等于止损单；
     - 两次检查之间最长可能隔 30 分钟；
     - 本地运行要到下一轮才会收到暂停。
2. **证据。** 每条关于云端的说法都必须来自仓库里的文件：`AGENTS.md` 的条目、`templates.json` 里的绑定，或者测试日志。
   - 仓库证明不了的步骤，在页面上标"not yet verified"（尚未验证），不要说成已完成。比如：PAUSE 来回测试、禁止下单测试、最终平仓演练。
   - 把所有云端说法列给我，每条注明出自哪个文件。
3. 在标题页和 "Reproduce it" 页加上 GitHub 链接。
4. 相应更新 `docs/submission/report-content.md` 的 §5 和 §6，也就是 08 对话列出的已过时说法。Q5 和 Q7 的答案不要改。
5. **不放实盘结果。** 比赛账户的任何数字都不加：不写收益、盈亏、权益，也不写持仓金额。回测那几页保持不变。
6. **构建检查。** 在 `build_report.py` 里加一条检查：报告里只要还说云端只读、写着 `can_execute: false`，或者出现 Guard，构建就失败。原有的检查全部保留。重新构建，把检查输出给我看。
7. **隐私规则照旧。** 不出现姓名、邮箱、电话、token、vault ID、session ID、schedule ID。GitHub 链接可以出现。
8. **重新发布**到同一个站点 https://trading-agent-deck-w89b43az0ul.qoder.website/，仍然设成"anyone with the link"（任何有链接的人可看）。不要新建站点。发布后在不登录的状态下打开，确认网址没变、内容已经更新。
9. **提交**只提交 `docs/submission/` 和 `prompts/`。不要 push，我自己推到 GitHub。

做完后给我：每页改了什么的摘要、检查输出，以及发布后的网址。
