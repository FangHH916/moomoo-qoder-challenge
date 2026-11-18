# Moomoo x Qoder Challenge — 阅读笔记（2026-11-18）

## Handbook 主页（Participant Handbook）
1. Qoder 账户：用 Luma 邮箱注册；Settings → My Billing 确认 Pro。
2. 比赛账户：moomoo app Discover → Paper Trade → Competition → Ongoing → "Moomoo x Qoder AI Paper Trading Agent Challenge"。只有这个账户的交易计分。
3. Qoder Desktop：qoder.com/download；建议项目文件夹 moomoo-trading-agent。
   3.1 Extensions → Connectors → "moomoo OpenAPI"（publisher moomoo）→ OAuth；在聊天框 tools 选择器里打开 moomoo；用 "What is the current price of US.AAPL?" 验证。
   3.2 Extensions → Skills → moomoo-skill（quotes, news, sentiment, unusual activity, simulated trading, grid strategy）→ One-click install → Qoder。用时点名 "use the moomoo-skill to …"。
   3.3 Extensions → Plugins → Qoder Cloud Agents（qca MCP + qoder-cloud-agents skill）。重启。
4. Cloud：容器里没有浏览器/OpenD，必须用 Vault。
   4.1 Cloud Agents 控制台 → Forward Mode → Vault → Create（moomoo-mcp）→ Add → MCP OAuth，URL https://mcp.moomoo.com/mcp，token/client 留空 → Connect → Moomoo OAuth（默认勾 Market Data + Watchlists）→ Confirm。每个需要 moomoo 的 agent/session 都要挂 vault，否则静默无工具。
   4.2 Skills：每个 skill 一个 zip，顶层一个文件夹=SKILL.md name，<50MB，frontmatter 有 name/description；handbook 给了让 Qoder 打包的 prompt（来源 https://github.com/MoomooOpen/moomoo-agent-hub/blob/main/docs/moomoo-install.md，规则 https://docs.qoder.com/cloud-agents/api/skills/schemas.md）。控制台 Skills → Create Skill 上传。
5. Slack channel（可选，但算"automation"分）：Forward Mode → Templates → 模板 → Channels 标签 → Slack → Create Bot → api.slack.com/apps 建 app → xapp- App token + xoxb- Bot token；Open/Pairing 模式创建后不能改；Show Thinking/Tool Calls 默认关。首条消息测试不要下单。
6. Schedule：一条请求写清 做什么/何时/时区/输出；默认时区 Asia/Shanghai，必须写明 America/New_York 等；cron 5 字段无秒，最小 1 分钟；interval 从创建时算，cron 对齐时钟；默认 max_concurrent_runs=1，重叠会 skipped(concurrency_limit_reached)；每次新 session 不带聊天历史，指令要自包含；聊天附件不会带进 schedule；自然语言 delete=归档。
   API：POST https://api.qoder.com/api/v1/forward/schedules（identity_id, template_id, environment_id, initial_events, trigger_policy{type interval/cron/once, expression, timezone, start_at, stop_at}, execution{session_mode new_session/reuse_session, max_concurrent_runs}）；/run 手动触发；GET /forward/schedule_runs/{run_id}；/pause /unpause。API 建的任务结果不会回聊天，需查 runs 或配 sinks。
7. Dashboard：本地先做（positions, P&L, equity curve, open orders, drawdown, decision log）；PAT 在 qoder.com/cloud/pat-keys，只显示一次；请求 https://api.qoder.com/api/v1/cloud，Bearer PAT，只在服务端；.env + .gitignore。加分：录屏。
8. 技巧：AGENTS.md 本地保存配置+时间戳注释（Retrieved at / Agent ID / Cloud version / Local revision / Change summary）；git 版本；更新后复核。
   结构：researcher（news/sentiment/unusual activity → 候选）、executor（只在 guard 通过后碰 paper 账户）、risk auditor（只负责说不 + 每日回撤报告，单独 session）。orchestrator prompt 加："never let the executor act on the researcher's output without the auditor's approval."
   固定 3-4 个评估 prompt 做回归 diff；trading agent 上 pin skill 版本。
   决策日志：每个决策一行结构化记录，含被拒的和原因。
