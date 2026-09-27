"""引擎可调参数。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class EngineConfig:
    # 价格超过该秒数即视为陈旧
    price_staleness_seconds: float = 60.0
    # 单笔价格相对上一笔有效价格允许的最大偏离比例（0.25 = 25%）
    max_price_deviation: Decimal = Decimal("0.25")
    # 清算人激励占被划转抵押品的比例（0.05 = 5%）
    liquidator_bonus_rate: Decimal = Decimal("0.05")
    # 罚金归入资金池的比例（占被划转抵押品）；bonus + penalty <= 1
    pool_penalty_rate: Decimal = Decimal("0.03")


DEFAULT_CONFIG = EngineConfig()
