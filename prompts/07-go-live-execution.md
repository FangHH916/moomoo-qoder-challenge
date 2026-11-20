# Prompt 07 — 今晚上线：测试账户冒烟测试 → 比赛账户建仓（P*15）

用法：
- **第一部分（Part 1）**：prompt 06 审查通过后，贴进 Qoder 新对话。开盘前做完：写上线工具和测试，**不下单**。
- **第二部分（Part 2）**：先贴 `07b-arm-and-evidence.md` 并通过 Claude 审查；然后今晚 11-20 美股开盘（22:30 SGT）后，贴进**同一个对话**。
  - Stage A（测试账户 5322438，4 笔 1 股的单）要你回一次 `CONFIRM`。
  - Stage B（比赛账户 5832812，约 80 笔建仓单）要你先回 `GO`（切账户），看完汇总后再回 `CONFIRM`（提交）。
- 如果 Qoder 每调用一次 moomoo 下单工具都弹窗要你允许：Stage B 在你回 `CONFIRM` 之后，可以把 `sim_trade_input_order` 设成"本会话允许"，结束后改回"每次询问"。只读工具可以一直"本会话允许"。

---

## Part 1 — EN（06 审查通过后粘贴）

Goal: build the go-live tooling for tonight's first P* rebalance. In this part: no orders, no schedules, no cloud changes.

Context: prompt 06 wired P* (variant `pstar15`). Tonight, 2026-11-20, is go-live (US regular session opens 09:30 ET = 22:30 SGT):
- first a smoke test on test account **5322438**;
- then the ENTRY batch on competition account **5832812**.

The moomoo MCP is reachable only from this Qoder Desktop chat, so responsibilities are split:
- **deterministic Python** parses, sizes, gates and reconciles everything;
- **the chat agent** only carries exact payloads to the MCP tools and saves raw responses back to files.

Build `live/golive.py` with the subcommands below. Each reads and writes JSON under `reports/live/golive/<date>/<account>/` and appends every step to the journal through `journal/store.py`.

1. **`snapshot --account <id> --cash <file> --positions <file> [--orders <file>]`**
   - Parse the raw responses of `sim_trade_cash_info`, `sim_trade_position_list` and `sim_trade_history_order_list` (saved verbatim by the chat agent) into a normalised snapshot: equity, cash, market value, long gross, short gross, margin (0.5 × long + 0.6 × short), and positions (symbol, signed quantity, average cost, market value).
   - Unknown fields are null, never zero.
   - Reject the snapshot if the account in the responses is not `--account`.
2. **`plan --account <id> --snapshot <file> --targets <file> --quotes <file> [--smoke]`**
   - Run the prompt-06 order planner and `rules/gate.py` against the snapshot and the targets, with bid/ask taken from raw MCP quote responses.
   - Write `plan.json`. Each ticket holds the exact `sim_trade_input_order` payload (built with `live/relay.py: paper_payload`) and its gate result.
   - Add a human summary: counts by side; long and short gross in $; margin after; largest ticker vs $38,000; positions after; skipped names with reasons (0 shares, ineligible ETF, not borrowable); estimated fees at $0.99 per order.
   - An order that fails the gate is left out of the submit list and logged as rejected, with its reasons.
   - `--smoke` (Stage A only, account 5322438 only):
     - opening tickets: BUY 1 share of IWM, and SELL 1 share of one executable short from the targets priced below $50;
     - closing tickets: the two orders that flatten them.
3. **`record --plan <file> --ticket <n> --response <file>`**
   - Log the raw response of one submission (order_id, status, error) against its ticket.
   - A ticket that already has an order_id is never planned or sent again.
4. **`reprice --plan <file> --orders <file> --quotes <file>`**
   - For tickets still unfilled after 5 minutes, produce cancel payloads plus new limit tickets for the remaining quantity at the new bid/ask, using the same price policy.
   - At most 3 rounds per ticket. After round 3, cancel and mark the ticket `skipped_unfilled`.
5. **`reconcile --plan <file> --snapshot <file> --orders <file>`**
   - Compare final holdings with the targets in shares.
   - Report fills: quantity, price, fee where available, and slippage vs the arrival quote in bps.
   - Also report: deviations above 0.25% of equity; margin; position count; executed orders today against the 100-order budget (the competition limit is 200; 100 are kept in reserve for an emergency flatten).
   - Write `reconcile.json` and `summary.md`, and append fill rows to `journal/fills.jsonl`.

