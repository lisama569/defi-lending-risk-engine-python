# 本地 DeFi 借贷风控与清算引擎（Python）

用本地内存存储模拟行情与用户仓位，提供：

- 行情价格推送：去重、乱序、过期、价格偏离保护，且失败原因**可区分**；
- 仓位健康度评估与**部分清算**：只有低于阈值的仓位才会被清算；
- 清算三方结算（清算方 / 借款方 / 资金池）：账目**守恒**、失败无中间状态；
- 以 `liquidation_id` 为幂等键：重复或并发提交**只扣款一次**。

所有金额使用 `Decimal`，关键"检查 + 落账"在同一把 `RLock` 内原子完成。

## 目录结构

| 文件 | 职责 |
| --- | --- |
| `app/models.py` | 枚举与数据模型（价格、仓位、健康报告、清算结果） |
| `app/config.py` | 可调参数（陈旧阈值、偏离阈值、激励/罚金比例） |
| `app/clock.py` | 可注入/可变时钟，便于测试时间相关规则 |
| `app/storage.py` | 线程安全的本地账本：账户、仓位、清算幂等表 |
| `app/oracle.py` | 行情簿：价格有效性、乱序/重复/过期/偏离判定 |
| `app/risk.py` | 健康因子计算与部分清算数量（激励/罚金）计算 |
| `app/settlement.py` | 三方原子结算，先校验后落账，失败整体不动账 |
| `app/engine.py` | 门面：串行化"判定 + 结算 + 记账"，实现幂等 |
| `app/api.py` | FastAPI HTTP 接口 |
| `tests/` | 单元、并发、HTTP、审计日志测试 |

## 一、价格有效性规则

推送价格时按以下顺序判定，**先命中的原因优先生效**（返回体 `reason` 可区分）：

| reason | 触发条件 |
| --- | --- |
| `INVALID_PRICE` | 价格为空、为零、为负；或时间戳晚于当前时间（未来报价） |
| `STALE_ON_ARRIVAL` | 报价时间戳距当前时间超过 `price_staleness_seconds`（默认 60s） |
| `DUPLICATE` | 时间戳与当前已生效价格完全相同 |
| `OUT_OF_ORDER` | 时间戳早于当前已生效价格（乱序） |
| `DEVIATION_GUARD` | `|新价-旧价| / 旧价` 超过 `max_price_deviation`（默认 25%） |
| `NO_PRICE` | 某资产从未有过有效价格（评估时） |
| `STALE_ON_EVALUATION` | 评估时最新有效价格年龄已超过陈旧阈值 |

规则要点：

- 被拒绝的价格**不会覆盖**当前有效价格；
- 陈旧价格**不参与任何后续判定**，健康度此时给出 `UNKNOWN` 而不是误判为可清算；
- 首个价格不做偏离比较，直接生效。

## 二、健康度与清算边界

```
collateral_value_usd = collateral_amount * collateral_price
debt_value_usd       = debt_amount       * debt_price
health_factor        = collateral_value_usd / debt_value_usd
```

| 条件 | rating | 是否可清算 |
| --- | --- | --- |
| 任一价格缺失或陈旧 | `UNKNOWN` | 否（拒绝，原因可解释） |
| `health_factor >= liquidation_threshold`（默认 1.0，**含等于**） | `HEALTHY` | 否（`SKIPPED_HEALTHY`） |
| `health_factor < liquidation_threshold` | `LIQUIDATABLE` | 是 |

部分清算约束：

- 单笔偿还量不得超过 `debt_amount * max_close_factor`（默认 50%），否则拒绝；
- 偿还量不得超过剩余债务；
- 抵押品划转数量（抵押品币种）：

```
base      = repay_debt_amount * debt_price / collateral_price
seized    = base * (1 + liquidator_bonus_rate)   # 借款方 -> 清算人（含激励）
penalty   = base * pool_penalty_rate             # 借款方 -> 资金池
```

默认激励 5%、资金池罚金 3%。激励与罚金分别记账，不漏扣、不重复。

### 三方账目守恒

- 债务币种：清算人减少 = 资金池增加 = 偿还量；
- 抵押品：借款方减少 = 清算人增加（seized）+ 资金池增加（penalty）；
- 结算前先做全部余额校验，任一不满足则**完全不落账**，无半完成状态。

## 三、幂等与并发语义

- 幂等键为 `liquidation_id`；
- "幂等检查 → 健康度判定 → 余额校验 → 三方结算 → 仓位扣减 → 写幂等表"
  全程在本地账本的同一把锁内完成，并发请求被串行化；
- 第一笔请求真正执行（`EXECUTED`），之后同一 `liquidation_id` 的重复/并发请求
  一律返回 `DUPLICATE`，**不重复扣款、不重复减债**，响应中携带首次结算的账目；
- 被健康度/上限拒绝的清算也会记录结论，重放返回 `DUPLICATE`（并注明首次结论）；
- 结算前置校验失败（如清算人余额不足）会抛错且不写入幂等表，调用方修正后可重试。

## 四、HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/accounts/deposit` | 初始化账户余额 |
| `POST` | `/prices` | 推送价格，返回 `accepted/reason/detail` |
| `GET` | `/prices/{symbol}` | 查询最新价格及 `stale` 标记 |
| `POST` | `/positions` | 开立仓位 |
| `GET` | `/positions/{id}/health` | 查询健康度与判定依据 |
| `POST` | `/liquidations` | 提交清算，返回结果及结算前后三方余额 |

金额以十进制数字/字符串传输，内部全程 `Decimal`。

## 五、本地验证方式

```bash
# 安装依赖（Python >= 3.14，使用 uv）
uv sync --group dev

# 全量单元/并发/HTTP 测试
.venv/bin/python -m pytest -q

# 查看可解释审计日志（输入、判定依据、结算前后余额、守恒结果）
.venv/bin/python -m pytest tests/test_audit_log.py -s

# 只看并发幂等测试（32 线程并发同一清算单，只执行一次）
.venv/bin/python -m pytest tests/test_concurrency.py -v

# 启动 HTTP 服务
uvicorn main:app --reload
# 交互式文档：http://127.0.0.1:8000/docs
```

### 手动联调示例

```bash
curl -s localhost:8000/accounts/deposit -H 'content-type: application/json' \
  -d '{"party":"LIQ1","symbol":"USDC","amount":"100000"}'
curl -s localhost:8000/prices -H 'content-type: application/json' \
  -d '{"symbol":"ETH","price":"1500","timestamp":1700000000}'
curl -s localhost:8000/liquidations -H 'content-type: application/json' \
  -d '{"liquidation_id":"L1","position_id":"p1","liquidator":"LIQ1","repay_debt_amount":"1000"}'
```

> 注意：示例时间戳需与本地时钟相差不超过 60 秒。测试中通过 `MutableClock`
> 固定时间，不受真实时间影响。
