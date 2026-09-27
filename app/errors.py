"""领域异常类型。"""

from __future__ import annotations


class EngineError(Exception):
    """引擎基础异常。"""


class PriceRejected(EngineError):
    """价格推送被拒绝。"""


class PositionNotFound(EngineError):
    """仓位不存在。"""


class LiquidationRejected(EngineError):
    """清算请求被拒绝。"""