**Price policy:** marketable limit orders.
- Buy at ask + 0.05%; sell at bid − 0.05%.
- Round to the cent away from the mid, and at least 1 cent through the touch.
- Regular session only (`order_trade_time_type = 1`).
- If the prompt-06 planner already has a limit-price policy, use it and say which one you used.

**Safety:**
- Submission is not idempotent (`text` is not an idempotency key). Before re-sending any ticket after an error or a timeout, the tooling must require a fresh order-history file, and must refuse to resend if a matching order already exists.
- **Config for Stage B:** `mode: competition` and `execution_enabled: true` apply only for the Stage B run; afterwards `execution_enabled` goes back to `false`. In each mode, R1 must reject every account except that mode's account.
- No other order types, no extended hours, no options.
- **Release contract:** the "Round 3 sequence" paragraph in `agents/AGENTS.md` still requires a full-session shadow run before any competition order. I have replaced that sequence: tonight's Stage A on 5322438 is the activation evidence for Stage B on 5832812. Stage A means one ETF buy, one short sale and both closes, with fills and fees reconciled. Stage B then needs my `GO` and `CONFIRM`. Rewrite that paragraph to say exactly this, dated today and marked "decided by the user". Leave the other unverified items as they are; the cloud items stay unverified until prompt 08.

**Tests (unittest)**, with fixtures built from `reports/live/r14_targets_2026-11-18.json`, a synthetic flat $100k snapshot and synthetic quotes. Check that:
- the ENTRY plan matches the prompt-06 planner (about 82 targets, 70 executable shorts);
- gate rejections appear in the log;
- a duplicate resend is refused;
- the 3-round reprice cap works;
- reconcile flags a missing fill;
- the smoke plan has exactly 4 tickets, all on 5322438.

Run all existing suites too.

Also write `docs/GOLIVE.md`: a one-page runbook for Part 2 (who does what, in which order, and the stop conditions).

Show me the diff summary and the test results. Do not place orders, do not create schedules, do not change cloud configuration.

---

## Part 2 — EN（今晚开盘后，在同一个对话里粘贴）

It is go-live time. Follow `docs/GOLIVE.md` and these rules exactly.

**Ground rules**
- Use only the moomoo paper and quote tools:
  - `sim_trade_account_list`, `sim_trade_cash_info`, `sim_trade_position_list`, `sim_trade_history_order_list`, `sim_trade_max_buy_sell`;
  - `sim_trade_input_order`, `sim_trade_cancel_order`;
  - the quote tools.
- Never use any `trading_*` or `account_*` tool, or any other live-account tool.
- Save every raw MCP response verbatim to a file, and let `live/golive.py` do all parsing, sizing and checks. Never compute an order yourself, and never change a payload from `plan.json`.
- Check the market state first. If the US regular session is not open, stop and tell me.
- Stop and report immediately on any of the following, and do not improvise:
  - an account mismatch;
  - a gate failure of the whole batch;
  - stale targets;
  - 3 consecutive MCP errors;
  - a drawdown halt;
  - anything unexpected.

**Before the open (from 22:00 SGT), read-only:**
- Read account 5832812's cash and positions.
- Make sure the P* targets for the latest completed session (2026-11-19) exist and validate.
- If they were already built today, only re-size them for this account's actual equity and holdings, using the prompt-06 entry point.

**Stage A — smoke test on 5322438 (mode test)**
1. List the paper accounts and select exactly 5322438. Save cash, positions and today's orders, then run `snapshot`. Report the equity and whether the account is flat. Then ask me to type `ARM 5322438`, and run `golive.py arm` with my exact reply.
2. Follow the call order in `docs/GOLIVE.md`: refresh the snapshot, fetch quotes for IWM and the target shorts, run `evidence --trigger "user pasted 07 Part 2"`, then `plan --smoke`. Show me the 2 opening tickets and wait. Submit only after I reply `CONFIRM`. If the approval expired before my reply, refresh, re-plan and show me again.
3. Submit, record each response, and poll the order history every 30 s until filled or until 5 minutes pass. Then check the positions:
   - Is there +1 IWM?
   - Is there −1 share of the short, as a real short position?
   - Record the fees from the cash change.
