# Prompt 10 — 提交用的演示报告（表单第 6 题 Presentation Submission Link）

用法：**新开一个 Qoder 对话**，贴 EN 部分。它只改 `docs/submission/` 和 `prompts/`，不碰 `live/`、`rules/`、`journal/`、`agents/`，所以可以和 08 并行做。做完告诉 Claude，Claude 打开页面逐页核对内容和数字。发布到 Qoder Sites 时，如果要登录或点按钮，由你来做。

用户 11-22 决定：表单没有要求写策略的实盘收益（主办方自己能看到账户），所以报告和 Q7 都不写实盘结果，做好就可以提交，不用等 11-27 之后。

为什么这样设计：
- handbook 要求至少交一个 Qoder 生成、可读的 HTML 报告或 PPT，讲清 agent 设计、策略、风控、结果（这里的结果用回测）；
- 前 10 名 12-08 要现场汇报，所以做成"能翻页的 deck，也能往下滚着读"；
- 表单第 6 题链接 ≤ 100 字符，评委必须不登录就能打开。

---

## EN（粘贴这段）

Build the presentation for the competition submission: a Qoder-generated HTML deck that answers form question 6, "Presentation Submission Link". Do not place, cancel or modify any order. Do not change any cloud agent, template or schedule. Edit only `docs/submission/` and `prompts/`; do not touch `live/`, `rules/`, `journal/` or `agents/`, so the release lock stays sealed.

**Audience and goal**
- The readers are the judges from moomoo and Alibaba Cloud/Qoder. They score:
  - Qoder and Qoder Cloud Agents usage 30%;
  - strategy and risk management 30%;
  - risk-adjusted return (Sharpe) 25%;
  - reusability and documentation 15%.
- The top 10 present on 2026-12-08, so the same page must work as slides on a projector and as a report someone scrolls through.
- Language: English.

**Source of truth**
- Content comes from `docs/submission/report-content.md`. Every claim and number must come from that file, from a file in this repo, or from the build script below. Invent nothing.
- I decided on 2026-11-22 not to report live returns: the organisers see the competition account directly. Every performance number in the deck is a backtest number; say so on each chart and table.
- Fix one inconsistency while you are there: §3 "Book at go-live" says "signals of 2026-11-17". Take the go-live book from `live/targets/competition/2026-11-19-pstar.json` (UUP 28.9%, SPY 21.4%, …) and correct §3 to match.
- Describe the system as it exists in the repo when you build. The cloud executor, the Slack `PAUSE` listener and the Slack alerts are being built in prompt 08. Until they exist, label them "being deployed". I may ask for a refresh after 08.

**Slides** (one section of the content file → one or two slides)
1. Title: project name, the one-sentence summary (Q5), "Built with Qoder", competition account 5832812, date.
2. TL;DR (§1), without the live-results line.
3. What we tried and what failed (§2) as a timeline. The failures are evidence.
4. Research discipline: pre-registration in git, the sealed holdout, deflated Sharpe.
5. Strategy P*15 (§3) with a small diagram: T9 trend sleeve + D77 short book → Ledoit-Wolf minimum variance → scale K → the go-live book.
6. Backtest (§4):
   - the results table;
   - an equity curve with the selection and holdout windows shaded;
   - a drawdown chart;
   - calendar-year bars;
   - the costs modelled.
