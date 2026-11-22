# 最终提交清单

- 提交入口：https://survey.alibabacloud.com/uone/sg/survey/Zd8GZRUDs （表单标题 "Qoder x moomoo Agent Trading Competition"）
- 截止：**2026-11-30 23:59 SGT**。计划 **11-29 晚上提交**，留一天缓冲。可以提前交，交了之后不用继续交易。
- 表单第 6 题的 "Submission guidelines HERE" 链接指向 Participant Handbook：https://moomoo-qoder-challenge-rgkelnyuzbm.qoder.website/
- 表单要你本人填写和点提交，Claude 不代填个人信息、不代点提交。

## 一、表单 10 题（* 为必填）

| # | 题目 | 限制 | 准备什么 | 谁来做 |
|---|---|---|---|---|
| 1* | Full Name | ≤100 字 | 姓名，和 Luma 报名一致 | 你 |
| 2* | Business Email | ≤100 字 | 用 **Luma 报名邮箱**（Qoder Pro 和比赛账户都绑在这个邮箱上） | 你 |
| 3* | Country（单选） | — | Singapore | 你 |
| 4* | Phone Number | ≤100 字，格式 `+65 9123 4567` | 和 Moomoo SG 账户一致的手机号 | 你 |
| 5* | One sentence about your project | ≤100 字符（英文，非常短） | 一句话概括项目，Claude 起草 | Claude 起草，你确认 |
| 6* | Presentation Submission Link | ≤100 字符 | **Qoder 生成的 deck / HTML 报告的链接**（如 Qoder Sites）。必须设成评委能打开（"Anyone can access" 或邀请主办方）。链接要短于 100 字符 | Qoder 生成，Claude 检查内容，你发布 |
| 7* | Project Description | ≤1000 字符 | 英文项目介绍：策略、agent 架构、风控、结果、Qoder 用法，Claude 起草 | Claude 起草，你确认 |
| 8 | Github Link | ≤100 字符，选填 | 可运行的 agent 包代码仓库。评委要能访问（公开仓库或给主办方权限） | Claude 整理仓库，你决定公开方式 |
| 9* | 同意把项目资料分享给评委 | 只有 Yes | 选 Yes | 你 |
| 10* | 同意阿里云联系你（营销） | 只有 Yes，必选 | 选 Yes（必选项，以后可退订） | 你 |

## 二、作品内容要求（来自 Handbook、T&C、FAQ、两份 deck）

必须有：
- [ ] **Qoder 生成的演示材料**（deck 或 HTML 报告）——第 6 题链接
- [ ] **可运行的交易 agent 包**（agent 模板 / AGENTS.md、skills、规则门代码、定时任务配置、README 说明怎么重跑）
- [ ] 策略说明（交易逻辑和 edge、为什么能赚钱）
- [ ] 风控限制（R1–Rn 规则门表、策略失效条件、仓位上限、回撤应对、kill switch）
- [ ] 交易决策日志（包括被拒绝的决策和原因）
- [ ] 成交记录和结果（比赛账户 5832812 的收益、回撤、Sharpe）
- [ ] Demo：什么有效、什么失败、为什么

## 二·五、全部加分项（Handbook 6 个标签页 + FAQ + 两份 kickoff deck，逐条对照）

评分：风险调整收益 25%（Moomoo deck：按 **Sharpe** 算）｜ Qoder & QCA 使用 30% ｜ 策略与风控 30% ｜ 可复用与文档 15%。所有项都要做。

