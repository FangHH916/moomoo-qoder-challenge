# Prompt 02 — 多因子版 PRD + 项目骨架（取代 01-prd.md）

用法：Qoder Desktop 打开 D:\Python\moomoo-qoder-challenge 作为工作区，新开对话，只粘贴下面 EN 部分。
Qoder 会分轮问问题 → 把它的问题贴给 Claude，Claude 帮你回答 → 它写完 docs/PRD.md 后告诉 Claude 检查。

---

## EN（粘贴这段）

Help me write the PRD and scaffold the project for my entry in the Moomoo × Qoder AI Trading Agent Challenge: US-only paper trading, agents on Qoder Cloud Agents (Forward Mode) connected to the Moomoo MCP and Moomoo skills, plus a local scoring job. Recommend the multi-agent design and a template for each agent.

Read these first (they are the source of truth):
- `notes/handbook-notes.md` — official handbook, T&C, FAQ and both kickoff decks, summarised. The handbook governs; flag any conflict.
- `docs/submission-checklist.md` — the submission form and every bonus item we want to hit (sections A–D). Design so that each item is covered.
- `research/` and `reports/` — the finished research: factor code (`factors.py`), panel/cleaning (`panel.py`), preprocessing + LASSO + backtests (`pipeline.py`, `pipeline_v2.py`, `pipeline_v21.py`, `pipeline_v3.py`) and their results (`reports/backtest*/metrics.json`, `sweep.csv`, `single_factor.csv`).

Write everything in English. Create `docs/PRD.md`, `agents/` (one spec per agent + `AGENTS.md` with a timestamped version log as in Handbook §8.1), `skills/` (our own reusable skills), `rules/` (deterministic rule gate + tests), `live/` (production scoring job), `journal/`, `dashboard/`, `reports/`.

### Decided — do not change without asking me

