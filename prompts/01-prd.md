# Prompt 01 — 生成 PRD 和项目结构

用法：在 Qoder（工作区打开 D:\Python\moomoo-qoder-challenge）新开一个对话，把下面 EN 部分整段粘贴进去。中文对照只给你看，不用贴。

---

## EN（粘贴这段）

Help me create a PRD for the Moomoo × Qoder AI Trading Agent Challenge. Use US-only paper trading with agents running in Qoder Cloud Agents (Forward Mode), connected to the Moomoo MCP and Moomoo skills. Recommend a multi-agent architecture and the templates for each agent.

First read `notes/handbook-notes.md` in this workspace — it summarises the official handbook, competition T&C, FAQ and both kickoff decks. The handbook governs; if anything below conflicts with it, flag it.

Write everything in English. Save the PRD as `docs/PRD.md` and create this project structure (empty placeholders are fine for now):

```
agents/        # one AGENTS-style spec per agent + AGENTS.md version log
skills/        # our own reusable skills (one folder per skill, SKILL.md inside)
rules/         # deterministic risk gate code + tests
journal/       # decision log (JSONL) and fills
dashboard/     # local web dashboard (later)
reports/       # daily reports and final deck material
docs/          # PRD and design notes
```

### Fixed decisions (do not change without asking me)

**Accounts (hard-coded allowlist, rule R1)**
- MODE=test → the only tradable account is `5322438` (US paper margin).
- MODE=competition → the only tradable account is `5832812` (the challenge account).
- Never both at once. Any other account ID (5322433, 5322434, 5322435, 5322436, 5322439, 5322440 or any live account) must be rejected, logged and alerted.
- Only the paper-trading skill may be used for orders; do not use any live-trading skill.

**Strategy scope**
- Short-term, mainly intraday, long AND short US stocks and ETFs (any instrument allowed by competition rules: price ≥ $1, market cap ≥ $500M). Add a liquidity filter for intraday trading.
- Short selling allowed; no margin financing for leverage.
- Market-regime filter (e.g. QQQ/SPY vs 20-day EMA + intraday trend) decides long-only / short-only / reduced-size.
- Baseline setups: opening-range breakout and intraday VWAP mean reversion. Cross-asset ETFs (TLT, GLD, sector ETFs, etc.) may be used for rotation/hedging.
- Options are auxiliary only (hedging and expressing strong signals). Underlyings allowed: AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA, SPX, XSP, SPY, QQQ, IWM. Buying and defined-risk selling structures (spreads, iron condors) are allowed; **naked short options are forbidden**. Close before expiry-day close. Multi-leg orders are placed leg by leg, so handle partial / one-leg fills.

**Risk limits (deterministic, enforced in code, never by the LLM)**
- Risk per trade 0.5–1% of equity; single-symbol position ≤ 35% (competition cap is 40%); max 5 open positions; total short exposure ≤ 50%; total options risk ≤ 5%.
- Daily loss −2% → no new entries for the day. 3 consecutive losses → halve size until 2 consecutive winners. Peak-to-trough drawdown −8% → flatten everything and halt until I re-enable.
- Pre-market/after-hours: limit orders only. Respect order limits (≤ 200 executed orders/day, ≤ 100 positions).
- Calendar: no trading on 26 Nov (Thanksgiving); 27 Nov is a half day (regular close 13:00 ET = 02:00 SGT) — flatten intraday positions by 01:45 SGT; the competition trading window ends then. Reduce size around scheduled macro events (e.g. PCE on 25 Nov).
- Missing or stale data → HOLD. Submitted ≠ filled: always reconcile.

**Agents (start with 2, then scale)**
- Scout: research (news, sentiment, unusual activity, technicals) → dated-evidence BUY/SELL/HOLD proposals. Never places orders.
- Guard: owns accounts/positions/orders, runs the deterministic rule gate R1–Rn, is the only execution path. Never originates trades.
- Later: Risk Auditor in a separate session (only says no + daily drawdown report), a scheduled Coordinator, optional Advisor.
- Coordinator rule: never let execution act on research output without passing the gate.

**Autonomy**
- MODE=test: every paper order requires my explicit approval (show symbol, side, size, rationale, risk checks; no approval → no order).
- MODE=competition: orders inside all limits execute automatically; anything outside limits is blocked or sent to Slack for my approval.

**Automation & reporting**
- Forward Mode schedules with explicit IANA time zones (America/New_York for market logic). Pre-market scan, intraday cycle (~15 min), end-of-day report.
- Slack channel delivery for scans, risk alerts and daily summaries (Pairing mode).
- Every decision (including rejected ones and why) is appended as one JSON line to `journal/decisions.jsonl`.
- Local dashboard later: positions, P&L, equity curve, open orders, drawdown, Sharpe, decision log, agent status.

### What I want in the PRD
1. Goal, scope, success metrics mapped to the judging rubric (risk-adjusted return/Sharpe 25%, Qoder & QCA usage 30%, strategy & risk design 30%, reusability & documentation 15%).
2. Architecture diagram (text/mermaid) and responsibilities per agent, including which agent gets which tools/vault and file ownership.
3. The full rule gate as a numbered table R1–Rn (condition, threshold, action, source rule), plus strategy invalidation conditions.
4. Trading logic for each setup: entry, exit, stop, sizing formula, session handling.
5. Schedules (name, cron/interval, time zone, task, output).
6. Data/logging schema for the decision journal and fills.
7. Acceptance tests that must pass in MODE=test before switching to competition.
8. Risks, open questions, and a day-by-day plan from 18 Nov to submission on 30 Nov 23:59 SGT.

Ask me clarifying questions in rounds (goal → rules → proof) before finalising. Do not place any orders, do not create schedules, and do not modify any cloud configuration in this task.

