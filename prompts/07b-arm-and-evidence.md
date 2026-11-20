# Prompt 07b — 上线前补两个缺口：熔断开关启用 + 本地模式证据文件

用法：在做 07 Part 1 的**同一个 Qoder 对话**里粘贴 EN 部分，在 Part 2 之前。做完后告诉 Claude，Claude 审代码、跑测试，通过后再贴 07 Part 2。

为什么要这一步（Claude 11-20 21:45 审查发现）：
- 两个账户的熔断开关（R16）都处于"未启用（Not armed）"，而且没有启用命令。不处理的话，每一张单都会被规则门拦下。
- 规则门 R12/R14/R17 和空头的借券检查都依赖一个 `evidence.json`，但 Part 2 没有说怎么填。Qoder 自己写的手册说"不要填你没验证过的项"，所以到时它要么停下，要么自己编。这里由你来定本地模式下每一项的含义。

---

## EN（粘贴这段）

Before Part 2, fix two blockers in the go-live tooling. Still no orders, no schedules, no cloud changes.

**1. R16 is unarmed and there is no arm command.** `Journal.control()` returns "Not armed" for both 5322438 and 5832812, so every order would be halted. Add:
- `golive.py arm --account <id> --reply "<text>"`:
  - only for the account of the current config mode (5322438 in test, 5832812 in competition);
  - `--reply` must be exactly `ARM <id>`, the text I type in chat; refuse anything else;
  - set `halted=0` with reason "armed by the user in chat at <ts>", and journal the event.
- `golive.py halt --account <id> --reason "<text>"`: latch the halt again, using the existing store method.
- The chat agent never arms on its own initiative. It asks me to type `ARM <id>`, then passes my exact reply.

**2. `evidence.json` for local execution.** Cloud agents are read-only, and every order goes through this chat after my `CONFIRM` (decision 1A). So I decide (2026-11-20) what each evidence item means in local mode. Add `golive.py evidence --account <id> --snapshot <file> --orders <file> --trigger <text> --out <file>`. Python computes or fills every field; the chat agent never writes this file by hand.
- `release_verified`: computed. True only if `live/*.py`, `rules/*.py` and `journal/*.py` have no uncommitted changes. Record the commit hash. Edits to the config's `mode` and `execution_enabled` do not count, because targets and the gate already check the config checksum.
- `lease_verified`: true, with basis "local mode, decided by the user 2026-11-20: this Qoder Desktop chat is the only order sender; no cloud agent exists or has order tools".
- `permission_policy_verified`: true, with basis "every submit batch needs the user's `CONFIRM` in this chat".
- `reconciled`: computed. True only if the fresh order history has no open, unknown or cancel-pending order for the account. Otherwise false, with the reason.
- `snapshot_verified_by_relay`: true only if the snapshot was made by `golive.py snapshot` and is bound to the account through `--accounts`.
- `approval`:
  - `approval_id` = `golive-<date>-<account>-<hhmmss>`;
  - `issued_at` = now, `expires_at` = now + 240 s;
  - `auditor_session_id` = "golive-deterministic-checks", `guard_session_id` = "qoder-desktop-chat";
  - `confirmed_by` = "user", `trigger` = `--trigger` (Stage A: "user pasted 07 Part 2"; Stage B: "user replied GO");
  - `verified_by_relay` true;
  - `basis` = "local mode decided by the user 2026-11-20: the independent check is the deterministic golive/gate run, not a separate agent; the human control is the user's CONFIRM before any submission".
- `borrow.easy_to_borrow_proxy` = "sim_trade_max_buy_sell.max_sell_short_qty". I accept this proxy: the paper tools expose no easy-to-borrow flag, so a short passes only if `max_sell_short_qty` ≥ its planned quantity.

**3. Timing.** R11 needs the snapshot and quotes to be at most 60 s old when `plan` runs. Put this order in `docs/GOLIVE.md`:
1. Borrow checks (`sim_trade_max_buy_sell`) first. They are slow and have no freshness limit.
2. Then, back to back: account list, cash, positions and orders → `snapshot` → quotes for all names in as few calls as possible → `evidence` → `plan`.
3. If `plan` reports R11, refetch cash, positions and quotes and run it again. That sends nothing.

Also add: if my `CONFIRM` arrives after `expires_at`, do not submit. Refresh the snapshot, quotes and evidence, re-plan, and show me the summary again.

**4. Docs, tests and commit.**
- Update `docs/GOLIVE.md`:
  - arm and halt, evidence, the call order above and the expiry rule;
  - remove the R16 "known gap";
  - state that `--hold` is not needed, because the planner protects every position this strategy did not open.
- Add a dated entry to `agents/AGENTS.md`: "Local execution evidence — decided by the user on 2026-11-20", with the meanings above.
- Tests (unittest):
  - `arm` refuses a wrong reply, a wrong account and the other mode's account;
  - after `arm` + `evidence`, the smoke plan passes R12, R14, R16 and R17;
  - without `arm`, R16 still rejects;
  - an approval older than 300 s fails R14;
  - a short whose `max_sell_short_qty` is below its quantity fails R5;
  - `reconciled` is false when the history has an open order;
  - `release_verified` is false with an uncommitted change in `live/`.
- Run all suites.
- Then commit the code, the docs, `agents/` and `prompts/` in one commit. First check `git status`; never commit `.env` or any credential.
- Show me the diff summary, the test results and the commit hash.

