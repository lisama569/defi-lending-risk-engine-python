"""核心数据模型：行情、仓位、健康度、清算结果。"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from app.errors import LiquidationRejectionReason, PriceRejectionReason


class PriceStatus(str, enum.Enum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class PriceTick:
    """一次行情推送。"""

    symbol: str
    price: float
    timestamp: float  # 行情产生时间（秒）


@dataclass(frozen=True)
class PriceUpdateResult:
    """价格推送的处理结果。"""

    status: PriceStatus
    reason: PriceRejectionReason | None = None
    accepted_tick: PriceTick | None = None


@dataclass
class Position:
    """用户借贷仓位（单一抵押物 + 单一债务资产）。"""

    user: str
    collateral_symbol: str
    collateral_amount: float
    debt_amount: float  # 以债务资产（USD 计价）计


@dataclass(frozen=True)
class HealthReport:
    """健康度评估结果。"""

    user: str
    health_factor: float | None  # None 表示无债务或无有效价格
    collateral_value: float
    debt_amount: float
    price_used: float | None
    price_timestamp: float | None
    liquidatable: bool


class LiquidationStatus(str, enum.Enum):
    EXECUTED = "EXECUTED"
    REJECTED = "REJECTED"
    DUPLICATE = "DUPLICATE"  # 幂等命中：返回首次执行的结果


@dataclass(frozen=True)
class Settlement:
    """一次清算的资金结算明细。"""

    repaid_debt: float            # 清算方代还债务
    collateral_seized: float      # 从借款方划走的抵押物数量
    liquidator_reward: float      # 清算方获得的抵押物（含激励）
    protocol_penalty: float       # 协议资金池获得的罚金（抵押物计）


@dataclass(frozen=True)
class LiquidationResult:
    """清算请求结果（幂等：同一 request_id 永远返回同一结果）。"""

    request_id: str
    status: LiquidationStatus
    reason: LiquidationRejectionReason | None = None
    health_before: HealthReport | None = None
    settlement: Settlement | None = None