9. Collaboration：coordinator + child agents（各自 Session Thread，共享文件系统）+ Advisor（只建议不行动）。每个 agent 单一职责、明确文件归属；委派规则写在 coordinator prompt；pin child 版本；凭证只给需要的 agent（coordinator 通常不需要）。https://docs.qoder.com/cloud-agents/multi-agents

## Event Details
- 时间线同前；"Top 10 entries shortlisted"。提交走 Luma 页面上的 submission link。

## Paper Trading T&C
- 只限美股/ETF/期权；4:00–20:00 ET。
- 10 万美元；可融资融券：多 50%/40%，空 60%/50%；融资 4.8%，融券 6.0%，每日收盘后计息。
- 股价 <$1、市值 <$5亿 不可交易；每日 ≤200 笔成交（撤改不算）；≤100 持仓。
- 股票/ETF：单只 ≤ 初始资产 40%（是"单个 ticker"上限，涨上去不罚但不能再买）；满仓需 ≥3 只。期权不计入 40%，单合约持仓+挂单 ≤1000 张。
- 流动性 <0 可能强平。ITM 到期收盘后自动行权；资金不足会强平。指数/个股期权按到期日收盘价结算。
- 公司行动：分红、拆股、合股、改代码；退市后第 2 个工作日按最后收盘价入账。
- 撮合：要有真实成交量；真实成交量×20 < 下单量 → 可能部分成交。
- 费用：股票佣金 0，平台费 $0.99/单，卖出活动费 $0.000166/股（上限 $8.30）；期权佣金 $0.65/张（权利金 ≤$0.10 时 $0.15），最低 $1.99/单，平台费 $0.30/张，活动费 $0.00279/张，监管费 $0.015/张。
- 结算：最后一天常规盘收盘价；最后一天盘后交易不计；周收益按每周五 22:00 ET 价格。
- 禁止 Reset Card → 自动取消资格。作弊/漏洞取消。

## Terms & Conditions
- 资格：持 Moomoo SG Universal Account 的新老用户；18+ 自然人，Singapore resident。
- **前 5 名**获奖（按评委分数），不是单看收益/排行榜。主办方可核查提交、交易记录、agent 配置。
- 奖金：第1 S$3500（现金2000+15×100券），第2 S$2500（1500+10×100），第3 S$1000（500+5×100），Honorable mention S$500（5×100券；"前5名"推断荣誉奖有 2 个）。券最低交易额 S$1，365 天有效。每人限一个奖。
- 主办方可使用昵称、头像、收益、持仓等信息宣传。一人一账户一手机号一设备。
- 参赛者对自己的 agent 负责；只能用主办方授权的工具/API/权限。

## FAQ（重点）
- Pro 本身含 2000 credits + 一次性 1500 add-on（claims.sg-cloudace.com）；可 5 折加购，最多 3 次、每天 1 次，先到先得，PayNow/信用卡（HitPay）。
- 个人赛。"策略与风险"指交易策略和金融风控。高收益高风险可能低分。可以提前提交。
- 比赛账户亏光也不能重置，但其他评分项照算。先用另一个模拟账户测试。
- 账户 17 Nov 11:00 SGT 起开通；问题找 WhatsApp 群。
- **连接正确账户：OAuth 时只选 Market Data（如 briefing 演示），不要勾 live trading execution / account-order 权限**；问 Qoder "Which accounts can I access?"（模拟账户有自己的 ID，app 里看不到也能读到）；交叉核对比赛账户；明确告诉 Qoder 只用该账户。
- 测试：在另一个模拟账户下测试单，看成交，再测平仓和盈亏。
- 价格跟随 moomoo 实时行情；盘前盘后可交易。
- 风控由参赛者定；建议止盈止损。
- 成本：按 handbook 费率 + 滑点记入交易日志。
- 多腿期权：建议单腿；多腿要拆成单独 leg 下单，占订单额度。
- MCP 工具：看 MCP quick-start 文档的工具列表，或问 Qoder "What can I do with this MCP?"。
- 历史期权数据暂不支持；回测可接别的 MCP/外部数据。
- 可以用已有 agent、数据库、MCP、知识库，但参赛作品必须用 Qoder 构建。
- moomoo AI 是另一个助手，可以把它的研究/选股作为输入。
- Dashboard 可部署 Qoder Sites 或本地；不必公开。
- **本地 Qoder 和 Cloud Agents 必须都用。**
- 不需要 QoderWake。Collaboration 开启后 agent 互通；共享 workspace memory（global/project）+ project wiki。
- 用一个 coordinator 定时调度，示例 15 分钟一次（不是要求）。
- 省 credits：稳定后简单任务用轻模型，不必 Auto mode。用量看 Observability。
- 提交：至少一个可读链接（Qoder 生成的 HTML 报告或 PPT），说明 agent 设计、策略、风控、结果；GitHub 可选；主办方能看到账户收益和 Qoder credit 用量。
- Dashboard 提示词在 Qoder Deck 里。

