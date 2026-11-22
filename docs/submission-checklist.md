# 最终提交清单

更新于 2026-11-22（下午）。✅ 已完成，⏳ 进行中或待做，⬜ 可选、未做，➖ 不适用。

- 提交入口：https://survey.alibabacloud.com/uone/sg/survey/Zd8GZRUDs （表单标题 "Qoder x moomoo Agent Trading Competition"）
- 截止：**2026-11-30 23:59 SGT**。
- 计划：**等 08 的云端部分上线、报告更新完，再提交**（用户 11-22 选的方案 A），目标 11-29。
  - 可以提前交，交了之后不用继续交易。
- 表单第 6 题旁边的 "Submission guidelines HERE" 链接指向参赛手册（Participant Handbook）：https://moomoo-qoder-challenge-rgkelnyuzbm.qoder.website/ 。这是说明，不是要填的链接。
- 表单由用户本人填写并点提交。Claude 不代填个人信息，也不代点提交。
- 用户 11-22 决定：表单、报告和仓库里**都不写比赛账户的实际收益**。只写回测数字，并标明是回测。评委会直接查看账户 5832812。

## 一、表单 10 题（* 为必填）

| # | 题目 | 限制 | 内容 | 状态 |
|---|---|---|---|---|
| 1* | Full Name | ≤100 字 | 姓名，和 Luma 报名一致 | 用户填 |
| 2* | Business Email | ≤100 字 | Luma 报名邮箱（Qoder Pro 和比赛账户都绑在这个邮箱上） | 用户填 |
| 3* | Country（单选） | — | Singapore | 用户填 |
| 4* | Phone Number | ≤100 字，格式 `+65 9123 4567` | 和 Moomoo SG 账户一致的手机号 | 用户填 |
| 5* | One sentence about your project | ≤100 字符 | `docs/submission/report-content.md` 的 Q5（96 字符） | ✅ |
| 6* | Presentation Submission Link | ≤100 字符 | https://trading-agent-deck-w89b43az0ul.qoder.website/ （Qoder Sites，不登录能打开） | ✅ 链接可用；⏳ 08 上线后用 prompt 11 更新云端那几页，重新发布到同一网址 |
| 7* | Project Description | ≤1000 字符 | `report-content.md` 的 Q7（998 字符，不含收益数字，提交 5598503） | ✅ 提交前核对云端说法 |
| 8 | Github Link | ≤100 字符，选填 | https://github.com/FangHH916/moomoo-qoder-challenge （公开，MIT） | ✅ 每次 Qoder 有新提交后要再 push |
| 9* | 同意把项目资料分享给评委 | 只有 Yes | Yes | 用户选 |
| 10* | 同意阿里云联系你（营销） | 只有 Yes，必选 | Yes（以后可退订） | 用户选 |

## 二、作品内容要求（来自 Handbook、T&C、FAQ、两份 deck）

- ✅ **Qoder 生成的演示材料**：第 6 题的 HTML 报告。由 prompt 10 生成，`docs/submission/build_report.py` 会重新核算每个数字。
- ⏳ **可运行的交易 agent 包**：
  - 已有：AGENTS.md、角色说明、templates.json、skills（7 个，含 `pstar-risk-check`、`pstar-executor`）、规则闸门代码、`live/schedule-plan.json`；README 写了怎么重跑。
  - 08 B 部分还在做：上传云端、固定版本、建定时任务。
- ✅ **策略说明**：P*15，即 9 只 ETF 趋势组合 + 77 只股票空头组合（15 个文献因子、不拟合），用 Ledoit-Wolf 最小方差混合。
- ✅ **风控限制**：R1–R21 规则闸门表、单只 38% 上限、保证金 ≤ 90%（超过 95% 减到 90%）、−8% 熔断全平、Slack `PAUSE`。
- ✅ **交易决策日志**：`journal/decisions.jsonl`，被拒的决策和原因也记。
- ➖ **比赛账户的实际收益、回撤、Sharpe**：用户 11-22 决定不写，评委直接查账户。
- ✅ **什么有效、什么失败、为什么**：报告的 "What worked, what failed, what next" 页（LASSO 留出年失败后被淘汰）。

## 二·五、加分项（Handbook 6 个标签页 + FAQ + 两份 kickoff deck）

评分：风险调整收益 25%（按 **Sharpe** 算）｜ Qoder & QCA 使用 30% ｜ 策略与风控 30% ｜ 可复用与文档 15%。