Do not arm any account now.

---

## 中文对照

在 Part 2 之前，先修上线工具里的两个阻断问题。仍然不下单、不建定时任务、不改云端配置。

**1. R16 熔断开关没有启用，也没有启用命令。** `Journal.control()` 对 5322438 和 5832812 都返回 "Not armed"，所以每一张单都会被拦下。新增：
- `golive.py arm --account <账户> --reply "<文本>"`：
  - 只能启用当前配置模式对应的账户（test 模式是 5322438，competition 模式是 5832812）；
  - `--reply` 必须正好是 `ARM <账户>`，也就是我在聊天里打的字，其他一律拒绝；
  - 把 `halted` 设成 0，原因写"用户在聊天中启用，时间 <ts>"，并记入交易日志。
- `golive.py halt --account <账户> --reason "<文本>"`：重新锁住熔断，用 store 里已有的方法。
- 聊天 agent 绝不自己启用。它要先请我打 `ARM <账户>`，再把我的原话传进去。

**2. 本地执行用的 `evidence.json`。** 云端 agent 只读，所有订单都经过这个对话、由我回 `CONFIRM`（决定 1A）。所以由我来定（2026-11-20）本地模式下每一项证据的含义。新增 `golive.py evidence --account <账户> --snapshot <文件> --orders <文件> --trigger <文本> --out <文件>`。每个字段都由 Python 计算或填写，聊天 agent 绝不手写这个文件。
- `release_verified`：计算得出。只有 `live/*.py`、`rules/*.py`、`journal/*.py` 都没有未提交的改动时才为 true，并记下 commit 哈希。改配置里的 `mode` 和 `execution_enabled` 不算，因为目标文件和规则门已经会核对配置校验和。
- `lease_verified`：true，依据写"本地模式，用户 2026-11-20 决定：只有这个 Qoder Desktop 对话会下单；没有云端 agent，也没有任何云端 agent 拿到下单工具"。
- `permission_policy_verified`：true，依据写"每一批提交都需要用户在这个对话里回复 `CONFIRM`"。
- `reconciled`：计算得出。只有最新的订单历史里，该账户没有挂着的、状态未知的、或正在撤单的订单时才为 true；否则为 false，并写明原因。
- `snapshot_verified_by_relay`：只有快照是 `golive.py snapshot` 生成、并且通过 `--accounts` 绑定了账户时才为 true。
- `approval`：
  - `approval_id` = `golive-<日期>-<账户>-<时分秒>`；
  - `issued_at` = 现在，`expires_at` = 现在 + 240 秒；
  - `auditor_session_id` = "golive-deterministic-checks"，`guard_session_id` = "qoder-desktop-chat"；
  - `confirmed_by` = "user"，`trigger` = `--trigger` 的值（Stage A 写"用户贴了 07 Part 2"，Stage B 写"用户回复了 GO"）；
  - `verified_by_relay` 为 true；
  - `basis` 写"本地模式，用户 2026-11-20 决定：独立检查就是确定性的 golive/规则门运行，不是另一个 agent；人工控制是提交前用户的 `CONFIRM`"。
- `borrow.easy_to_borrow_proxy` = "sim_trade_max_buy_sell.max_sell_short_qty"。我接受这个替代指标：模拟盘工具没有"容易借券"的标志，所以只有 `max_sell_short_qty` ≥ 计划股数时，这只空头才算通过。

**3. 时效。** R11 要求运行 `plan` 时，快照和报价都不超过 60 秒。在 `docs/GOLIVE.md` 里写明这个顺序：
1. 先做借券检查（`sim_trade_max_buy_sell`）。这一步慢，但没有时效要求。
2. 然后一口气做完：账户列表、现金、持仓、订单 → `snapshot` → 用尽量少的调用拿到所有代码的报价 → `evidence` → `plan`。
3. 如果 `plan` 报 R11，就重新拿现金、持仓和报价，再跑一次。这一步不会发出任何订单。

另外写明：如果我的 `CONFIRM` 晚于 `expires_at` 才到，不要提交。重新取快照、报价和证据，重新规划，再把汇总给我看一次。

**4. 文档、测试和提交。**
- 更新 `docs/GOLIVE.md`：
  - 写上 arm 和 halt、evidence、上面的调用顺序和过期规则；
  - 删掉 R16 那条"已知缺口"；
  - 写明不需要 `--hold`，因为规划器会自动保护所有不是本策略开的仓位。
- 在 `agents/AGENTS.md` 加一条带日期的记录："本地执行证据——由用户于 2026-11-20 决定"，写明上面每一项的含义。
- 测试（unittest）：
  - `arm` 会拒绝错误的回复、错误的账户、以及另一个模式的账户；
  - 做了 `arm` 和 `evidence` 之后，冒烟计划能通过 R12、R14、R16、R17；
  - 没有 `arm` 时，R16 仍然拒绝；
  - 超过 300 秒的批准过不了 R14；
  - `max_sell_short_qty` 小于计划股数的空头过不了 R5；
  - 订单历史里有挂单时，`reconciled` 为 false；
  - `live/` 里有未提交的改动时，`release_verified` 为 false。
- 跑全部测试。
- 然后把代码、文档、`agents/` 和 `prompts/` 一起提交成一个 commit。先看 `git status`；绝不提交 `.env` 或任何凭证。
- 给我看改动摘要、测试结果和 commit 哈希。

现在不要启用任何账户。