## Disclaimer
- 只能 SIMULATE 账户，不得把 agent 指向真实账户。

## Moomoo Deck（Alwin Aw，43 页，图片）
- p4 Getting Started：Moomoo 账户（ID+地址验证）；向 Moomoo 代表提交 Moomoo ID；Qoder Desktop > Extension 授权 moomoo MCP，OAuth 里 Market Data → "Paper Trading (Obtain Competition Account No)"。
- p5 评分：Risk-adjusted return 用 **Sharpe Ratio**；Strategy 含 "if-then rules"；"55% Trade Strategy Modeling"。
- p6 目标：2 周内找到并交易你的 edge；Edge = 交易数量 × setup 质量；你是 Risk Manager，AI agents 团队过滤负面、保留正面。
- p7 时间线（SGT）：盘前 17:00–22:30，常规 22:30–05:00(+1)，盘后 05:00–09:00(+1)。11-17~20 4 天；11-23~25 3 天（周三下午机构量变薄）；**11-26 感恩节休市**（调 bug 日）；**11-27 半天，常规盘 13:00 ET = 02:00 SGT 收，盘后到 06:00 SGT**，提前平日内仓；**11-28（周六）交易窗口正式结束**；11-30 23:59 SGT 提交。
- p8 A.I.M：A 自动化迭代——快速做简单 baseline（10:30 PM SGT 的 ORB、动量或均值回归）；I 按会话区分——代码里写死盘前/常规/盘后标志，调整仓位/订单类型/规模，不要 24/7 一套逻辑；M 节假日——11-28 02:00 SGT 前强制平仓定时器，跳过 11-26。
- p9 11 月事件：11/18 FOMC 纪要，11/25 PCE+Beige Book，11/26 休市，11/27 提前收盘（NVDA 财报 11/17 盘后，零售销售 11/17）。
- p10–17 资产风险谱：宽基/行业 ETF（SPY QQQ IWM XLF SMH）低-中；个股（NVDA TSLA AAPL AMD PLTR）中-高；杠杆/反向 ETF（TQQQ SQQQ SOXL NVDL TSLL TSLQ）高，每日重置有波动损耗；期权（SPX XSP SPY QQQ TQQQ NVDA TSLA）非线性。不能交易加密货币/期货。
- p18 8.5 天策略：ETF——宏观/指数趋势、ORB、多空行业对；个股——催化剂反应、日内均值回归、做空过度上涨股；杠杆 ETF——短线动量、无借券费对冲；期权——定义风险价差、高 IVR 铁鹰、日内借方价差。
- p19 规则：≥3 只；单只上限 40%，**建议 35–38% 留缓冲**；日内来回无隔夜利息；盘前盘后用限价单。
- p20 期权：SPX/XSP 现金结算；ETF/个股期权 ITM 16:00 ET 后自动行权；到期日收盘前自动平仓规则。
- p21 资本优先：交易前过滤；订单纪律；到期自动化；最大日回撤、个股止损、最低流动性缓冲。
- p22 研究流水线：Thesis → 多因子（宏观/基本面/技术/情绪资金流/季节性财报）→ 观点（多/空/震荡）→ 工具（ETF/个股多空/期权）→ Edge = Win%×AvgWin − Loss%×AvgLoss；执行：仓位、bracket/trailing 单、滚动。
- p23 灵感来源：CPI/PPI/Fed；行业轮动；S&P500 bull flag（screener → watchlist）；财报/季节性；ORB/趋势/均值回归；复制优秀交易者（Options Playbook 频道）；SSRN、Google Scholar。
- p24–29 数据源：moomoo 宏观数据和经济日历；moomoo 热力图 + finviz.com/groups；行业轮动周期图；moomoo screener（MA 周线、RSI14 > 50、MA30 > MA200 之类）→ 加入 watchlist 给 agent；moomoo AI bull-flag 扫描；Seasonax 季节性（中期选举后）。
- p30 示例 prompt（NVDA "Affair Rally"）：分析论点，按预设技术条件（收盘高于前一日高点且有量能配合）找回调机会，评估工具（股票/杠杆 ETF/期权），按预设风控限制下模拟单。
- p31 订单类型：Limit、Market、Stop Limit、Stop Market、Trailing Stop Limit、Trailing Stop；Session 可选 RTH+Pre/Post-Mkt。来源 https://moomoo-order-types-j692pppjg7l.qoder.website
- p32 阶梯挂单（最优成交）：限价每 5 分钟 +$0.10，上限为当前 ask，15 轮约 70 分钟；停止条件：成交/取消/到上限；后台运行。
- p33 期望值：90% 胜率也可能 −$30/笔。
- p34 风控框架：价格侧（概率 vs 盈利）、规模侧（规则管何时、最大亏损管多少、仓位管能否扛过连亏）；简单：固定金额/% 风险；进阶：Van Tharp R 倍数、Kelly。
- p35 能做：市场研究、筛选、技术分析、期权链/IV/Greeks、组合监控、仓位计算（不是券商强制限制）、交易单准备、模拟下单/改撤、定时任务、交易日志（需配置持久存储）、回测（可能需额外引擎/数据）、专职 agent（"多个 agent 同意 ≠ 独立证据"）。
- p36 不能做：不保证盈利；Desktop 权限不会自动带到云；云端访问不了本地 localhost/OpenD；定时检查有空档，"alert 不是止损单"；不是低延迟；订单 ID ≠ 成交；报价可能不新鲜；MCP ≠ app 全部功能；模拟 ≠ 实盘；**没有比赛规则/排行榜工具**，规则要自己写进去。
- p37 Moomoo Skills Hub：https://www.moomoo.com/skillhub（News Search、Sentiment Meter、Stock Digest、Technical Analysis、Capital Analysis、Derivatives Analysis…）
- p38 可接多个 MCP：tradingview.com/mcp、quantconnect.com/mcp；botspot、mcpserverfinder、mcp.aibase.com/class/Finance。
- p39–41 最终清单：确认 Connectors 里 moomoo OpenAPI 和 Qoder Cloud Agents 都是 Ready；学 Forward Mode：https://kgcedzh3.qwenwork.page/（把比赛时间线护栏加进去）；Agent Teams .MD 文件，**先从 2 个 agent 开始再扩展**；baseline 策略+进出场规则，估算每个时段交易次数，先试一笔；在 app 里 Portfolio/Performance 查看；Qoder API 文档 https://docs.qoder.com/cloud-agents/api/conventions/overview；持续用 .MD 版本迭代策略。
- p42 Dashboard 示例：https://challenge-dashboard-j692pppjg7l.qoder.website/ ；https://trading-playbook-j692pppjg7l.qoder.website/
- p43 moomoo 门店入金奖励（广告）。