### A. Qoder & Qoder Cloud Agents 使用（30%）
| # | 加分项 | 来源 | 状态 |
|---|---|---|---|
| A1 | **本地 Qoder 和 Cloud Agents 都要用**（FAQ：必须两者都用），构建过程用 Qoder 做（主办方看得到 credit 用量） | FAQ | ⏳ |
| A2 | 项目文件夹作为 Qoder 工作区，所有 skill、代码、dashboard、决策日志放在一起 | Handbook §3 | ✅ D:\Python\moomoo-qoder-challenge |
| A3 | Desktop 装好 moomoo OpenAPI connector、moomoo-skill、Qoder Cloud Agents plugin | Handbook §3.1–3.3 | ✅（用户已能列出账户） |
| A4 | Cloud Vault（MCP OAuth，`https://mcp.moomoo.com/mcp`），挂到每个需要 moomoo 的 agent；凭证 URL 与 agent 的 MCP URL 完全一致 | Handbook §4.1、docs | ⏳ |
| A5 | moomoo skills 打包成 QCA zip（每个 skill 一个 zip）并上传 Skill Center | Handbook §4.2 | ⏳ |
| A6 | **多 agent 编排**：Coordinator 定时调度 → Researcher/Scout、Executor/Guard、Risk Auditor（单独 session，只负责说"不"+ 每日回撤报告）；coordinator prompt 写明 "never let the executor act on the researcher's output without the auditor's approval" | Handbook §8.2、§9 | ⏳ |
| A7 | 开启 **Collaboration**；加 **Advisor**（只建议不执行）用于关键决策的第二意见 | Handbook §9 | ⏳ |
| A8 | 每个 agent 单一职责、明确文件归属；委派条件和输出格式写进 coordinator 系统提示；凭证只给需要的 agent（coordinator 不拿凭证） | Handbook §9 | ⏳ |
| A9 | **Pin** 子 agent 版本和 trading agent 的 skill 版本（不用 latest） | Handbook §8.2、§9 | ⏳ |
| A10 | **Channel（Slack）** 推送盘前扫描、风控警报、交易日志总结——Handbook 原话：这算 usage 分里"automation"那一半 | Handbook §5 | ✅ 已接通 #trading-agent |
| A11 | **Forward Mode 定时任务**：盘前扫描、盘中循环、收盘报告；写明 IANA 时区（America/New_York）、开始/结束时间（比赛窗口）、跳过休市日；建好后确认任务 ID、规则、下次触发时间；先手动 /run 测试再长期运行 | Handbook §6、Qoder deck | ⏳ |
| A12 | 一个 coordinator 定时跑、按需触发其他 agent，而不是每个 agent 各自排程（例：15 分钟） | FAQ | ⏳ |
| A13 | **Dashboard**（Handbook：评分明确奖励 dashboard 和报告）：持仓、P&L、净值曲线、挂单、回撤、决策日志、agent 状态；先本地跑通；服务端用 PAT 读 QCA sessions/events（PAT 放 .env） | Handbook §7、Qoder deck | ⏳ |
| A14 | Dashboard 加 **Qoder 用量视图**（FAQ 建议）；可部署到 **Qoder Sites**（设成仅自己/受邀访问） | FAQ | ⏳ |
| A15 | **录屏**：dashboard 实时运行（Handbook "Bonus tip"），可让 Qoder 录 | Handbook §7 | ⏳ |
| A16 | 用 shared workspace memory（global/project）+ project wiki 让 agent 共享上下文 | FAQ | ⏳ |
| A17 | 省 credits：稳定后简单任务换轻模型，复杂分析用强模型；换模型前重测 | FAQ、Qoder deck | ⏳ |
| A18 | 用 Qoder 生成 PRD（多轮：目标→规则→证明），用 QCA plugin + QCA Assistant 调试 | Qoder deck | ⏳ |
| A19 | 可接多个 MCP（TradingView、QuantConnect 等）或外部数据源（我们用 Alpaca 历史数据做回测） | Moomoo deck、FAQ | ✅ Alpaca 回测数据 |

### B. 策略与风控（30%）
| # | 加分项 | 来源 | 状态 |
|---|---|---|---|
| B1 | 清晰的交易逻辑和 edge（多因子：清洗→去极值→行业+市值中性化→LASSO 合成） | Event Details、T&C | ⏳ 回测中 |
| B2 | **策略失效条件**（defined conditions that invalidate the strategy） | Event Details | ⏳ |
| B3 | **仓位限制**：单只 ≤35–38%（比赛上限 40%）、持仓数、做空总规模、期权风险 | T&C、Moomoo deck | ⏳ |
| B4 | **回撤应对**：单日 −2% 停开仓、连亏减仓、−8% 全平停机 | Event Details | ⏳ |
| B5 | **Kill switch**（含手动一键停） | Event Details、Qoder deck | ⏳ |
| B6 | 规则门是**确定性代码**（不靠 LLM），R1 账户白名单（test=5322438 / competition=5832812） | Qoder deck（"Keep risk checks deterministic"） | ⏳ |
| B7 | 止盈止损计划 | FAQ | ⏳ |
| B8 | 按时段区分：盘前/常规/盘后标志，盘前盘后只用限价单 | Moomoo deck（A.I.M 的 I） | ⏳ |
| B9 | 节假日处理：11-26 不交易，11-27 在 02:00 SGT 前平日内仓 | Moomoo deck（A.I.M 的 M） | ⏳ |
| B10 | 期权到期日收盘前自动平仓；多腿拆单 + 单腿成交保护 | Moomoo deck、FAQ | ⏳ |
| B11 | 自主度分级：测试期每单人工批准 → 限额内自动 → 超限停/复核；审批有过期时间 | Qoder deck | ⏳ |
| B12 | 下单前展示 symbol/side/size/理由/风控检查，执行前复核；提交 ≠ 成交，必须对账 | Qoder deck、Handbook §5 | ⏳ |
| B13 | 先在 5322438 测试：下单→成交→平仓→看盈亏，再上比赛账户 | FAQ | ⏳ |

