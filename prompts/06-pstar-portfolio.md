# Prompt 06 — 部署方案换成第十轮组合 P*（用户 11-19 确认"按推荐的来"）

用法：等 Qoder 把 prompt 05 做完（不要中途打断），然后在 Qoder Desktop 打开 D:\Python\moomoo-qoder-challenge，
新开对话，只粘贴 EN 部分。它改完后告诉 Claude，Claude 检查代码、跑测试。

---

## EN（粘贴这段）

The deployment strategy changes. Plan A from prompt 05 (LASSO short book + SPY) had a holdout Sharpe of −0.30 and is retired: keep its code and tests, but make it non-default (`strategy: legacy-a`, execution disabled). The new default is the round-10 portfolio **P\***. The research is finished and frozen; this task is production wiring only.

Read first: `reports/lab/RESEARCH_SUMMARY.md` (round 10), `reports/lab/R10_DEPLOY.json`, `reports/lab/R10_REALISTIC.json`, `research/ROUND10_PREREG.md` and `research/r10_deploy.py`, which is the single source of truth (`run_pstar`, `ticker_weights`, `apply_caps`, `simulate`, `cmd_live`). Do not re-implement the strategy anywhere; call or import this file, so live and backtest can never drift apart.

### Frozen strategy P* (change no number)
1. **Sleeve T9, ETF trend:** SPY QQQ IWM TLT IEF GLD SLV USO UUP. Long when the 252-day return is positive, flat otherwise. Weight = (0.10 / 60-day annualised vol) / 9, capped at 38%.
2. **Sleeve D77, no-estimation short book:** equal-weight composite of 11 literature factors (bm ep sp mom12_1 ivol60 roe gpa ag accr nsi ear), each cleaned and neutralised on industry + log market cap. Short the 77 lowest-scored shortable, easy-to-borrow names with market cap ≥ $1B: inverse-vol weights capped at 3× equal weight, 3× rank buffer, 5-day score smoothing, vol-targeted entries, SPY beta hedge.
3. **Combination:** long-only Ledoit-Wolf minimum variance on the two sleeves' trailing 252 daily returns (2-session lag), times the frozen scale K = 1.4634.
4. **Rebalance** every 5 sessions counted from 2021-11-18. The next decision is after the close of 2026-11-19 (trade at the 11-20 open); the one after that is after the 11-27 close, i.e. after the competition. Between rebalances, positions drift and only the risk rules in item 6 may trade.
5. **Competition caps** (`apply_caps`): any ticker ≤ 38% of initial assets ($38,000). SPY above that moves to DIA, then QQQ, sized by their 60-day beta to SPY. Margin 0.5 × long + 0.6 × short, including discretionary positions, ≤ 90% of equity; above that, the strategy book is scaled down. Positions ≤ 100.
6. **Execution rules** (fixed in advance; see `R10_REALISTIC.json`):
   - Whole shares at the next open, rounded to the nearest share. A name that rounds to 0 shares is skipped, and its weight is not redistributed.
   - On rebalance sessions, an existing position is re-sized only if it is off target by more than 0.25% of equity. Entries and exits always trade.
   - Between rebalances, only two risk rules trade: a ticker above 39% of initial assets is cut back to 38%, and margin above 95% is cut back to 90%.
   - Total drawdown of −8% from the peak → flatten everything and halt (unchanged). Keep the existing final-exit schedule for 11-27.
7. **Discretionary positions** (for example the Anthropic IPO trade from prompts 03/04) are listed explicitly in config (`discretionary_symbols`). The strategy never trades them, but they count toward margin, the per-ticker cap and the position count. Every other position in the account belongs to the strategy.

The composition at the 2026-11-17 close ($100k, from `R10_DEPLOY.json`) was UUP 28.9%, SPY 22.1%, IWM 7.7%, QQQ 5.9%, USO 3.2%, plus 77 shorts totalling 6.1%. That is a margin of 37.6% and 82 positions. Most of the risk sits in the ETF trend sleeve, and the short book is small (about $80 per name). This is expected: the minimum-variance weight on D77 is 11% today.

