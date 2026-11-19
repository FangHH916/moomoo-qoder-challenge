# Prompt 05 — 把打分脚本和规则门更新成定稿方案（用户 11-19 选 A）

用法：Qoder Desktop 打开 D:\Python\moomoo-qoder-challenge，新开对话，只粘贴 EN 部分。
它改完后告诉 Claude，Claude 检查代码和测试。

---

## EN（粘贴这段）

The research is finished and the strategy is now frozen. Update the production code to match it exactly. Read `reports/lab/RESEARCH_SUMMARY.md`, `reports/lab/FINAL.json`, `research/lab_fix.py`, `research/lab_run.py` (functions `fit_scores`, `make_target_fn_short`, `tradable_mask`) and `research/pipeline_v2.py` (`backtest`) first — they are the reference implementation.

### Frozen strategy ("short alpha + SPY hedge", half size)
1. Factors: the 56 factors cached by `research/lab_cache.py` — `research/factors.py::compute_all` (21), `research/pipeline_v3.py::extra_factors` (resid_mom, max21) and `research/lab_factors.py::library` (33). Import these functions from `research/` instead of re-implementing them, so live and backtest definitions can never drift. Make the factor list a config value (`factor_set`) so it can be extended later.
2. Cleaning: `research/pipeline.py::preprocess` (5×MAD winsorise → z-score → industry + log-mcap neutralisation → z-score) on the eligible universe (price ≥ $1, market cap ≥ $500M, 20-day median dollar volume ≥ $5M).
3. Model: LassoCV (TimeSeriesSplit 5, 30 alphas, max_iter 10000), retrained every 5 trading days on the trailing 252 days, target = 5-day forward open-to-open return residualised on industry dummies + log market cap + 60-day SPY beta and ranked to [-0.5, 0.5] (`research/pipeline_v3.py::residual_target`), trained ONLY on the tradable universe = eligible AND shortable AND easy-to-borrow AND market cap ≥ $1B. The 5-day target at day s is usable for training only from s+6.
4. **Carry-forward**: if a retrain returns all-zero coefficients, keep the last non-zero coefficient vector (persist the history in `live/state/`). If no non-zero fit exists yet → HOLD (no new positions). Replace the current behaviour that raises `Hold` on every all-zero fit.
5. **Signal guard**: if today's scores have fewer than 50 distinct values or ~zero standard deviation → HOLD (keep current positions, never pick names by index/alphabetical order).
6. Book: short only. Smooth scores with a 5-day mean. Short the 20 lowest-scored tradable names; inverse 60-day-volatility weights, each name capped at 3× the equal weight; buffer: a held short stays while its rank is within the bottom 60. **Gross short = 35% of equity** (half of the researched 70%). Long side = SPY only, sized to offset the shorts' 60-day beta (beta clipped to [-1, 3]). No single-stock longs. Rebalance every 5 trading days; vol targeting scales new positions toward 10% annual volatility (factor in [0.3, 1.0]).
7. Margin cap: 0.5 × long gross + 0.6 × short gross ≤ 90% of equity at all times; scale the whole book down if it would be exceeded.
7b. **Competition per-ticker cap applies to ETFs too**: no single stock or ETF may exceed 40% of initial account assets ($40,000). Cap SPY at 38% of initial assets; put any remaining beta hedge in QQQ (sized by QQQ's 60-day beta to SPY), also capped at 38%; if both caps bind, scale the whole book down. Total positions (shorts + hedges + any discretionary position) must stay ≤ 100.
8. Costs used for reporting: 5 bp slippage per side, $0.99 per order, 6% p.a. short borrow.

### Required changes
- `live/daily_scores.py` + its config: implement items 1–8; the targets JSON must contain the short list with weights, the SPY weight, the coefficient vector actually used (and whether it was carried forward), signal-guard status, data timestamp and checksum.
- `rules/gate.py` + tests: add/adjust rules — gross short ≤ 35%, no single-stock longs (only SPY/QQQ hedges may be long), any single ticker ≤ 38% of initial assets (item 7b), total positions ≤ 100, margin-cap rule (item 7), signal-guard HOLD, carry-forward provenance check (refuse a target file whose coefficients are all zero).
- `docs/PRD.md`, `agents/*.md`, `AGENTS.md` (timestamped entry): describe the frozen strategy and state honestly that its out-of-sample (holdout) Sharpe was −0.30 with deflated Sharpe 0.435, so it is deployed at half size mainly to demonstrate the full agent + risk pipeline.
- Add a replay test that runs the new scoring on the last 30 cached trading days and checks: no all-zero-coefficient days are traded, never more than 20 shorts, gross short ≤ 35%, margin ≤ 90%, SPY hedge within ±0.10 of the target beta.

Keep MODE=test (account 5322438) as the default. Do not place orders, do not create schedules, and do not change cloud configuration in this task. Show me the diff summary and the test results when done.

---

## 中文对照

研究已经结束，策略已经冻结。请把生产代码改成和它完全一致。先读 `reports/lab/RESEARCH_SUMMARY.md`、`reports/lab/FINAL.json`、`research/lab_fix.py`、`research/lab_run.py`（`fit_scores`、`make_target_fn_short`、`tradable_mask` 函数）和 `research/pipeline_v2.py`（`backtest`），它们是参考实现。

### 冻结的策略（"空头 alpha + SPY 对冲"，半仓）
1. 因子：`research/lab_cache.py` 缓存的 56 个因子——`research/factors.py::compute_all`（21 个）、`research/pipeline_v3.py::extra_factors`（resid_mom、max21）和 `research/lab_factors.py::library`（33 个）。直接从 `research/` 导入这些函数，不要重写，这样实盘和回测的定义永远不会不一致。把因子列表做成配置项（`factor_set`），方便以后扩展。
2. 清洗：用 `research/pipeline.py::preprocess`（5 倍 MAD 去极值 → z-score → 行业 + 对数市值中性化 → z-score），在合格股票池上做（股价 ≥ $1、市值 ≥ 5 亿美元、20 日成交额中位数 ≥ 500 万美元）。
3. 模型：LassoCV（时序交叉验证 5 折、30 个 alpha、max_iter 10000），每 5 个交易日用过去 252 天重训一次；目标 = 未来 5 天开盘到开盘收益，对行业哑变量 + 对数市值 + 60 日 SPY beta 做残差后排名到 [-0.5, 0.5]（`research/pipeline_v3.py::residual_target`）；**只在可交易池上训练** = 合格 且 可融券 且 容易借到 且 市值 ≥ 10 亿美元。第 s 天的 5 日目标从 s+6 天起才能用于训练。
4. **沿用旧系数**：如果某次重训得到的系数全为 0，就沿用上一次非零的系数向量（历史存在 `live/state/`）。如果还从来没有过非零系数 → HOLD（不开新仓）。取代现在"系数全零就抛出 Hold"的做法。
5. **信号保护**：如果当天得分的不同取值少于 50 个，或者标准差约等于 0 → HOLD（维持现有持仓，绝不按代码或字母顺序选股）。
6. 组合：只做空。得分取 5 日平均。做空得分最低的 20 只可交易股票；按 60 日波动率倒数分配权重，单只不超过等权的 3 倍；缓冲带：已持有的空头只要排名仍在最后 60 名内就保留。**空头总仓位 = 权益的 35%**（研究时 70% 的一半）。多头只用 SPY，金额按空头的 60 日 beta 配平（beta 限制在 [-1, 3]）。不做个股多头。每 5 个交易日调仓；新开仓按年化 10% 的目标波动率缩放（系数在 [0.3, 1.0]）。
7. 保证金上限：任何时候 0.5 × 多头总额 + 0.6 × 空头总额 ≤ 权益的 90%；超出就把整个组合按比例缩小。
7b. **比赛的单只上限对 ETF 同样适用**：任何一只股票或 ETF 都不能超过初始资金的 40%（4 万美元）。SPY 最多占初始资金的 38%；剩下的 beta 对冲用 QQQ（按 QQQ 对 SPY 的 60 日 beta 折算），QQQ 同样不超过 38%；两个上限都触到的话，就把整个组合按比例缩小。持仓总数（空头 + 对冲 + 人工判断仓位）不能超过 100 个。
8. 报告用的成本：每边 5 bp 滑点、每单 $0.99、融券年利率 6%。

### 需要改的
- `live/daily_scores.py` 和它的配置：实现第 1–8 条；目标持仓 JSON 要包含空头清单和权重、SPY 权重、实际使用的系数向量（以及是否沿用了旧系数）、信号保护状态、数据时间戳和校验和。
- `rules/gate.py` 和测试：增加或调整规则——空头总仓位 ≤ 35%、不做个股多头（只有 SPY/QQQ 对冲可以做多）、任何单只标的 ≤ 初始资金 38%（第 7b 条）、持仓总数 ≤ 100、保证金上限规则（第 7 条）、信号保护 HOLD、沿用系数的来源检查（拒绝系数全为 0 的目标文件）。
- `docs/PRD.md`、`agents/*.md`、`AGENTS.md`（加一条带时间戳的记录）：描述冻结后的策略，并如实写明它的样本外（留出期）夏普是 −0.30、Deflated Sharpe 0.435，所以只用半仓部署，主要目的是展示完整的 agent 和风控流程。
- 加一个回放测试：用新的打分逻辑跑最近 30 个缓存交易日，检查：系数全零的日子没有交易、空头从不超过 20 只、空头总仓位 ≤ 35%、保证金 ≤ 90%、SPY 对冲与目标 beta 的偏差在 ±0.10 以内。

默认保持 MODE=test（账户 5322438）。本任务不要下单、不要建定时任务、不要改云端配置。做完给我看改动摘要和测试结果。
