# Prompt 03 — 通过 Qoder Desktop 下 Anthropic IPO 单（人工判断仓位）

> **已取消（用户 2026-11-20 决定）：不买 Anthropic，只按 P\*15 策略交易。不要粘贴本文件。**

用法：等 moomoo 上 Anthropic 已经开始交易（美东上午晚些时候到中午，新加坡时间半夜到凌晨）再用。
Qoder Desktop 新开对话，确认 moomoo connector 已开启，粘贴 EN 部分。它给出订单后，你核对无误再回 `CONFIRM`。
用户已定：单只上限 $40,000（比赛 40% 规则）；3% 移动止损（相对最高价，初始相对成交均价）；
止损后价格回到卖出价上方 1% 时买回，最多买回 2 次（用户 11-19 已确认；买回用 prompts/04-anthropic-rebuy.md）。

---

## EN（粘贴这段）

Use the moomoo connector / moomoo-skill to place a discretionary paper trade for me. This is a manual-judgment position in my Moomoo × Qoder challenge, so follow these rules exactly.

Account guard
1. List my paper accounts and select ONLY account `5832812` (the "Moomoo x Qoder AI Paper Trading Agent Challenge" account). If you cannot select it explicitly, stop and tell me. Never use any other account.

Instrument check
2. Find the US ticker for Anthropic (newly listed today). Show me the ticker, the latest trade price, bid/ask and whether it is trading in the regular session. If it is not trading yet, stop and tell me.

Buy order (do not submit until I confirm)
3. Prepare a LIMIT BUY: limit price = current ask × 1.005 rounded to 2 decimals; quantity = floor(40,000 / limit price) so that the notional is ≤ $40,000 (the competition caps any single stock at 40% of initial assets). Regular session only.
4. Show me the order ticket (account, symbol, side, quantity, limit price, notional, session, time-in-force) and wait. Submit only after I reply exactly `CONFIRM`.
5. After submitting, poll the order until it is filled, partially filled, or 2 minutes pass. Report fill price and quantity. If it is not filled after 2 minutes, tell me and ask whether to amend the price.

Protective stop
6. After a fill, try to place a SELL trailing-stop order for the filled quantity: trailing type = ratio, trailing ratio = 3%, regular session, in account 5832812. If the paper API does not support trailing-stop orders, do not invent a workaround — tell me clearly so I can place it in the moomoo app myself.

Logging
7. Append one JSON line per step (account check, quote, ticket, my confirmation, submission, fill, stop order or its failure) to `journal/decisions.jsonl` with fields: ts_utc, agent="qoder-desktop", mode="competition", account, type="manual_judgment", symbol, action, qty, price, order_id, status, rationale="user discretionary IPO view", notes. Create the file if it does not exist.

Do not place any other orders. Do not change any settings.

---

## 中文对照

用 moomoo connector / moomoo-skill 帮我下一笔自主判断的模拟单。这是我在 Moomoo × Qoder 比赛里的人工判断仓位，严格按以下规则执行。

账户保护
1. 列出我的模拟账户，只选账户 `5832812`（"Moomoo x Qoder AI Paper Trading Agent Challenge" 比赛账户）。如果不能明确选中它，就停下来告诉我。绝不使用其他账户。

标的检查
2. 找到 Anthropic 的美股代码（今天新上市）。给我看代码、最新成交价、买卖盘，以及是否在常规交易时段交易。如果还没开始交易，停下来告诉我。

买单（我确认前不要提交）
3. 准备一张限价买单：限价 = 当前卖一价 × 1.005，保留两位小数；数量 = 40,000 ÷ 限价，向下取整，确保金额 ≤ 4 万美元（比赛规定单只股票不超过初始资金 40%）。只在常规时段。
4. 把订单信息给我看（账户、代码、方向、数量、限价、金额、交易时段、有效期），然后等待。只有我回复 `CONFIRM` 才提交。
5. 提交后查询订单，直到成交、部分成交或者过了 2 分钟。报告成交价和数量。2 分钟还没成交，就告诉我，问我要不要改价。

保护性止损
6. 成交后，尝试为成交数量挂一张卖出移动止损单：按比例，比例 3%，常规时段，账户 5832812。如果模拟盘接口不支持移动止损单，不要自己想变通办法——明确告诉我，我去 moomoo app 里手动挂。

日志
7. 每一步（账户检查、报价、订单信息、我的确认、提交、成交、止损单或失败原因）都往 `journal/decisions.jsonl` 追加一行 JSON，字段：ts_utc、agent="qoder-desktop"、mode="competition"、account、type="manual_judgment"、symbol、action、qty、price、order_id、status、rationale="user discretionary IPO view"、notes。文件不存在就新建。

不要下任何其他订单，不要改任何设置。