4. If the short sale is rejected, or does not create a short position, STOP. Report the exact error text and do not continue to Stage B.
5. Submit the 2 closing tickets; closing the smoke positions needs no extra confirmation. Then:
   - confirm the account is flat;
   - run `reconcile`;
   - show me a table: order, status, fill price, fee, seconds to fill;
   - tell me what you learned about the order-status fields and the day / time-in-force behaviour.

**Stage B — ENTRY batch on 5832812 (only after Stage A passed and I reply `GO`)**
1. Switch the config to `mode: competition`, `execution_enabled: true`. Then ask me to type `ARM 5832812`, and run `golive.py arm` with my exact reply.
2. List the paper accounts and select exactly 5832812. Save cash, positions and today's orders, then run `snapshot`.
   - The planner protects every position this strategy did not open; there is no `--hold` flag. List any such position for me.
3. Publish the competition-mode targets as `docs/GOLIVE.md` says. Then follow its call order: borrow checks first, then a fresh snapshot, quotes for all target symbols, `evidence --trigger "user replied GO"`, and `plan`.
   - Show me the summary, not the full list (give me the file path), and wait.
   - Submit only after I reply `CONFIRM`. If the approval expired before my reply, refresh, re-plan and show me the summary again.
4. Submit the ETF tickets first, then the shorts, recording every response.
   - A short rejected as not borrowable is skipped and logged, not replaced.
5. Poll every 60 s. After 5 minutes run `reprice` (cancel, then a new limit order for the rest), at most 3 rounds. Then cancel anything still open, because we cannot rely on day orders expiring by themselves.
6. Run `reconcile` and the risk monitor. Show me:
   - filled, skipped and rejected counts;
   - long and short gross, margin, positions;
   - the largest ticker vs $38,000;
   - fees and average slippage;
   - deviations from target;
   - executed orders today.
7. Set `execution_enabled` back to `false`, commit the journal and the reports to git, and add a timestamped entry to `agents/AGENTS.md`.

Do not place any other orders. Do not change cloud configuration or create schedules.

---

## 中文对照

### 第一部分（06 审查通过后粘贴）

目标：为今晚 P* 的第一次调仓写好上线工具。这一部分不下单、不建定时任务、不改云端配置。

背景：prompt 06 已经把 P*（版本 `pstar15`）接进来了。今晚 2026-11-20 上线（美股常规时段美东 09:30 开盘，等于新加坡时间 22:30）：
- 先在测试账户 **5322438** 做冒烟测试；
- 再在比赛账户 **5832812** 下建仓单。

moomoo MCP 只能在这个 Qoder Desktop 对话里调用，所以分工是：
- **确定性的 Python** 负责解析、算仓位、过规则门和对账；
- **聊天里的 agent** 只负责把算好的原样参数交给 MCP 工具，再把原始返回结果存回文件。

写 `live/golive.py`，包含下面这些子命令。每个子命令都在 `reports/live/golive/<日期>/<账户>/` 下读写 JSON，每一步都通过 `journal/store.py` 追加到交易日志。

1. **`snapshot --account <账户> --cash <文件> --positions <文件> [--orders <文件>]`**
   - 把 `sim_trade_cash_info`、`sim_trade_position_list`、`sim_trade_history_order_list` 的原始返回（由聊天 agent 原样保存）解析成标准化快照：权益、现金、市值、多头总额、空头总额、保证金（0.5 × 多头 + 0.6 × 空头），以及持仓（代码、带正负号的股数、平均成本、市值）。
   - 不知道的字段填 null，绝不填 0。
   - 返回结果里的账户不是 `--account` 时拒绝。
2. **`plan --account <账户> --snapshot <文件> --targets <文件> --quotes <文件> [--smoke]`**
   - 用 prompt 06 的下单规划器和 `rules/gate.py`，对照快照和目标持仓算单；买卖价取自 MCP 报价的原始返回。
   - 输出 `plan.json`。每张订单票里放 `sim_trade_input_order` 的原样参数（用 `live/relay.py` 的 `paper_payload` 生成）和规则门结果。
   - 加一段给人看的汇总：各方向笔数；多空总额（美元）；下单后的保证金；最大单只持仓和 $38,000 的对比；下单后的持仓数；被跳过的代码及原因（不足 1 股、ETF 不合格、借不到券）；按每单 $0.99 估算的手续费。
   - 没通过规则门的单不进提交列表，以"被拒"记日志，并写明原因。
   - `--smoke`（只用于 Stage A，只限账户 5322438）：
     - 开仓票：买 1 股 IWM，再从目标持仓里挑一只股价低于 $50、能成交的空头，卖空 1 股；
     - 平仓票：把这两笔平掉的两张单。