### C. 风险调整收益（25%）
| # | 加分项 | 来源 | 状态 |
|---|---|---|---|
| C1 | 以 Sharpe 为目标设计（控制波动，不赌高收益；高风险即使赚钱也可能低分） | Moomoo deck、FAQ | ⏳ |
| C2 | 交易日志里计入手续费和滑点 | FAQ | ⏳ |
| C3 | 计入模拟撮合的部分成交（成交量 ×20 规则） | T&C、Qoder deck | ⏳ |

### D. 可复用与文档（15%）
| # | 加分项 | 来源 | 状态 |
|---|---|---|---|
| D1 | 可直接参考的 **agent 模板**（AGENTS.md / template 配置） | Event Details | ⏳ |
| D2 | **可复用 skill 和脚本**（规则门、交易日志、日报、因子研究 skill） | Event Details | ⏳ |
| D3 | **AGENTS.md 本地副本 + 带时间戳注释**（Retrieved at / Agent ID / Cloud version / Local revision / Change summary） | Handbook §8.1 | ⏳ |
| D4 | **版本控制**：git，每次改动前保存、写原因、保留最终提交版本；更新后对比云端配置确认只改了想改的 | Handbook §8.1 | ✅ git 已建 |
| D5 | **固定 3–4 个评估 prompt**，每次改 prompt/skill 后重跑并 diff，再放进"生产" agent | Handbook §8.2 | ⏳ |
| D6 | **决策日志作为一等产物**：每个决策一行结构化记录（含被拒的和原因），dashboard 画出来 | Handbook §8.2 | ⏳ |
| D7 | 建 **baseline**，展示改进了什么；**样本外测试**（调参和评估分开） | Qoder deck | ⏳ |
| D8 | **可复现**：README 写清楚怎么重跑 | Qoder deck | ⏳ |
| D9 | Qoder 生成的 deck / HTML 报告清楚解释 agent 怎么工作 | Event Details、FAQ | ⏳ |
| D10 | 诚实 demo：什么有效、什么失败、为什么 | Event Details、Qoder deck | ⏳ |
| D11 | 用 .MD 版本持续迭代策略，记录每次优化 | Moomoo deck | ⏳ |

### 已有的附加材料
- [ ] Dashboard 截图或录屏（持仓、盈亏、净值、回撤、决策日志、agent 状态）
- [ ] （可选）demo 旁白用用户自己的克隆声音：GPT-SoVITS v2 Windows 整合包（RTX 3050 Laptop 4GB：推理可以，微调 batch 1–2），录 1–3 分钟英文样本。等回测跑完、内存空出来再装，11-26 休市日前后做。
- [ ] **1–2 分钟演示视频**（Handbook 第 7 节的 bonus tip，非必须）：dashboard 实时画面 → QCA agent 运行和定时触发 → Slack 消息 → 一笔交易从提议到规则门（最好录到一次拦截）到成交入日志。表单没有上传位，传到网盘（Google Drive / YouTube 不公开）后把链接写进 deck 和第 7 题。11-27 或 11-28 录。
- [ ] 自己写的可复用 skill（规则门、交易日志、日报），上传 QCA 并 pin 版本
- [ ] 回测报告：多因子 + 中性化 + LASSO，年化/月化收益、Sharpe、最大回撤、换手率；baseline 对比；成本和部分成交计入；Deflated Sharpe
- [ ] AGENTS.md 版本记录 + git 提交历史（可复现）
- [ ] 诚实复盘：每天"什么失败了、为什么、改了什么"

评委能直接看到：比赛账户收益、Qoder credit 用量（所以构建过程要用 Qoder 做）。

## 三、提交前检查

- [ ] 第 6 题链接在**无痕窗口**里能打开（不是"Only you can access"）
- [ ] 链接、仓库、截图里**没有** token / PAT / Alpaca key / Slack token
- [ ] 所有交易都在 5832812 上，没有碰过真实账户，没用过 Reset Card
- [ ] 第 5、7 题字数在限制内
- [ ] 最后一个交易日（11-27 半天）的日内仓位在 02:00 SGT 前已平
- [ ] 前 10 名 12-08 现场汇报，提前准备好演示

## 四、时间表

| 日期 | 事项 |
|---|---|
| 11-18 | 回测、skill 索引 |
| 11-19 | 定策略参数 → 写给 Qoder 的 prompt → Qoder 搭 agent → 5322438 测试 |
| 11-20 晚 | 切到比赛账户 5832812 |
| 11-20 ~ 11-25 | 自动交易，每天复盘一次；同时做 dashboard、skill |
| 11-26 | 休市：修 bug、整理文档 |
| 11-27 | 半天，02:00 SGT 前平仓 |
| 11-28 ~ 11-29 | Qoder 生成 deck、录屏、整理仓库、起草第 5/7 题 → **11-29 提交** |
| 11-30 23:59 | 最终截止 |
| 12-08 | 前 10 名汇报 |