---

## 中文对照

帮我为 Moomoo × Qoder AI Trading Agent Challenge 写一份 PRD。只做美股模拟交易，agent 运行在 Qoder Cloud Agents（Forward Mode），连接 Moomoo MCP 和 Moomoo skills。推荐一个多 agent 架构，以及每个 agent 的模板。

先读工作区里的 `notes/handbook-notes.md`，它总结了官方 handbook、比赛条款、FAQ 和两份 kickoff deck。以 handbook 为准；下面内容如果和它冲突，请指出来。

全部用英文写。PRD 保存为 `docs/PRD.md`，并建立以下项目结构（目前空文件夹即可）：agents（每个 agent 一份说明 + AGENTS.md 版本记录）、skills（自写可复用 skill）、rules（确定性风控门代码 + 测试）、journal（决策日志 JSONL 和成交）、dashboard（本地网页面板，后做）、reports（日报和最终 deck 素材）、docs（PRD 和设计文档）。

### 已定的决定（没问我不准改）

**账户（写死白名单，规则 R1）**
- MODE=test → 唯一可交易账户 5322438（美股模拟融资账户）。
- MODE=competition → 唯一可交易账户 5832812（比赛账户）。
- 两者不同时开放。其他任何账户 ID（5322433、5322434、5322435、5322436、5322439、5322440 或任何实盘账户）一律拒绝、记日志、发警报。
- 下单只能用模拟交易 skill，不得使用任何实盘交易 skill。

**策略范围**
- 短线、以日内为主，多空交易美股股票和 ETF（比赛规则允许的都可以：股价 ≥ $1、市值 ≥ 5 亿美元）。日内交易加流动性过滤。
- 允许融券做空；不用融资加杠杆。
- 市场状态过滤（如 QQQ/SPY 相对 20 日 EMA + 日内趋势）决定只做多 / 只做空 / 减仓。
- 基础策略：开盘区间突破（ORB）和日内 VWAP 均值回归。可用跨资产 ETF（TLT、GLD、行业 ETF 等）做轮动或对冲。
- 期权只做辅助（对冲、表达强信号）。标的限：AAPL、MSFT、NVDA、AMZN、GOOGL、META、TSLA、SPX、XSP、SPY、QQQ、IWM。允许买入和有限风险的卖出结构（价差、铁鹰）；**禁止裸卖期权**。到期日收盘前平仓。多腿要逐腿下单，所以要处理部分成交 / 只成交一条腿。

**风控限制（确定性，用代码执行，绝不靠 LLM）**
- 单笔风险为权益的 0.5–1%；单只仓位 ≤ 35%（比赛上限 40%）；最多 5 个持仓；做空总敞口 ≤ 50%；期权总风险 ≤ 5%。
- 单日亏损 −2% → 当天停止开新仓。连亏 3 笔 → 仓位减半，直到连赢 2 笔。从高点回撤 −8% → 全部平仓并停机，等我重新启用。
- 盘前/盘后只用限价单。遵守订单限制（每天成交 ≤ 200 笔，持仓 ≤ 100 个）。
- 日历：11-26（感恩节）不交易；11-27 半天（常规盘 13:00 ET = 新加坡 02:00 收），新加坡时间 01:45 前平掉日内仓位，比赛交易窗口到此结束。重要宏观事件前后（如 11-25 的 PCE）减仓。
- 数据缺失或过期 → HOLD。提交 ≠ 成交，必须对账。

**Agent（先 2 个，再扩展）**
- Scout：研究（新闻、情绪、异动、技术面）→ 带日期证据的 BUY/SELL/HOLD 提议。从不下单。
- Guard：管理账户/持仓/订单，执行确定性规则门 R1–Rn，是唯一执行通道。从不发起交易。
- 之后加：单独 session 的 Risk Auditor（只负责说不 + 每日回撤报告）、定时运行的 Coordinator、可选 Advisor。
- Coordinator 规则：研究结果不过规则门，绝不允许执行。

**自主度**
- MODE=test：每一笔模拟单都要我明确批准（展示代码、方向、数量、理由、风控检查；没批准就不下单）。
- MODE=competition：所有限制内的订单自动执行；超出限制的拦截或发 Slack 等我批准。

**自动化与报告**
- Forward Mode 定时任务，写明 IANA 时区（市场逻辑用 America/New_York）。盘前扫描、盘中循环（约 15 分钟）、收盘报告。
- Slack 推送扫描、风控警报和每日总结（Pairing 模式）。
- 每个决策（包括被拒的和原因）以一行 JSON 追加到 `journal/decisions.jsonl`。
- 之后做本地 dashboard：持仓、盈亏、净值曲线、挂单、回撤、Sharpe、决策日志、agent 状态。

### PRD 要包含
1. 目标、范围、成功指标，对应评分标准（风险调整收益/Sharpe 25%、Qoder 与 QCA 使用 30%、策略与风控 30%、可复用与文档 15%）。
2. 架构图（文字/mermaid）和每个 agent 的职责，包括哪个 agent 拿哪些工具/vault、文件归属。
3. 完整规则门编号表 R1–Rn（条件、阈值、动作、对应规则来源），以及策略失效条件。
4. 每个策略的交易逻辑：入场、出场、止损、仓位公式、时段处理。
5. 定时任务（名称、cron/间隔、时区、任务、输出）。
6. 决策日志和成交的数据结构。
7. 从 test 切到 competition 之前必须通过的验收测试。
8. 风险、未决问题，以及 11-18 到 11-30 23:59 SGT 提交的逐日计划。

定稿前分几轮问我澄清问题（目标 → 规则 → 证明）。本任务不要下任何单、不要创建定时任务、不要修改任何云端配置。
