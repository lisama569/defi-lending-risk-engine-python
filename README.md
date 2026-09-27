# 本地 DeFi 借贷风控与清算引擎

一个基于 FastAPI + 本地存储（内存，线程安全）的借贷风控与部分清算原型：
行情有效性校验、仓位健康度判定、幂等清算、三方账目守恒结算。

## 目录结构

```
app/
  errors.py       # 可区分的失败原因枚举
  models.py       # 行情 / 仓位 / 健康度 / 结算等数据模型
  price_feed.py   # 行情推送校验：重复/乱序/陈旧/偏离
  positions.py    # 仓位库 + 复式账本（先校验后提交，无中间状态）
  liquidation.py  # 健康度判定 + 幂等清算引擎
  service.py      # 进程内单例装配（策略参数集中在此）
main.py           # FastAPI HTTP 端点
tests/            # pytest 单测（含并发场景）
```

## 1. 价格有效性规则

每条推送 `PriceTick(symbol, price, timestamp)` 按以下顺序校验，命中即拒绝，
拒绝原因互不相同（见 `PriceRejectionReason`）：

| 顺序 | 原因 | 规则 |
|---|---|---|
| 1 | `INVALID_PRICE` | 价格必须为正数 |
| 2 | `STALE` | `now - timestamp > max_price_age_seconds`（默认 60s），推送即陈旧 |
| 3 | `DUPLICATE` | (price, timestamp) 与当前有效 tick 完全相同 |
| 4 | `OUT_OF_ORDER` | 新 tick 时间戳不新于当前有效价格（乱序不覆盖） |
| 5 | `DEVIATION_TOO_LARGE` | 与上一有效价格偏离比例 `abs(new-cur)/cur > max_deviation_ratio`（默认 25%，边界等于允许） |

要点：

- 只有通过全部校验的价格才会写入；较新的有效价格不会被重复、乱序或陈旧推送覆盖。
- 偏离保护与陈旧是**不同原因**，调用方可以区分“行情疑似异常”与“行情已过期”。
- 读取侧同样设防：`get_valid_price` 对超过有效期的历史价格返回 `None`，
  **陈旧价格不会参与健康度判定或清算**。

## 2. 健康度与清算边界

```
health_factor = 抵押物数量 × 有效抵押物价格 / 债务
```

- `health_factor >= liquidation_threshold`（默认 1.0）：仓位健康，
  清算请求以 `POSITION_HEALTHY` 拒绝，健康仓位不可能被误清算。
- `health_factor < 1.0`：允许（部分）清算。
- 无有效（新鲜）价格：拒绝，原因 `STALE_PRICE`，不做任何资金操作。
- 债务为 0：`NOTHING_TO_LIQUIDATE`。
- 仓位不存在：`POSITION_NOT_FOUND`。

部分清算经济（对每笔 `repaid_debt`）：

```
罚没抵押物数量 = repaid_debt × (1 + liquidation_bonus) / price      # bonus 默认 5%
清算方所得抵押物 = repaid_debt × (1 + bonus − protocol_fee) / price # fee 默认 2%
协议罚金抵押物   = repaid_debt × protocol_fee / price
```

- 代还金额同时被**存量债务**和**可用抵押物**封顶（不会多扣现金、不会罚没超过持仓）；
  抵押物不足以覆盖任何清算时返回 `INSUFFICIENT_COLLATERAL`。
- 部分清算使仓位恢复健康后，后续请求会以 `POSITION_HEALTHY` 停止——这是边界的一部分。

## 3. 结算与三方守恒

每笔清算在同一把引擎串行锁内完成，账本分录采用“影子账本预校验 → 一次性提交”，
仓位结算同样是 preview/commit 两段式：任何一项校验失败（如清算方现金不足），
账本与仓位都**不发生写入**，不存在半完成状态。

三方账目（清算方 / 借款方 / 资金池）恒满足：

- 现金：`清算方 −repay == 资金池 +repay`，三方现金总和不变；
- 抵押物：`清算方所得 + 协议罚金 + 借款方剩余 == 开仓抵押物总量`；
- 债务：债务减少额 == 清算方代还额 == 资金池现金增加额；
- 激励与罚金来自罚没抵押物的切分，不漏扣、不重复。

## 4. 幂等语义

- 每个清算请求必须带 `request_id`。
- 同一 `request_id` 的**重复或并发**提交：只有第一次到达临界区的请求实际结算，
  其余全部回放首次结果——首次成功则返回 `status=DUPLICATE` 且携带**同一**
  settlement（不重复扣款、不重复减债）；首次被拒则返回相同拒绝原因。
- 不同 `request_id` 并发：每笔最多结算一次，串行化后最终账目与调度顺序无关，守恒始终成立。

## 5. HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/prices` | 推送行情；无效返回 422 + 可区分 `reason` |
| POST | `/positions` | 开仓（抵押物镜像入账本） |
| GET  | `/positions/{user}/health` | 健康度与判定依据（价格、时间戳、HF） |
| POST | `/liquidations` | 提交清算（`request_id` 幂等） |
| POST | `/accounts/credit` | 本地注资（给清算方充入现金） |
| GET  | `/accounts/{account}` | 查询账本余额 |

## 6. 本地验证

```bash
# 运行全部单测
uv run pytest -q

# 带日志（打印输入、判定依据、结算前后余额）
uv run pytest -q --log-cli-level=INFO

# 启动服务
uv run python main.py
```

### curl 快速走查

```bash
T=$(date +%s.%N)
curl -s -X POST localhost:8000/prices -H 'content-type: application/json' \
  -d "{\"symbol\":\"ETH\",\"price\":1000,\"timestamp\":$T}"
curl -s -X POST localhost:8000/positions -H 'content-type: application/json' \
  -d '{"user":"alice","collateral_symbol":"ETH","collateral_amount":10,"debt_amount":8000}'
curl -s -X POST localhost:8000/accounts/credit -H 'content-type: application/json' \
  -d '{"account":"liquidator","asset":"USD","amount":10000}'
curl -s localhost:8000/positions/alice/health
curl -s -X POST localhost:8000/liquidations -H 'content-type: application/json' \
  -d '{"request_id":"r1","user":"alice","repay_debt":2000}'
```

## 7. 测试覆盖

- 行情：首次接受、重复、乱序、推送即陈旧、读取时陈旧、偏离保护（含边界）、
  非法价格、多资产独立。
- 清算：健康仓位误清算防护、部分清算的激励/罚金拆分与三方守恒、
  陈旧价格禁止判定、串行重复幂等、**16 线程同一 request_id 并发**只执行一次、
  不同 request_id 并发各自最多结算一次且守恒、超额代还封顶、现金不足失败无残留、
  仓位不存在。
- 单测日志使用 `caplog` 断言并打印：输入 tick、健康度判定依据（价格、时间戳、HF）、
  结算前后仓位与系统余额。