3. **`record --plan <文件> --ticket <编号> --response <文件>`**
   - 把一次提交的原始返回（order_id、状态、错误）记到对应的订单票上。
   - 已经有 order_id 的票，绝不再规划、再发送。
4. **`reprice --plan <文件> --orders <文件> --quotes <文件>`**
   - 5 分钟后还没成交的票，生成撤单参数，并按新的买卖价、同样的定价规则，给剩余数量生成新的限价票。
   - 每张票最多 3 轮。第 3 轮后还不成交就撤单，标记为 `skipped_unfilled`。
5. **`reconcile --plan <文件> --snapshot <文件> --orders <文件>`**
   - 按股数对比最终持仓和目标持仓。
   - 报告成交情况：数量、价格、能拿到的手续费、相对下单时报价的滑点（基点）。
   - 另外报告：偏离超过权益 0.25% 的持仓；保证金；持仓数；今天已成交笔数和 100 笔额度的对比（比赛上限 200 笔，留 100 笔给紧急平仓）。
   - 写出 `reconcile.json` 和 `summary.md`，并把成交记录追加到 `journal/fills.jsonl`。

**定价规则**：能立即成交的限价单。
- 买单 = 卖一价 + 0.05%；卖单 = 买一价 − 0.05%。
- 向远离中间价的方向取整到美分，并且至少越过对手价 1 美分。
- 只在常规时段（`order_trade_time_type = 1`）。
- 如果 prompt 06 的规划器已有定价规则，就用它的，并说明用的是哪一个。

**安全：**
- 提交订单不是幂等的（`text` 字段不能当去重键）。出错或超时后要重发任何一张票之前，工具必须先拿到一份新的订单历史；如果已经有对应的订单，就拒绝重发。
- **Stage B 的配置开关**：`mode: competition` 和 `execution_enabled: true` 只在 Stage B 这次运行时打开，之后 `execution_enabled` 改回 `false`。每种模式下，R1 都必须拒绝该模式账户以外的所有账户。
- 不用其他订单类型，不做盘前盘后，不做期权。
- **发布约定**：`agents/AGENTS.md` 里的 “Round 3 sequence” 那一段还要求先完整跑一个交易日的 shadow run，才能在比赛账户下单。我已经改掉这个顺序：今晚在 5322438 上的 Stage A，就是 5832812 上 Stage B 的上线依据。Stage A 指一笔 ETF 买入、一笔卖空和两笔平仓，成交和费用都要对上账。之后 Stage B 还需要我回复 `GO` 和 `CONFIRM`。把那一段改写成正好这个意思，写上今天的日期，并注明“由用户决定”。其他未验证的事项保持不变；云端相关的事项到 prompt 08 之前都保持未验证。

**测试（unittest）**，用 `reports/live/r14_targets_2026-11-18.json`、一个空仓 10 万美元的模拟快照和模拟报价做样例。检查：
- 建仓计划与 prompt 06 规划器的结果一致（约 82 个目标，70 只能成交的空头）；
- 被规则门拒绝的单出现在日志里；
- 重复发送会被拒绝；
- 改价最多 3 轮的上限有效；
- 对账能发现漏掉的成交；
- 冒烟计划正好 4 张票，并且都在 5322438 上。

原有的测试也全部跑一遍。

另外写 `docs/GOLIVE.md`：第二部分的一页操作手册（谁做什么、什么顺序、什么情况停下）。

做完把改动摘要和测试结果给我看。不下单，不建定时任务，不改云端配置。

### 第二部分（今晚开盘后，在同一个对话里粘贴）

到上线时间了。严格按 `docs/GOLIVE.md` 和下面的规则做。

**基本规则**
- 只用 moomoo 模拟盘和行情工具：
  - `sim_trade_account_list`、`sim_trade_cash_info`、`sim_trade_position_list`、`sim_trade_history_order_list`、`sim_trade_max_buy_sell`；
  - `sim_trade_input_order`、`sim_trade_cancel_order`；
  - 行情工具。
