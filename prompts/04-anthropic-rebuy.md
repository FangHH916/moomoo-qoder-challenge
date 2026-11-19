# Prompt 04 — Anthropic 止损后买回（最多 2 次）

用法：移动止损卖出后，在 moomoo 里给该股票设价格提醒 = 卖出成交价 × 1.01。
提醒响了 → Qoder Desktop 新开对话（或在原对话里）粘贴 EN 部分 → 核对订单 → 回复 `CONFIRM`。
规则（用户 11-19 确认）：价格回到上次止损卖出价上方 1% 才买回；最多买回 2 次；每次买回后重新挂 3% 移动止损；单只上限 $40,000。

---

## EN（粘贴这段）

Re-entry check for my discretionary Anthropic position, account `5832812` only (stop if you cannot select it explicitly).

1. Read `journal/decisions.jsonl`. Find the Anthropic entries with type="manual_judgment": the most recent stop-out SELL fill price (call it S) and how many re-entry BUYs have already been filled (count action="rebuy" with status filled).
2. If 2 re-entries have already been filled, do nothing except log action="rebuy_blocked" with reason "max 2 re-entries reached", and tell me.
3. If there is no stop-out sell yet, or I still hold the position, do nothing and tell me.
4. Get the latest trade price and ask. Only if the latest price ≥ S × 1.01 (regular session): prepare a LIMIT BUY with limit = ask × 1.005 (2 decimals) and quantity = floor(40,000 / limit), so notional ≤ $40,000. Otherwise tell me the gap and do nothing.
5. Show me the ticket and wait. Submit only after I reply exactly `CONFIRM`. Poll until filled / partially filled / 2 minutes.
6. After the fill, place a SELL trailing-stop for the filled quantity, ratio 3%, regular session. If the paper API does not support it, tell me so I can place it in the app.
7. Log every step to `journal/decisions.jsonl` (ts_utc, agent="qoder-desktop", mode="competition", account, type="manual_judgment", symbol, action ∈ {rebuy_check, rebuy, rebuy_blocked, trailing_stop}, qty, price, order_id, status, rationale="re-entry after stop-out: price ≥ 1.01 × last stop-out", notes).

Do not place any other orders.

---

## 中文对照

为我的 Anthropic 人工判断仓位做买回检查，只用账户 `5832812`（如果不能明确选中它就停下）。

1. 读 `journal/decisions.jsonl`，找出 type="manual_judgment" 的 Anthropic 记录：最近一次止损卖出的成交价（记为 S），以及已经成交了几次买回（数 action="rebuy" 且状态为已成交的条数）。
2. 如果已经买回过 2 次，就什么都不做，只记一条 action="rebuy_blocked"、原因"已达最多 2 次买回"，并告诉我。
3. 如果还没有止损卖出，或者我仍然持有这个仓位，什么都不做，告诉我。
4. 获取最新成交价和卖一价。只有当最新价 ≥ S × 1.01（常规时段）时：准备一张限价买单，限价 = 卖一价 × 1.005（两位小数），数量 = 40,000 ÷ 限价向下取整，金额 ≤ 4 万美元。否则告诉我还差多少，什么都不做。
5. 把订单信息给我看并等待。只有我回复 `CONFIRM` 才提交。查询订单直到成交、部分成交或 2 分钟。
6. 成交后，为成交数量挂卖出移动止损单，比例 3%，常规时段。如果模拟盘接口不支持，告诉我，我去 app 里挂。
7. 每一步都记入 `journal/decisions.jsonl`（ts_utc、agent="qoder-desktop"、mode="competition"、account、type="manual_judgment"、symbol、action ∈ {rebuy_check, rebuy, rebuy_blocked, trailing_stop}、qty、price、order_id、status、rationale="止损后价格 ≥ 上次止损价 × 1.01 时买回"、notes）。

不要下任何其他订单。