### A. Qoder & Qoder Cloud Agents 使用（30%）
| # | 加分项 | 状态 |
|---|---|---|
| A1 | **本地 Qoder 和 Cloud Agents 都要用**（FAQ 硬性要求） | ✅ 本地：prompt 01–10 全部用 Qoder Desktop 完成；⏳ 云端：08 B 部分 |
| A2 | 项目文件夹作为 Qoder 工作区 | ✅ D:\Python\moomoo-qoder-challenge |
| A3 | Desktop 装好 moomoo OpenAPI connector、moomoo-skill、QCA plugin | ✅ |
| A4 | Cloud Vault（MCP OAuth，`https://mcp.moomoo.com/mcp`），只挂给要读 moomoo 的角色 | ⏳ 用户重做 OAuth，**这次要勾 Paper Trading 权限** |
| A5 | skills 打包成 zip 并上传 Skill Center | ✅ 打包（`agents/package_skills.py`）；⏳ 上传 |
| A6 | 多 agent 编排：Coordinator → Scout、Risk Auditor、Executor（Guard 已改名 Executor） | ✅ 角色说明（6f3e49c）；⏳ 云端配置 |
| A7 | 开启 Collaboration；Advisor 给第二意见 | ⏳ 08 B 部分 |
| A8 | 每个角色单一职责；只有 Executor 有下单工具，且只能用于 5832812；Coordinator 不拿凭证 | ✅ 角色说明；⏳ 云端禁用测试 |
| A9 | Pin 子 agent 和 skill 版本（不用 latest） | ✅ `agents/release-lock.json`；⏳ 云端 pin |
| A10 | Slack 推送：风控警报、异常订单、每日总结；Slack `PAUSE` / `RESUME` | ✅ 频道 #trading-agent 已接通；⏳ PAUSE 来回测试 |
| A11 | Forward Mode 定时任务（America/New_York，跳过 11-26，记录任务 ID 和下次触发时间，先手动跑一次） | ⏳ 08 B 部分 |
| A12 | 一个 Coordinator 定时跑，再调度其他角色 | ✅ 设计；⏳ 建任务 |
| A13 | Dashboard：持仓、P&L、净值曲线、挂单、回撤、决策日志、agent 状态 | ✅ 本地 `dashboard/` |
| A14 | Dashboard 加 Qoder 用量视图 / 部署到 Qoder Sites | ⬜ 可选 |
| A15 | 录屏 / 演示视频 | ⬜ 可选 |
| A16 | shared workspace memory + project wiki | ➖ Qoder 实测无法证明跨会话可用，暂停开关改用 moomoo 自选股分组 `PSTAR-PAUSE` |
| A17 | 轻模型跑 30 分钟检查，强模型跑收盘报告 | ⏳ 08 B 部分 |
| A18 | 用 Qoder 写 PRD（多轮）、用 QCA plugin 调试 | ✅ prompt 01/02、`docs/PRD.md` |
| A19 | 接外部数据源 | ✅ Alpaca 历史数据做回测 |

### B. 策略与风控（30%）
| # | 加分项 | 状态 |
|---|---|---|
| B1 | 清晰的交易逻辑和 edge | ✅ P*15（LASSO 多因子留出年失败，已淘汰） |
| B2 | 策略失效条件 | ✅ −8% 熔断、R9 锁定；研究阶段的预注册淘汰规则 |
| B3 | 仓位限制：单只 ≤ 38%（比赛上限 40%）、持仓数、保证金 | ✅ 闸门规则 |
| B4 | 回撤应对 | ✅ −4% 警告、−8% 全平并锁定 |
| B5 | Kill switch，含手动一键停 | ✅ 熔断；Slack `PAUSE`（停新单 + 撤挂单）。⏳ 用户在 moomoo app 建空分组 `PSTAR-PAUSE` 并让 Qoder 初始化成 running，**周一 11-23 21:00 SGT 前** |
| B6 | 规则闸门是确定性代码，R1 账户白名单（5322438 / 5832812） | ✅ `rules/gate_pstar.py` |
| B7 | 止盈止损计划 | ✅ 单只超 39% 减回 38%；保证金超 95% 减到 90%；−8% 全平；11-27 最终平仓 |
| B8 | 只用限价单、只在常规时段 | ✅ R7：LIMIT DAY；唯一例外是 11-27 12:45 ET 以后的最终平仓市价单 |
| B9 | 节假日：11-26 不交易；11-27 半天，收盘前全部平仓 | ✅ 设计：云端 Executor 12:15–12:45 ET（新加坡 11-28 01:15–01:45）平仓，12:50 检查；⏳ 11-25 收盘前完成演练 |
| B10 | 期权到期平仓、多腿拆单 | ➖ 不交易期权 |
| B11 | 自主度分级 | ✅ 测试期每批 `CONFIRM`；用户 11-21 改为自动执行，靠规则闸门 + Slack `PAUSE` + 异常推送 |
| B12 | 下单前展示代码、方向、数量、规则检查；提交 ≠ 成交，必须对账 | ✅ `golive.py plan` / `record` / `reconcile` |
| B13 | 先在 5322438 测试，再上比赛账户 | ✅ |

