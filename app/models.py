"""领域模型与枚举：价格、仓位、健康度报告、清算结算结果。

本文件只包含类型定义，不包含业务逻辑。所有金额均使用 Decimal，
时间戳使用 epoch 秒（float）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Optional


class RejectReason(str, Enum):
    """价格推送 / 评估被拒绝时的可区分原因。"""

    INVALID_PRICE = "INVALID_PRICE"              # 价格缺失、为负或为零
    STALE_ON_ARRIVAL = "STALE_ON_ARRIVAL"        # 推送时已明显过期
    OUT_OF_ORDER = "OUT_OF_ORDER"               # 时间戳早于已生效价格
    DUPLICATE = "DUPLICATE"                      # 时间戳与已生效价格完全相同
    DEVIATION_GUARD = "DEVIATION_GUARD"         # 偏离上一笔有效价格过多
    NO_PRICE = "NO_PRICE"                        # 该资产从未有过价格
    STALE_ON_EVALUATION = "STALE_ON_EVALUATION"  # 判定时最新价格已陈旧


class HealthRating(str, Enum):
    """仓位健康评级。"""

    HEALTHY = "HEALTHY"                # 健康：不允许清算
    LIQUIDATABLE = "LIQUIDATABLE"      # 低于阈值：允许清算
    UNKNOWN = "UNKNOWN"                # 无法判定（价格缺失/陈旧）


class LiquidationOutcome(str, Enum):
    """清算请求的处理结果。"""

    EXECUTED = "EXECUTED"                # 本次实际执行了清算
    SKIPPED_HEALTHY = "SKIPPED_HEALTHY"  # 仓位健康，拒绝清算
    DUPLICATE = "DUPLICATE"              # 幂等命中：该清算单已处理
    REJECTED = "REJECTED"               # 其他拒绝（价格陈旧等）


@dataclass(frozen=True)
class PriceTick:
    """一笔行情报价。"""

    symbol: str
    price: Decimal            # 以 USD 计价的单位价格
    timestamp: float          # 行情来源时间戳（epoch 秒）
    source: str = "oracle"


@dataclass(frozen=True)
class PriceDecision:
    """价格推送的判定结果，便于审计与日志解释。"""

    accepted: bool
    incoming: PriceTick
    current: Optional[PriceTick]
    reason: Optional[RejectReason]
    detail: str


@dataclass
class Position:
    """一个借贷仓位。

    health_factor = collateral_value_usd / debt_value_usd
    当 health_factor < liquidation_threshold（默认 1.0）时可被清算。
    """

    position_id: str
    owner: str
    collateral_symbol: str
    collateral_amount: Decimal
    debt_symbol: str
    debt_amount: Decimal
    liquidation_threshold: Decimal = Decimal("1.0")
    # 单笔清算最多可偿还的债务比例（部分清算上限），如 0.5
    max_close_factor: Decimal = Decimal("0.5")


@dataclass(frozen=True)
class PriceView:
    """判定时使用的价格快照（含是否陈旧标记）。"""

    symbol: str
    price: Decimal
    timestamp: float
    stale: bool


@dataclass(frozen=True)
class HealthReport:
    """健康度评估报告，记录判定依据。"""

    position_id: str
    healthy: Optional[bool]              # None 表示无法判定
    rating: HealthRating
    collateral_price: Decimal
    debt_price: Decimal
    collateral_value_usd: Decimal
    debt_value_usd: Decimal
    health_factor: Optional[Decimal]
    threshold: Decimal
    detail: str
    evaluated_at: float


@dataclass(frozen=True)
class BalanceSnapshot:
    """某一方在某一时点各币种的余额快照。"""

    party: str
    balances: dict[str, Decimal]


@dataclass(frozen=True)
class LiquidationResult:
    """清算请求的结果，包含幂等语义与三方结算账目。"""

    liquidation_id: str
    position_id: str
    outcome: LiquidationOutcome
    detail: str
    repay_debt_amount: Decimal = Decimal("0")
    collateral_seized: Decimal = Decimal("0")
    liquidator_bonus: Decimal = Decimal("0")
    pool_penalty: Decimal = Decimal("0")
    before: Optional[tuple[BalanceSnapshot, BalanceSnapshot, BalanceSnapshot]] = None
    after: Optional[tuple[BalanceSnapshot, BalanceSnapshot, BalanceSnapshot]] = None