**Strategy (from the research, already backtested 2021-11 → 2026-11):**
- Cross-sectional multi-factor long-short on US common stocks allowed by the competition (price ≥ $1, market cap ≥ $500M, plus a liquidity floor of 20-day median dollar volume ≥ $5M). ETFs are not scored; SPY is used only as the beta hedge.
- Factors: 16 ported from my A-share library (PV/MD/DC families, DC ones are approximations) + US classics (12-1 momentum, 52-week-high proximity, idiosyncratic volatility, short-term reversal, Amihud) — see `research/factors.py`. Per-day cleaning: 5×MAD winsorize → z-score → OLS-neutralize on industry (Fama-French 12 from SEC SIC) + log market cap → z-score.
- Combination: LASSO (rolling 252-day training, weekly retrain, time-series CV for the penalty). Signal at close t, trade at open t+1.
- Portfolio: 20 long / 20 short, 70% gross each side, weekly rebalance, 5-day score smoothing, buffer (a held name stays until it leaves the top/bottom 60), SPY overlay to neutralise 60-day beta, vol targeting on new positions (10% annual). Shorts only in shortable, easy-to-borrow names.
- Current best backtest (v2, all 21 factors): +7.4% annual net, Sharpe 0.52, max drawdown −38%, 5.5% daily turnover, beta ≈ 0. A v3 variant (5-day residual target, inverse-vol weights, borrowable shorts ≥ $1B) is running; I will tell you which config wins — build so the config is a parameter file, default = v2.
- Options: phase 2 only, after the stock book runs cleanly (underlying must be optionable; buy calls for the top-1% scores / puts for the bottom-1% using part of that name's allocation; premium ≤ 2% of equity per trade; no naked short options).

**Architecture:**
- LOCAL (my PC, always on): `live/daily_scores.py` productionises the research code: incremental update of Alpaca daily bars (keys in `.env`, never print or commit them), factors, preprocessing, LASSO, smoothing, portfolio construction → `live/out/targets_YYYYMMDD.json` (target weights per symbol, SPY hedge weight, model coefficients, data timestamp, checksum). Runs via Windows Task Scheduler at 06:30 Asia/Singapore, Tuesday–Saturday (after the US close).
- Then the targets file must reach the cloud. Find the right Qoder Cloud Agents mechanism (Files API, template/identity resources, or a schedule's resources field) and propose it to me before building; fallback is a private GitHub repo.
- CLOUD (QCA Forward Mode): a scheduled **Coordinator** delegates to — **Scout** (sanity checks on the target list: stale data, earnings within 2 trading days, halts, news shocks via moomoo skills → may veto a name to HOLD, never trades), **Guard** (the only agent with order tools: diff targets vs. current positions, run the rule gate, place limit orders through the Moomoo MCP, reconcile fills), **Risk Auditor** (separate session; exposure, beta, daily P&L and drawdown report; can only say no), and an **Advisor** for a second opinion on rebalances. Collaboration enabled. Pin child-agent and skill versions. The Moomoo vault is attached only to agents that need it (not the Coordinator). Coordinator rule: "never let the Guard act on a target list without the Risk Auditor's approval".

**Accounts (rule R1, hard-coded):** MODE=test → only account `5322438`; MODE=competition → only account `5832812`. Never both. Any other account ID is rejected, logged and alerted. Only the paper-trading skill may place orders.

**Rule gate (deterministic code, never the LLM) — at least:**
- R1 account allowlist by MODE · R2 instrument eligibility (price, market cap, liquidity; shorts: shortable + easy-to-borrow) · R3 per-name weight ≤ 10% and ≤ competition 40% cap · R4 gross long ≤ 70% and gross short ≤ 70% (× vol multiplier) · R5 net beta after hedge within ±0.10 · R6 ≤ 45 positions and ≤ 60 executed orders per day (competition cap 200) · R7 regular-hours rebalancing only (09:35–15:45 ET), limit orders with a price band, no market orders in pre/after-hours · R8 daily loss −2% → no new entries that day · R9 drawdown −8% from peak → flatten and halt until I re-enable · R10 target file older than 2 trading days or checksum mismatch → no rebalance (HOLD) · R11 missing/stale quote → HOLD · R12 submitted ≠ filled: reconcile, handle partial fills, cancel stale orders · R13 calendar: no trading on 26 Nov (Thanksgiving); 27 Nov half day (close 13:00 ET = 02:00 SGT), no rebalance that day.
- Each rule has an ID, threshold, action and unit tests.

**Autonomy:** MODE=test — every paper order needs my explicit approval (symbol, side, size, rationale, rule checks; no approval → no order; approvals expire). MODE=competition — orders inside all limits run automatically; anything outside is blocked or sent to Slack for approval. Manual kill switch.

**Schedules (America/New_York):** 09:00 pre-market readiness (Scout + Auditor) → Slack · 09:35 rebalance on rebalance days, otherwise drift/risk check · every 30 min 10:00–15:30 intraday risk check · 16:10 end-of-day report → Slack + journal. Explicit start/end dates = the competition window; skip holidays.

**Logging:** every decision (including vetoes and rejected orders, with reasons) → one JSON line in `journal/decisions.jsonl`; fills → `journal/fills.jsonl`; include commissions and slippage.

**Evaluation (Handbook §8.2):** keep 3–4 fixed evaluation prompts and re-run/diff them after every prompt or skill change.

### What I want in the PRD
1. Goals and success metrics mapped to the rubric (risk-adjusted return/Sharpe 25%, Qoder & QCA usage 30%, strategy & risk 30%, reusability & docs 15%) and to the bonus items in `docs/submission-checklist.md`.
2. Architecture diagram (mermaid): local job → cloud agents → Moomoo paper account → journal/dashboard/Slack; which agent has which tools/vault; file ownership.
3. The full rule gate table, strategy invalidation conditions, and the kill-switch procedure.
4. Data contract for `targets_*.json`, `decisions.jsonl`, `fills.jsonl`.
5. Schedules table, Slack messages, dashboard spec (positions, P&L, equity curve, drawdown, exposure/beta, decision log, agent status, Qoder usage).
6. Acceptance tests that must pass in MODE=test on account 5322438 before switching to 5832812.
7. Risks, open questions, and a day-by-day plan: build/test 19–20 Nov, go live in competition mode by 20 Nov (US session), run 23–27 Nov, deck/video/package 28–29 Nov, submit 29 Nov.

Ask me clarifying questions in rounds (goal → rules → proof) before finalising. In this task do not place orders, do not create schedules, and do not change any cloud configuration.

---

## 中文对照

帮我为 Moomoo × Qoder AI 交易 Agent 挑战赛的参赛作品写 PRD 并搭好项目骨架：只做美股模拟交易；agent 跑在 Qoder Cloud Agents（Forward Mode）上，连接 Moomoo MCP 和 Moomoo skills；另外有一个本地打分任务。请推荐多 agent 设计，并给每个 agent 一个模板。

先读这些（以它们为准）：
- `notes/handbook-notes.md`：官方 handbook、条款、FAQ 和两份 kickoff deck 的摘要。以 handbook 为准，有冲突请指出。
- `docs/submission-checklist.md`：提交表单和我们要拿到的所有加分项（A–D 部分），设计要覆盖每一项。
- `research/` 和 `reports/`：已完成的研究，包括因子代码、面板与清洗、预处理 + LASSO + 回测代码，以及各版结果。

全部用英文写。创建 `docs/PRD.md`、`agents/`（每个 agent 一份说明 + 带时间戳版本记录的 `AGENTS.md`，按 Handbook §8.1）、`skills/`（自写可复用 skill）、`rules/`（确定性规则门 + 测试）、`live/`（生产打分任务）、`journal/`、`dashboard/`、`reports/`。

### 已定事项——没问我不准改

**策略（来自研究，已回测 2021-11 → 2026-11）：**
- 截面多因子多空，股票池为比赛允许的美股普通股（股价 ≥ $1、市值 ≥ 5 亿美元，再加 20 日成交额中位数 ≥ 500 万美元的流动性门槛）。ETF 不打分，SPY 只用来对冲 beta。
- 因子：从我的 A 股因子库移植的 16 个（PV/MD/DC 系列，DC 是近似版）+ 美股经典因子（12-1 动量、52 周新高、特质波动率、短期反转、Amihud），见 `research/factors.py`。每日清洗：5 倍 MAD 去极值 → z-score → 按行业（SEC SIC 映射的 FF12）和对数市值做 OLS 中性化 → 再 z-score。
- 合成：LASSO（滚动 252 天训练、每周重训、时序交叉验证选惩罚系数）。t 日收盘出信号，t+1 日开盘成交。
- 组合：多 20 只 / 空 20 只，每边 70% 仓位，每周调仓，5 日得分平滑，缓冲带（已持有的股票直到掉出前/后 60 名才卖），用 SPY 对冲 60 日 beta，新开仓做波动率目标（年化 10%）。只做空可融券、容易借到的股票。
- 当前最佳回测（v2，全部 21 个因子）：扣成本年化 +7.4%，夏普 0.52，最大回撤 −38%，每天换手 5.5%，beta 约 0。v3 版本（5 日残差目标、波动率倒数权重、只空市值 ≥ 10 亿且可借的股票）正在跑；我会告诉你哪个胜出。配置要做成参数文件，默认用 v2。
- 期权：第二阶段再做，等股票组合跑稳以后（标的必须有期权；得分前 1% 买 call、后 1% 买 put，用的是这只股票分配额度的一部分；每笔权利金 ≤ 总资产 2%；禁止裸卖期权）。

**架构：**
- 本地（我的电脑，一直开着）：`live/daily_scores.py` 把研究代码做成生产版：增量更新 Alpaca 日线（key 在 `.env`，绝不打印或提交）、计算因子、预处理、LASSO、平滑、构建组合 → 输出 `live/out/targets_YYYYMMDD.json`（每只股票的目标权重、SPY 对冲权重、模型系数、数据时间戳、校验和）。用 Windows 任务计划程序在新加坡时间周二到周六 06:30（美股收盘后）运行。
- 然后目标文件要送到云端。请找出合适的 Qoder Cloud Agents 机制（Files API、模板/identity 资源，或定时任务的 resources 字段），先给我方案再动手；备选方案是私有 GitHub 仓库。
- 云端（QCA Forward Mode）：一个定时运行的 **Coordinator** 把任务委派给——**Scout**（检查目标清单：数据是否过期、2 个交易日内是否有财报、是否停牌、是否有新闻冲击，用 moomoo skills；可以否决某只股票改为 HOLD，但从不下单）、**Guard**（唯一有下单工具的 agent：对比目标和当前持仓、跑规则门、通过 Moomoo MCP 下限价单、对账成交）、**Risk Auditor**（单独 session；负责敞口、beta、当日盈亏和回撤报告；只能说"不"）、以及一个 **Advisor**（调仓时给第二意见）。开启 Collaboration。锁定子 agent 和 skill 的版本。Moomoo vault 只挂给需要的 agent（不给 Coordinator）。Coordinator 规则："没有 Risk Auditor 批准，绝不让 Guard 执行目标清单"。

**账户（规则 R1，写死）：** MODE=test → 只允许账户 `5322438`；MODE=competition → 只允许账户 `5832812`。两者不同时开放。其他任何账户 ID 一律拒绝、记日志、发警报。只有模拟交易 skill 能下单。

**规则门（确定性代码，绝不靠 LLM）——至少包括：**
- R1 按 MODE 的账户白名单 · R2 标的资格（股价、市值、流动性；做空还要求可融券且容易借到）· R3 单只权重 ≤ 10%，并且 ≤ 比赛 40% 上限 · R4 多头总仓位 ≤ 70%、空头总仓位 ≤ 70%（× 波动率系数）· R5 对冲后净 beta 在 ±0.10 以内 · R6 持仓 ≤ 45 个、每天成交订单 ≤ 60 笔（比赛上限 200）· R7 只在常规交易时段调仓（美东 09:35–15:45），用带价格区间的限价单，盘前盘后不下市价单 · R8 当日亏损 −2% → 当天不再开新仓 · R9 从高点回撤 −8% → 全部平仓并停机，等我重新启用 · R10 目标文件超过 2 个交易日或校验和不对 → 不调仓（HOLD）· R11 报价缺失或过期 → HOLD · R12 提交 ≠ 成交：要对账，处理部分成交，撤掉过期挂单 · R13 日历：11-26 感恩节不交易；11-27 半天（美东 13:00 收盘 = 新加坡 02:00），当天不调仓。
- 每条规则要有编号、阈值、动作和单元测试。

**自主度：** MODE=test——每一笔模拟单都要我明确批准（显示代码、方向、数量、理由、规则检查；没批准就不下单；批准有过期时间）。MODE=competition——所有限制内的订单自动执行；超出的拦截或发 Slack 等我批准。要有手动 kill switch。

**定时任务（America/New_York 时区）：** 09:00 盘前就绪检查（Scout + Auditor）→ Slack · 09:35 调仓日调仓，其他日子做漂移和风控检查 · 10:00–15:30 每 30 分钟盘中风控检查 · 16:10 收盘报告 → Slack + 日志。开始和结束日期设为比赛窗口，跳过休市日。

**日志：** 每个决策（包括否决和被拒订单，附原因）→ `journal/decisions.jsonl` 一行 JSON；成交 → `journal/fills.jsonl`；要包含手续费和滑点。

**评估（Handbook §8.2）：** 保留 3–4 个固定评估 prompt，每次改 prompt 或 skill 后重跑并对比输出。

### PRD 要包含
1. 目标和成功指标，对应评分标准（风险调整收益/夏普 25%、Qoder 与 QCA 使用 30%、策略与风控 30%、可复用与文档 15%）以及 `docs/submission-checklist.md` 里的加分项。
2. 架构图（mermaid）：本地任务 → 云端 agent → Moomoo 模拟账户 → 日志/dashboard/Slack；哪个 agent 拿哪些工具和 vault；文件归属。
3. 完整规则门表、策略失效条件、kill switch 流程。
4. `targets_*.json`、`decisions.jsonl`、`fills.jsonl` 的数据格式。
5. 定时任务表、Slack 消息、dashboard 规格（持仓、盈亏、净值曲线、回撤、敞口/beta、决策日志、agent 状态、Qoder 用量）。
6. 在 MODE=test、账户 5322438 上必须通过、之后才能切到 5832812 的验收测试。
7. 风险、未决问题，以及逐日计划：11-19 到 11-20 搭建和测试，11-20（美股时段）切到比赛模式上线，11-23 到 11-27 运行，11-28 到 11-29 做 deck、视频、打包，11-29 提交。

定稿前分几轮问我澄清问题（目标 → 规则 → 证明）。本任务不要下任何单、不要创建定时任务、不要修改任何云端配置。
