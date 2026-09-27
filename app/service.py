"""进程内单例装配：行情源、仓位库、账本、清算引擎。"""

from __future__ import annotations

from app.liquidation import LiquidationEngine
from app.positions import Ledger, PositionStore
from app.price_feed import PriceFeed

# 全局策略参数（本地模拟）
MAX_PRICE_AGE_SECONDS = 60.0
MAX_DEVIATION_RATIO = 0.25
LIQUIDATION_THRESHOLD = 1.0
LIQUIDATION_BONUS = 0.05
PROTOCOL_FEE_RATIO = 0.02

price_feed = PriceFeed(MAX_PRICE_AGE_SECONDS, MAX_DEVIATION_RATIO)
position_store = PositionStore()
ledger = Ledger()
engine = LiquidationEngine(
    position_store,
    price_feed,
    ledger,
    liquidation_threshold=LIQUIDATION_THRESHOLD,
    liquidation_bonus=LIQUIDATION_BONUS,
    protocol_fee_ratio=PROTOCOL_FEE_RATIO,
)