## Qoder Deck（Ken Xu, Alibaba Cloud，https://ngynckpj.qwenwork.page/）
- 入口推荐：**Workbench**（for everyone，本次推荐）+ **Cloud Agents**（本次推荐）；QoderWake 不需要。
- QCA 两种模式：**Forward mode（本次推荐，起步更简单：templates & identities，自带 sessions/schedules/channels）**；Managed mode（agents/sessions/deployments/webhooks/multiagent，更自定义）。
- Agent = 指令/目标 + LLM + Skills（playbook，不是工具）+ Memory/state + Controller（选择→执行→检查）；Tools 通过 MCP（"MCP connects; it does not reason"）；Channels 输入输出。
- 最适合的任务：难做但容易检查（例如"paper portfolio review + position/order reconciliation"）。检查的是流程正确，不是未来收益。
- Skill = 可复用方法；Connector = 授权工具；Agent = 协调者。
- **Moomoo Connector 授权：只勾 Market Data / Watchlists；Accounts & Orders / Trade Execution 不要勾**（模拟账户访问不需要）。测试 prompt："List my paper trading accounts and confirm you can access the Qoder AI Agent Trading Challenge."
- QCA 里每个 moomoo skill 一个 zip；Skills → Create Skill → 上传 → 挂到 agent template。
- 两条线：管理线（Qoder：PRD、构建、测试、版本、写 dashboard → QCA plugin 配置管理）；运行线（QCA hosted agents → Skills + OAuth Vault → Moomoo MCP → US paper account）。
- Dashboard 架构：Qoder 写的前端 → 服务端 adapter（密钥只在服务端）→ Moomoo APIs（账户+订单）+ QCA APIs（sessions+events）。**示例 prompt**："Create a dashboard for my Moomoo × Qoder challenge agents. Use Moomoo integration for paper-account positions, orders and P&L, and QCA agent status, decisions and logs. Run the dashboard locally." 连之前先在 QCA 控制台建 API token，并确认 moomoo connector 已装并连上 agent。
- 用 Qoder 创作（PRD、agent template、dashboard、HTML/PPT 文档）；用 QCA 设置和运行；用 QCA plugin + QCA Assistant 调试。
- **先写 PRD**：第 1 轮目标（目标/哪些美股资产/信号），第 2 轮规则（进出场/风险限制/谁审批），第 3 轮证明可行（失败怎么办/验收测试）。起手 prompt："Help me create a PRD for the Moomoo × Qoder AI Trading Agent Challenge. Use US-only paper trading with agents running in QCA, connected to Moomoo MCP and Skills. Recommend a multi-agent architecture and the templates for each agent."
- 小循环：写 PRD → QCA 设置 → 试跑几轮或半个交易日 → 看 dashboard → 调优。调优 prompt："Read the agent logs and review the setup. Are there any issues?"；"The strategy seems conservative. Suggest a more aggressive version while keeping risk limits explicit."
- **讲者 demo：两个 agent + 一本规则手册**：Trading Agent/Scout（研究新闻、资金流、技术、情绪，用带日期的证据给出 BUY/SELL/HOLD 提议，**从不下单**）→ **确定性规则门 R1–R11（由 Guard 负责检查）** → Portfolio Agent/Guard（管账户、持仓、订单，验证提议，唯一执行通道，**从不发起交易**）→ 模拟券商 → 对账成交/记日志。每 15 分钟：检查 → 扫描（最多 8 只）→ 研究 → 决策 → 门禁/执行。"Paper · long · cash | 3.5% starter | Missing evidence → HOLD | Submitted ≠ filled"。
- 自主度分三级：人工审批每单 → 测试后小单自动+例外审批 → 在已验证范围内自主，超出就停或复核。模板片段："Before any paper order, show symbol, side, size, rationale and risk checks. Wait for explicit approval. No approval, no order. Recheck before execution." 风控门、权限、审批过期、日志、kill switch。
- 定时 prompt："Every 15 minutes on US trading days, 09:30-16:00 America/New_York, run my market-research agent. Check the exchange calendar, skip holidays, and post a concise summary here. Stop after the competition window." 查看："Show my schedules and the latest run status, errors and outputs." 停止："Delete this schedule to stop future triggers; show any runs still active." Channel 要在 template 上配置测试；从目标 channel 创建 schedule 才会回到那里。Trigger ≠ completion。
- 评分要可检查的证据：建 baseline、样本外测试（调参和评估分开）、计入成本和部分成交、决策日志含被拒和失败、可复现（写明怎么重跑）。
- 省成本：复杂分析用强模型，执行这种窄任务在稳定后用便宜模型；传紧凑状态；**风控检查保持确定性**；换模型前重测。
- Credits：Pro 含 2000；**先用完 2000 后**再领免费 1500（claims.sg-cloudace.com，用比赛邮箱）；再要可 5 折买。
- 交付：可运行包 + Qoder 生成的 deck；证据；诚实 demo。12-08 前 10 名展示。
- 求助：问 Qoder（模板 prompt：Goal/Error/Context）；QCA Assistant（控制台里，BETA）；WhatsApp 群（扫码）。