### How live gets its targets
Run `python r10_deploy.py live --equity <E> --hold SYMBOL=DOLLARS ...` from `research/` with the project venv. It takes about 10–15 minutes and does the following:
- refreshes Alpaca bars (adjusted and raw, completed sessions only; a symbol's full history is re-downloaded when its adjustment basis changes);
- refreshes 8-K earnings dates from the SEC;
- rebuilds a lean cache in `data/cache_live`;
- recomputes the whole P* backtest through the latest completed session;
- writes `reports/live/r10_targets_<session>.json`, which contains the targets (weight, dollars, sleeve), `diagnostics`, `checksum_sha256`, data provenance and a consistency check against the research run.

If the command fails, or the newest JSON is not for the latest completed session → HOLD: place no new strategy orders, but keep the risk rules active.

### Required changes
- **`live/`:** add a P* entry point (for example `live/pstar.py`, or `--strategy pstar` in `daily_scores.py`). It must:
  - run the command above with the account's current equity and its discretionary holdings from the moomoo snapshot;
  - validate the JSON: schema, checksum, session = latest completed session, `diagnostics.ok`, positions ≤ 100, consistency diffs < 1e-6;
  - convert the targets into the existing targets contract (bump the schema version) for the gate and the order planner.
- **Order planner:** implement item 6 against actual broker holdings (shares), using limit orders as now.
  - The go-live day is one ENTRY batch of about 87 orders.
  - Normal trading may use at most 100 executed orders per day. Keep the other 100 of the competition's 200 in reserve for an emergency flatten.
- **Pre-trade eligibility check:** before the first order on each account, confirm that each of the 10 ETFs (SPY QQQ IWM TLT IEF GLD SLV USO UUP DIA) is tradable and competition-eligible (price ≥ $1, market value ≥ $500M). UUP is the one most at risk. If an ETF fails, skip it, log the deviation and report it in Slack. A short that moomoo cannot borrow is skipped the same way, not replaced.
- **`rules/gate.py` + tests**, rules for P*:
  - long positions only in the 10 ETFs and the whitelisted discretionary symbols;
  - shorts only in stock symbols present in the current targets;
  - any ticker, including the order, ≤ 38% of initial assets ($38,000); SPY overflow only via DIA/QQQ;
  - margin including discretionary positions ≤ 90%;
  - positions ≤ 100;
  - daily executed orders ≤ 200, with the reserve kept;
  - targets freshness;
  - −8% drawdown halt;
  - the plan-A rules apply only when strategy = legacy-a.
- **Risk monitor:** run after the open fills and before every order batch. It checks the 39% trim, margin above 95% and drawdown.
- **Replay test:** feed `reports/lab/R10_reference_targets.csv` (30 sessions; `weight` = before caps, `weight_capped_100k` = after caps at $100k equity) through your conversion and gate. Check that:
  - your capped weights equal `weight_capped_100k` within 1e-9 (calling `apply_caps` is fine);
  - every ticker ≤ 38% of initial assets;
  - margin ≤ 90%;
  - positions ≤ 100;
  - there are no long single stocks;
  - shorts ≤ 77.
- **Config:** add `live/config.pstar.json` with the frozen constants (copy them from `R10_DEPLOY.json`), plus `strategy: pstar`, `mode: test`, `execution_enabled: false` and `discretionary_symbols: []`.
- **Docs:** update `docs/PRD.md`, `AGENTS.md` (timestamped entry), `agents/*.md` and `README.md` to describe P*, with honest numbers:

  | Five-year Sharpe | Full | Selection | Holdout |
  |---|---|---|---|
  | Ideal backtest | 1.04 | 0.98 | 1.29 |
  | Realistic $100k execution | 1.05 | 0.93 | 1.55 |

  - Max drawdown: −5.6% ideal, −6.8% realistic. Every calendar year is positive.
  - Deflated Sharpe: 0.78 over the 32 round-10 combinations, 0.65 over all trials.
  - Expected return is about 6% a year, so over the six remaining competition sessions the result is mostly noise. The point of P* is a robust, risk-controlled process.
  - Plan A was retired because its holdout Sharpe was −0.30.

Keep MODE=test (account 5322438) as the default. Do not place orders, do not create schedules, and do not change cloud configuration in this task. Show me the diff summary and the test results when done.

---

## 中文对照

部署策略要换。prompt 05 里的方案 A（LASSO 空头组合 + SPY）留出期夏普是 −0.30，现在停用：代码和测试保留，但不再作为默认（`strategy: legacy-a`，禁止执行）。新的默认策略是第十轮的组合 **P\***。研究已经结束并冻结，这次只做生产环境的接入。

先读：`reports/lab/RESEARCH_SUMMARY.md`（第十轮）、`reports/lab/R10_DEPLOY.json`、`reports/lab/R10_REALISTIC.json`、`research/ROUND10_PREREG.md` 和 `research/r10_deploy.py`。最后这个文件是唯一的标准实现（`run_pstar`、`ticker_weights`、`apply_caps`、`simulate`、`cmd_live`）。任何地方都不要重写策略，直接调用或导入这个文件，这样实盘和回测永远不会不一致。

### 冻结的策略 P*（任何数字都不要改）
1. **T9 子策略，ETF 趋势：** SPY QQQ IWM TLT IEF GLD SLV USO UUP。过去 252 天收益为正就做多，否则空仓。权重 = (0.10 / 60 日年化波动率) / 9，单只上限 38%。
2. **D77 子策略，不做估计的空头组合：** 11 个文献因子（bm ep sp mom12_1 ivol60 roe gpa ag accr nsi ear）等权合成，每个因子都先清洗，再对行业 + 对数市值做中性化。做空得分最低的 77 只可融券、容易借到、市值 ≥ 10 亿美元的股票：按波动率倒数分配权重，单只不超过等权的 3 倍；排名缓冲带 3 倍；得分取 5 日平均；新开仓按目标波动率缩放；用 SPY 对冲 beta。
3. **组合方式：** 用两个子策略过去 252 天的日收益（滞后 2 个交易日）做 Ledoit-Wolf 只做多的最小方差配权，再乘以冻结的缩放系数 K = 1.4634。
4. **调仓：** 从 2021-11-18 起每 5 个交易日调一次。下一次是 2026-11-19 收盘后决策（11-20 开盘成交）；再下一次是 11-27 收盘后，也就是比赛结束以后。两次调仓之间仓位自然漂移，只有第 6 条里的风控规则可以交易。
5. **比赛上限**（`apply_caps`）：任何一只标的 ≤ 初始资金的 38%（38,000 美元）。SPY 超出部分依次转给 DIA、QQQ，按它们对 SPY 的 60 日 beta 折算。保证金 0.5 × 多头 + 0.6 × 空头（包括人工判断的仓位）≤ 权益的 90%；超出就把策略仓位整体按比例缩小。持仓数 ≤ 100。
6. **执行规则**（事先固定，见 `R10_REALISTIC.json`）：
   - 下一个开盘按整股成交，四舍五入到整股。四舍五入后为 0 股的股票直接跳过，它的权重也不分给别的股票。
   - 调仓日只有当已有仓位偏离目标超过权益的 0.25% 才调整。新开仓和清仓总是执行。
   - 两次调仓之间只有两条风控规则会交易：单只超过初始资金 39% 就减回 38%；保证金超过 95% 就降回 90%。
   - 从高点回撤 −8% → 全部平仓并停机（不变）。11-27 现有的收尾平仓安排保持不变。
7. **人工判断的仓位**（比如 prompt 03/04 的 Anthropic 新股交易）要在配置里明确列出（`discretionary_symbols`）。策略永远不交易它们，但它们要计入保证金、单只上限和持仓数。账户里其他所有仓位都属于策略。

2026-11-17 收盘时（10 万美元，数据来自 `R10_DEPLOY.json`）的持仓：UUP 28.9%、SPY 22.1%、IWM 7.7%、QQQ 5.9%、USO 3.2%，另有 77 只空头合计 6.1%。保证金占用 37.6%，持仓 82 个。大部分风险在 ETF 趋势子策略上，空头组合很小（每只约 80 美元）。这是正常的：现在最小方差给 D77 的权重是 11%。

### 实盘怎样拿到目标持仓
在 `research/` 下用项目的虚拟环境运行 `python r10_deploy.py live --equity <E> --hold 代码=金额 ...`，大约 10–15 分钟，依次做这些事：
- 刷新 Alpaca 行情（复权和不复权两套，只取已经收盘的交易日；某只股票的复权基准变了，就重新下载它的全部历史）；
- 从 SEC 刷新 8-K 财报日期；
- 在 `data/cache_live` 重建精简缓存；
- 把 P* 的完整回测重算到最近一个已收盘的交易日；
- 写出 `reports/live/r10_targets_<交易日>.json`，内容包括目标持仓（权重、金额、所属子策略）、`diagnostics`、`checksum_sha256`、数据来源，以及和研究结果的一致性检查。

如果命令失败，或者最新的 JSON 不是最近一个已收盘交易日的 → HOLD：策略不下新单，但风控规则照常运行。

### 需要改的
- **`live/`：** 加一个 P* 的入口（比如 `live/pstar.py`，或者在 `daily_scores.py` 里加 `--strategy pstar`）。它要做到：
  - 用账户当前权益和 moomoo 快照里的人工判断仓位运行上面的命令；
  - 校验 JSON：格式、校验和、交易日 = 最近一个已收盘交易日、`diagnostics.ok`、持仓 ≤ 100、一致性差异 < 1e-6；
  - 把目标持仓转换成现有的目标合约格式（版本号加一），交给规则门和下单模块。
- **下单模块：** 按第 6 条、对照券商里的实际持股数执行，继续用限价单。
  - 上线当天是一批约 87 笔的 ENTRY 订单。
  - 日常交易每天最多用 100 笔成交订单，比赛上限 200 笔里剩下的 100 笔留给紧急平仓。
- **交易前资格检查：** 每个账户第一次下单前，确认 10 只 ETF（SPY QQQ IWM TLT IEF GLD SLV USO UUP DIA）都能交易、符合比赛条件（股价 ≥ 1 美元、市值 ≥ 5 亿美元）。最可能不合格的是 UUP。不合格的就跳过，记录偏差，并在 Slack 里说明。moomoo 借不到券的空头也同样跳过，不找替补。
- **`rules/gate.py` 和测试**，P* 的规则：
  - 多头只能是这 10 只 ETF 和白名单里的人工判断标的；
  - 空头只能是当前目标里的股票；
  - 任何一只标的（含这笔订单）≤ 初始资金 38%（38,000 美元）；SPY 超出部分只能通过 DIA/QQQ 承接；
  - 含人工判断仓位的保证金 ≤ 90%；
  - 持仓数 ≤ 100；
  - 每天成交订单 ≤ 200，并保留紧急平仓的额度；
  - 目标持仓必须是最新的；
  - 回撤 −8% 停机；
  - 方案 A 的规则只在 strategy = legacy-a 时生效。
- **风控监控：** 开盘成交后、每批订单之前运行，检查 39% 减仓、保证金超过 95% 和回撤。
- **回放测试：** 把 `reports/lab/R10_reference_targets.csv`（30 个交易日；`weight` 是限额前的权重，`weight_capped_100k` 是按 10 万美元权益施加限额后的权重）送进你的转换和规则门，检查：
  - 你算出的限额后权重和 `weight_capped_100k` 的差异在 1e-9 以内（直接调用 `apply_caps` 也可以）；
  - 任何一只标的 ≤ 初始资金 38%；
  - 保证金 ≤ 90%；
  - 持仓数 ≤ 100；
  - 没有个股多头；
  - 空头 ≤ 77 只。
- **配置：** 新增 `live/config.pstar.json`，放入冻结的常数（从 `R10_DEPLOY.json` 复制），再加上 `strategy: pstar`、`mode: test`、`execution_enabled: false` 和 `discretionary_symbols: []`。
- **文档：** 更新 `docs/PRD.md`、`AGENTS.md`（加一条带时间戳的记录）、`agents/*.md` 和 `README.md`，描述 P*，数字如实写：

  | 5 年夏普 | 完整 | 选择期 | 留出期 |
  |---|---|---|---|
  | 理想回测 | 1.04 | 0.98 | 1.29 |
  | 10 万美元真实执行 | 1.05 | 0.93 | 1.55 |

  - 最大回撤：理想 −5.6%，真实 −6.8%。每个自然年都是正收益。
  - Deflated Sharpe：在第十轮 32 个组合里是 0.78，在所有试验里是 0.65。
  - 预期年化约 6%，所以比赛剩下的 6 个交易日里，结果主要是随机波动。P* 的意义在于一套稳健、风险可控的流程。
  - 方案 A 停用，因为它的留出期夏普是 −0.30。

默认保持 MODE=test（账户 5322438）。本任务不要下单、不要建定时任务、不要改云端配置。做完给我看改动摘要和测试结果。