7. Agent architecture (§5) as a diagram: research → targets → local Qoder (golive, gate, journal) → moomoo paper account; Qoder Cloud agents, Slack and the dashboard.
8. One order's path, with real records from `journal/decisions.jsonl`:
   - the IWM order 3075765, from plan to gate to fill to journal;
   - at least one plan the gate rejected, with its rule and reason (for example the stale-quote R11 holds, or UUP's fund size under R2 on 2026-11-20).
9. Risk management (§6) as a table.
10. Automation and the human control: automatic execution since 2026-11-21, `PAUSE`/`RESUME`, the order alerts, the scheduled final exit before the 27 Nov close, and why the per-batch `CONFIRM` was dropped.
11. What worked, what failed, what next (§8).
12. How we used Qoder:
    - the numbered prompts in `prompts/` as the build log, with dates from `git log`;
    - the `agents/AGENTS.md` revisions;
    - the test suites and their counts;
    - the cloud agents, skills and the Slack channel.
13. Reproduce (§9).
14. Appendix: a table mapping each judging criterion to the slide and the repo file that is its evidence.

**Build script: `docs/submission/build_report.py`**
- Reads `reports/lab/daily_R14_realistic.csv` and draws the charts as inline SVG.
- Recomputes Sharpe, annual return, volatility and max drawdown for the full period, the selection window (2021-11-18 → 2025-11-17) and the holdout (2025-11-18 → 2026-11-17), plus the calendar-year returns.
  - Use the same formulas as `metrics()` in `research/r10_deploy.py`.
  - Stop with an error if any of them differs from the §4 table or the year list after rounding.
- Prints the character counts of the Q5 and Q7 answers and fails if Q5 > 100 or Q7 > 1000.
- Writes one self-contained file, `docs/submission/site/index.html`.
- Standard library plus pandas only.

**Page requirements**
- One self-contained HTML file: inline CSS, JS and SVG. No external scripts, fonts, trackers or analytics.
- 16:9 slides that scale to the window. Navigate with ←/→, click and a slide counter. On a phone it scrolls as one long page.
- Printing gives one slide per page, so it can be saved as PDF.
- Light theme, one accent colour, high contrast, readable on a 1280×720 projector.
- Privacy and secrets:
  - no personal name, email or phone number;
  - no token, PAT, API key, vault ID or session ID;
  - say nothing about the test account's holdings. Where §7 mentions it, write "the test account held an unrelated position".

**Form answers**: replace Q5 and Q7 in `report-content.md` with these, unchanged, and remove the line that says both must be re-counted after the live numbers.
- Q5: `Research-first agent team trading a pre-registered ETF-trend + short-alpha book on moomoo paper.`
- Q7:
  > We built the trading desk around one question: what still works out of sample? Our first idea, a LASSO multi-factor stock model, failed its sealed holdout year (Sharpe −0.30) and was retired. From then on every experiment was pre-registered in git. The survivor, P*15, combines a 9-ETF trend sleeve with a 77-name short book ranked by 15 literature factors with no fitting, mixed by Ledoit-Wolf minimum variance: five-year Sharpe 1.17 after costs, max drawdown −6.9%, every year positive. Qoder built the agent stack. Python sizes every order and a deterministic rule gate checks it (account whitelist, 38% ticker cap, margin ≤ 90%, −8% kill switch); the LLM only relays. Execution is automatic: local Qoder runs the entry, Qoder Cloud agents run the final exit and risk cuts on schedule, flag anomalous orders in Slack, and stop on a Slack PAUSE. Every decision, including rejections, is journaled.

**Publish**
- Deploy `docs/submission/site/index.html` with Qoder Sites.
- Access must be "anyone with the link", with no login. The URL must be shorter than 100 characters.
- If publishing needs my login, a click or a setting, stop and tell me exactly what to do.
- Any later update goes to the same URL.

**Finish**
- Run the build script and show me:
  - its metric check;
  - the Q5/Q7 character counts;
  - the slide list;
  - the published URL.
- Commit only `docs/submission/` and `prompts/` in one commit. Check `git status` first; never commit `.env` or any credential. Show me the commit hash.

---

## 中文对照

为比赛提交做演示材料：一个由 Qoder 生成的 HTML deck，用来回答表单第 6 题 "Presentation Submission Link"。不要下单、撤单或改单。不要改任何云端 agent、模板或定时任务。只改 `docs/submission/` 和 `prompts/`；不要碰 `live/`、`rules/`、`journal/`、`agents/`，这样发布锁保持封存状态。

**读者和目标**
- 读者是 moomoo 和阿里云/Qoder 的评委。评分项：
  - Qoder 和 Qoder Cloud Agents 的使用 30%；
  - 策略与风控 30%；
  - 风险调整收益（夏普）25%；
  - 可复用性与文档 15%。
- 前 10 名 2026-12-08 现场汇报，所以同一个页面既要能在投影上当幻灯片用，也要能当报告往下滚着读。
- 语言：英文。

**内容来源**
- 内容来自 `docs/submission/report-content.md`。每一句说法、每一个数字都必须来自这个文件、仓库里的文件，或下面的构建脚本。不要编造任何东西。
- 我 2026-11-22 决定不写实盘收益：主办方能直接看到比赛账户。deck 里所有业绩数字都是回测数字，每张图和表上都要写明。
- 顺手修一处不一致：§3 "Book at go-live" 写的是 "signals of 2026-11-17"。上线时的组合以 `live/targets/competition/2026-11-19-pstar.json` 为准（UUP 28.9%、SPY 21.4%……），把 §3 改成一致。
- 按构建时仓库里实际有的东西来描述系统。云端执行、Slack `PAUSE` 监听和 Slack 警报正在 prompt 08 里做；做好之前，标成"部署中"。08 做完后我可能让你刷新一次。

**幻灯片**（内容文件的每一节 → 一到两页）
1. 标题页：项目名、一句话简介（Q5）、"Built with Qoder"、比赛账户 5832812、日期。
2. 一页摘要（§1），去掉实盘结果那一行。
3. 试过什么、什么失败了（§2），做成时间线。失败本身就是证据。
4. 研究纪律：在 git 里预登记、封存的留出期、Deflated Sharpe。
5. P*15 策略（§3），配一张小图：T9 趋势仓位 + D77 空头组合 → Ledoit-Wolf 最小方差 → 缩放系数 K → 上线时的组合。
6. 回测（§4）：
   - 结果表；
   - 净值曲线，选择期和留出期用底色区分；
   - 回撤图；
   - 各年收益柱状图；
   - 计入的成本。
7. agent 架构（§5），画成图：研究 → 目标 → 本地 Qoder（golive、规则门、日志）→ moomoo 模拟账户；Qoder 云端 agent、Slack 和 dashboard。
8. 一张单的完整路径，用 `journal/decisions.jsonl` 里的真实记录：
   - IWM 订单 3075765，从规划到规则门到成交到记入日志；
   - 至少一个被规则门拒掉的计划，写明是哪条规则、什么原因（例如报价过期被 R11 挡住，或 2026-11-20 UUP 基金规模不够被 R2 挡住）。
9. 风控（§6），做成表格。
10. 自动化和人工控制：2026-11-21 起自动执行，`PAUSE`/`RESUME`，订单警报，27 日收盘前的定时最终平仓，以及为什么去掉了每批的 `CONFIRM`。
11. 什么有效、什么失败、下一步（§8）。
12. 我们怎么用 Qoder：
    - `prompts/` 里编号的 prompt 就是构建记录，日期取自 `git log`；
    - `agents/AGENTS.md` 的各个版本；
    - 各测试套件和测试数量；
    - 云端 agent、skill 和 Slack 频道。
13. 复现方法（§9）。
14. 附录：一张表，把每个评分项对应到证明它的那一页和仓库文件。

**构建脚本：`docs/submission/build_report.py`**
- 读取 `reports/lab/daily_R14_realistic.csv`，把图画成内嵌 SVG。
- 重新计算全期、选择期（2021-11-18 → 2025-11-17）、留出期（2025-11-18 → 2026-11-17）的夏普、年化收益、波动率、最大回撤，以及各年收益。
  - 用和 `research/r10_deploy.py` 里 `metrics()` 相同的公式。
  - 四舍五入后只要有一个和 §4 的表格或年度列表对不上，就报错停止。
- 打印 Q5 和 Q7 的字符数，Q5 超过 100 或 Q7 超过 1000 就报错。
- 输出一个自包含的文件 `docs/submission/site/index.html`。
- 只用标准库和 pandas。

**页面要求**
- 一个自包含的 HTML 文件：CSS、JS、SVG 全部内嵌。不加载任何外部脚本、字体、跟踪或统计代码。
- 16:9 的幻灯片，随窗口缩放。用 ←/→、点击翻页，显示页码。手机上变成一整页往下滚。
- 打印时一页一张幻灯片，方便存成 PDF。
- 浅色主题，一个强调色，高对比度，在 1280×720 的投影上看得清。
- 隐私和密钥：
  - 不出现任何人名、邮箱、电话；
  - 不出现任何 token、PAT、API key、vault ID、session ID；
  - 不提测试账户里有什么持仓。§7 提到它的地方，写成"测试账户里有一个无关的持仓"。

**表单答案**：把 `report-content.md` 里的 Q5 和 Q7 原样换成下面两段，并删掉"填入实盘数字后要重新数字数"那一行。
- Q5：`Research-first agent team trading a pre-registered ETF-trend + short-alpha book on moomoo paper.`
  （中文意思：以研究为先的 agent 团队，在 moomoo 模拟盘上交易一个事先登记好的"ETF 趋势 + 空头 alpha"组合。）
- Q7（英文原样写进文件）：
  > We built the trading desk around one question: what still works out of sample? Our first idea, a LASSO multi-factor stock model, failed its sealed holdout year (Sharpe −0.30) and was retired. From then on every experiment was pre-registered in git. The survivor, P*15, combines a 9-ETF trend sleeve with a 77-name short book ranked by 15 literature factors with no fitting, mixed by Ledoit-Wolf minimum variance: five-year Sharpe 1.17 after costs, max drawdown −6.9%, every year positive. Qoder built the agent stack. Python sizes every order and a deterministic rule gate checks it (account whitelist, 38% ticker cap, margin ≤ 90%, −8% kill switch); the LLM only relays. Execution is automatic: local Qoder runs the entry, Qoder Cloud agents run the final exit and risk cuts on schedule, flag anomalous orders in Slack, and stop on a Slack PAUSE. Every decision, including rejections, is journaled.

  中文意思：我们围绕一个问题搭建交易台：什么在样本外依然有效？第一个想法是 LASSO 多因子选股模型，它在封存的留出年份里失败了（夏普 −0.30），随即被淘汰。从那以后，每个实验都先在 git 里预登记。最终留下的是 P*15：9 只 ETF 的趋势仓位，加上 77 只股票的空头组合；空头按 15 个文献因子排序，不做拟合；两部分用 Ledoit-Wolf 最小方差混合。扣除成本后，5 年夏普 1.17，最大回撤 −6.9%，每年都是正收益。agent 体系由 Qoder 搭建。每张单的数量由 Python 计算，再由确定性规则门检查（账户白名单、单只 38% 上限、保证金 ≤ 90%、−8% 熔断）；LLM 只负责转发。执行是全自动的：本地 Qoder 负责建仓，Qoder 云端 agent 按定时任务做最终平仓和风控减仓，在 Slack 里标出异常订单，收到 Slack 的 PAUSE 就停下。每个决策，包括被拒的，都记入日志。

**发布**
- 用 Qoder Sites 发布 `docs/submission/site/index.html`。
- 访问权限必须是"任何拿到链接的人"，不用登录。链接要短于 100 个字符。
- 如果发布需要我登录、点按钮或改设置，停下来，告诉我具体怎么做。
- 以后如有更新，都发布到同一个链接。

**收尾**
- 运行构建脚本，给我看：
  - 指标核对结果；
  - Q5/Q7 的字符数；
  - 幻灯片清单；
  - 发布后的链接。
- 只把 `docs/submission/` 和 `prompts/` 提交成一个 commit。先看 `git status`；绝不提交 `.env` 或任何凭证。给我 commit 哈希。
