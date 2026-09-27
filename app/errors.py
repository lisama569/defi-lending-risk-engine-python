"""错误码定义：所有失败原因都必须可区分、可解释。"""

from __future__ import annotations

import enum


class PriceRejectionReason(str, enum.Enum):
    """价格推送被拒绝的原因。"""

    DUPLICATE = "DUPLICATE"                      # 与当前有效价格完全相同（重复推送）
    OUT_OF_ORDER = "OUT_OF_ORDER"                # 时间戳不新于当前价格（乱序）
    STALE = "STALE"                              # 时间戳距当前时间超过最大有效期
    DEVIATION_TOO_LARGE = "DEVIATION_TOO_LARGE"  # 与上一有效价格偏离超过阈值
    INVALID_PRICE = "INVALID_PRICE"              # 价格非正数或非法


class LiquidationRejectionReason(str, enum.Enum):
    """清算请求被拒绝的原因。"""

    POSITION_NOT_FOUND = "POSITION_NOT_FOUND"  # 仓位不存在
    POSITION_HEALTHY = "POSITION_HEALTHY"      # 健康度不低于阈值，禁止清算
    STALE_PRICE = "STALE_PRICE"                # 无有效价格，禁止判定
    NOTHING_TO_LIQUIDATE = "NOTHING_TO_LIQUIDATE"  # 债务已为 0
    INSUFFICIENT_COLLATERAL = "INSUFFICIENT_COLLATERAL"  # 抵押物不足以覆盖本次清算