- 绝不使用任何 `trading_*`、`account_*` 工具，或其他实盘账户工具。
- 每一个 MCP 原始返回都原样存成文件，解析、算仓位和检查全部交给 `live/golive.py`。绝不自己算订单，绝不改动 `plan.json` 里的参数。
- 先查市场状态。美股常规时段没开就停下来告诉我。
- 遇到下面任何一种情况，立即停下来报告，不要自己想办法：
  - 账户对不上；
  - 整批单没通过规则门；
  - 目标持仓过期；
  - MCP 连续出错 3 次；
  - 触发回撤停机；
  - 任何意外情况。

**开盘前（新加坡时间 22:00 起），只读：**
- 读取账户 5832812 的现金和持仓。
- 确认最近一个完整交易日（2026-11-19）的 P* 目标持仓已经生成并通过校验。
- 如果今天已经生成过，只需用 prompt 06 的入口按这个账户的实际权益和持仓重新计算仓位。

**Stage A — 在 5322438 上做冒烟测试（测试模式）**
1. 列出模拟账户，只选 5322438。保存现金、持仓和今天的订单，然后运行 `snapshot`。报告权益，以及账户是不是空仓。然后请我打 `ARM 5322438`，再用我的原话运行 `golive.py arm`。
2. 按 `docs/GOLIVE.md` 里的调用顺序：刷新快照，取 IWM 和目标空头的报价，运行 `evidence --trigger "user pasted 07 Part 2"`，再运行 `plan --smoke`。把 2 张开仓票给我看，然后等待。只有我回复 `CONFIRM` 才提交。如果我回复前批准已经过期，就重新取数、重新规划，再给我看一次。
3. 提交，记录每个返回，每 30 秒查一次订单历史，直到成交或过了 5 分钟。然后检查持仓：
   - 是不是有 +1 股 IWM？
   - 是不是有那只空头的 −1 股，而且是真正的空头仓位？
   - 根据现金变化记录手续费。
4. 如果卖空被拒，或者没有形成空头仓位，就停下：报告原始错误信息，不要进入 Stage B。
5. 提交 2 张平仓票，平掉冒烟测试的仓位不需要我再确认。然后：
   - 确认账户已经空仓；
   - 运行 `reconcile`；
   - 给我一张表：订单、状态、成交价、手续费、几秒成交；
   - 告诉我你从订单状态字段和当日有效期行为上了解到了什么。

**Stage B — 在 5832812 上建仓（只有 Stage A 通过、并且我回复 `GO` 之后）**
1. 把配置切到 `mode: competition`、`execution_enabled: true`。然后请我打 `ARM 5832812`，再用我的原话运行 `golive.py arm`。
2. 列出模拟账户，只选 5832812。保存现金、持仓和今天的订单，然后运行 `snapshot`。
   - 规划器会自动保护所有不是本策略开的仓位，没有 `--hold` 参数。如果有这样的仓位，列给我看。
3. 按 `docs/GOLIVE.md` 发布比赛模式的目标文件。然后按它的调用顺序：先做借券检查，再取新的快照、所有目标代码的报价，运行 `evidence --trigger "user replied GO"`，最后运行 `plan`。
   - 只给我看汇总，不要给完整清单（给出文件路径），然后等待。
   - 只有我回复 `CONFIRM` 才提交。如果我回复前批准已经过期，就重新取数、重新规划，再把汇总给我看一次。
4. 先提交 ETF 的票，再提交空头的票，每个返回都记录下来。
   - 因为借不到券被拒的空头直接跳过并记日志，不找替代。
5. 每 60 秒查一次。5 分钟后运行 `reprice`（撤单，剩余数量重新挂限价单），最多 3 轮。然后把仍未成交的单全部撤掉，因为我们不能指望当日单会自动失效。
6. 运行 `reconcile` 和风控监控。给我看：
   - 成交、跳过、被拒的笔数；
   - 多空总额、保证金、持仓数；
   - 最大单只持仓和 $38,000 的对比；
   - 手续费和平均滑点；
   - 和目标的偏差；
   - 今天已成交笔数。
7. 把 `execution_enabled` 改回 `false`，把交易日志和报告提交到 git，并在 `agents/AGENTS.md` 里加一条带时间戳的记录。

不要下任何其他订单。不要改云端配置，不要建定时任务。