## 外部链接（已读）
- kgcedzh3.qwenwork.page = Forward Mode Schedule 指南，内容与 handbook 第 6 节相同。
- moomoo-order-types-…qoder.website：需要 Qoder OAuth 登录，未读。
- challenge-dashboard-…：私有，需登录；布局：Paper portfolio value / Day P&L / Largest position / Agent fleet；Portfolio 标签（Performance, Positions, Open orders, Recent trades, Accounts, Strategy review）+ Competition risk controls；Cloud agents monitor；Scheduler recent cycles；Feedback loop daily performance reports。
- trading-playbook-…（AA Trading Desk 参考）：每笔风险 0.5–1%；组合总风险（heat）≤6%；用 QQQ 和 20 EMA 判断市场状态，分 FULL/CAUTIOUS/DEFENSE/CASH 四档；按行业 ETF 相对强度打分（0.3R12+0.3R6+0.2R3+0.2R1，相对 SPY）；仓位 = 风险预算 ÷ |入场价 − 止损价|；禁止摊低成本；kill switch：单日 −2% 停止新开仓，连亏 3 笔仓位减半，回撤 −10% 转防守；**"A stop level is not a stop order"——它说模拟 API 只支持限价/市价单，不支持挂着的保护性止损单（需验证）**；DAY 单当天过期。
- moomoo-agent-hub（GitHub）：11 个 skill，用 `moomoo` CLI + OAuth，不需要 OpenD；`moomoo-sim-trading` 走 `moomoo sim`，**不会自动选第一个账户**；moomoo-quote（行情/K 线/screener，不下单）；moomoo-trading 是**实盘**账户，别装或别用；news-search / stock-digest / comment-sentiment 只需 curl；unusual 三件套（技术信号/资金流/衍生品）；grid-strategy 默认用模拟账户。仓库里没有 MCP 工具清单，要问 Qoder "What can I do with this MCP?"。安装：`npx skills add -y -g MoomooOpen/moomoo-agent-hub#main`。
- Qoder docs：Forward API 基址 https://api.qoder.com/api/v1/forward（templates, identities, sessions, skills, vaults, schedules…）；Managed 是 /api/v1/cloud。多 agent：最多 20 个子 agent + 1 个 Advisor；只有 coordinator 能派生子线程；对话历史按线程隔离；共享文件系统；**Vault 里的凭证 URL 必须和 agent 配置的 MCP URL 完全一致**；multiagent 更新是整体替换，只对新 session 生效；child 写 "latest" 才会跟着更新。Skill name 只能用 ^[a-z0-9][a-z0-9_-]*$，≤64 字符；description ≤5120 字符；建议每个 agent 不超过 10 个 skill。PAT 前缀 pt-。环境：setup_script 跑一次，startup_script 每次恢复都跑；进程不会跨暂停保留。Sites：可设置"Only you"或邀请访问。
- Zoom 录像：视频，未看。
- WhatsApp 群：Qoder Deck 最后一页有二维码。
- Qoder Deck https://ngynckpj.qwenwork.page/
- Zoom 录像（视频）
- https://kgcedzh3.qwenwork.page/（Forward Mode）
- https://moomoo-order-types-j692pppjg7l.qoder.website
- https://challenge-dashboard-j692pppjg7l.qoder.website/ ，https://trading-playbook-j692pppjg7l.qoder.website/
- docs：cloud-agents/overview、multi-agents、api/skills/schemas.md、api/conventions/overview、qoder/sites
- GitHub moomoo-agent-hub moomoo-install.md
- moomoo skillhub