### C. 风险调整收益（25%）
| # | 加分项 | 状态 |
|---|---|---|
| C1 | 以 Sharpe 为目标设计 | ✅ 回测 Sharpe 1.17（全期）/ 1.68（留出年），年化波动 6.3% |
| C2 | 回测和日志计入手续费、滑点 | ✅ realistic 回测 |
| C3 | 计入部分成交 | ✅ 部分成交会作为异常推送并对账 |

### D. 可复用与文档（15%）
| # | 加分项 | 状态 |
|---|---|---|
| D1 | 可直接参考的 agent 模板（AGENTS.md / templates.json） | ✅ |
| D2 | 可复用 skill 和脚本 | ✅ 7 个 skill + 规则闸门 + golive 工具 |
| D3 | AGENTS.md 带时间戳的版本记录 | ✅；⏳ 云端 ID 待 08 B 部分写入 |
| D4 | 版本控制 | ✅ git，已推到公开 GitHub 仓库 |
| D5 | 固定评估 prompt，改动后重跑并对比差异 | ✅ 4 个固定 prompt + `agents/evaluate.py --self-check`；⏳ 云端对比 |
| D6 | 决策日志作为一等产物 | ✅ |
| D7 | baseline + 样本外测试 | ✅ 预注册、封存留出年 |
| D8 | README 写清楚怎么重跑 | ✅；⏳ README 顶部 "Status" 一行还是旧状态，08 B 部分第 8 步更新 |
| D9 | 报告清楚解释 agent 怎么工作 | ✅；⏳ 云端那几页用 prompt 11 更新 |
| D10 | 诚实复盘：什么有效、什么失败 | ✅ |
| D11 | 用编号的 .md 迭代 | ✅ `prompts/01–11` |

### 附加材料
- ✅ 回测报告：Sharpe、回撤、分年收益、成本、样本外、Deflated Sharpe（报告第 6–7 页）
- ✅ AGENTS.md 版本记录 + git 历史
- ⬜ Dashboard 截图或录屏（可选）
- ⬜ 1–2 分钟演示视频（可选，表单没有上传栏）

## 三、提交前检查

- ✅ 第 6 题链接在不登录的状态下能打开（11-22 实测）
- ✅ 仓库、报告里没有 token、PAT、Alpaca key、Slack token（11-22 扫过全部 git 历史）
- ✅ 报告、表单、仓库都没有比赛账户的实际收益
- ✅ 第 5、7 题字数在限制内（96 / 998）
- ⏳ 报告和 Q7 里的云端说法，和 08 实际上线的情况一致
- ⏳ README 顶部状态已更新
- ⏳ 所有交易都在 5832812 上，没碰真实账户，没用 Reset Card（比赛结束时核对）
- ⏳ 11-27 收盘前已全部平仓（12:50 ET 检查，13:10 ET 最终报告）
- ⏳ GitHub 已推到最新提交
- ⬜ 前 10 名 12-08 现场汇报，提前准备

## 四、时间表

| 日期（SGT） | 事项 |
|---|---|
| 11-22 | ✅ 08 A 部分（6f3e49c）、Q7 新版、GitHub 公开仓库；⏳ 08 B 部分 |
| 11-23 21:00 前 | 用户建 `PSTAR-PAUSE` 分组，Qoder 初始化成 running；Slack PAUSE 和 30 分钟检查上线 |
| 11-23 22:35 后 | 用户在 09 的对话里贴启动句，本地自动补仓（用 11-19 目标） |
| 11-24 ~ 11-26 | 云端每天检查和报告；**11-25 收盘前**完成最终平仓演练；11-26 休市，作为缓冲 |
| 11-28 01:15–01:45 | 云端自动最终平仓（11-27 12:15–12:45 ET），01:50 检查是否已全部平仓 |
| 08 上线后 | 贴 prompt 11 更新报告 → 检查 → push GitHub |
| 11-29 | 用户提交表单 |
| 11-30 23:59 | 最终截止 |
| 12-08 | 前 10 名汇报 |
