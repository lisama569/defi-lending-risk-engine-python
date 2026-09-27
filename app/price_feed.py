"""本地行情存储：接收推送、校验有效性，只对外提供新鲜有效价格。

有效性规则（全部按顺序检查，命中即拒绝并给出可区分原因）：
1. INVALID_PRICE       价格必须为正数。
2. STALE               tick.timestamp 距当前时间超过 max_price_age_seconds。
3. DUPLICATE           (price, timestamp) 与该 symbol 当前有效 tick 完全相同。
4. OUT_OF_ORDER        tick.timestamp 不新于当前有效价格（乱序）。
5. DEVIATION_TOO_LARGE 与上一有效价格偏离比例超过 max_deviation_ratio。

只有通过全部检查的价格才会写入存储；陈旧价格永远不会被 get_valid_price 暴露。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from app.errors import PriceRejectionReason
from app.models import PriceStatus, PriceTick, PriceUpdateResult

logger = logging.getLogger("risk_engine.price_feed")


class PriceFeed:
    def __init__(
        self,
        max_price_age_seconds: float,
        max_deviation_ratio: float,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._max_age = float(max_price_age_seconds)
        self._max_deviation = float(max_deviation_ratio)
        self._clock = clock or time.time
        self._latest: dict[str, PriceTick] = {}
        self._lock = threading.Lock()

    def _reject(
        self,
        tick: PriceTick,
        reason: PriceRejectionReason,
        detail: str,
    ) -> PriceUpdateResult:
        logger.warning(
            "价格被拒绝 input={symbol=%s price=%s ts=%s} reason=%s detail=%s",
            tick.symbol,
            tick.price,
            tick.timestamp,
            reason.value,
            detail,
        )
        return PriceUpdateResult(status=PriceStatus.REJECTED, reason=reason)

    def push(self, tick: PriceTick) -> PriceUpdateResult:
        """处理一次价格推送；并发调用串行化，较新有效价格不会被覆盖。"""
        now = self._clock()

        if not isinstance(tick.price, (int, float)) or tick.price <= 0:
            return self._reject(tick, PriceRejectionReason.INVALID_PRICE, "价格必须为正数")

        age = now - tick.timestamp
        if age > self._max_age:
            return self._reject(
                tick, PriceRejectionReason.STALE, f"age={age:.3f}s > max_age={self._max_age:.3f}s"
            )

        with self._lock:
            current = self._latest.get(tick.symbol)
            if current is not None:
                if tick.price == current.price and tick.timestamp == current.timestamp:
                    return self._reject(
                        tick, PriceRejectionReason.DUPLICATE,
                        f"与当前有效价格完全相同 current_ts={current.timestamp}",
                    )
                if tick.timestamp <= current.timestamp:
                    return self._reject(
                        tick, PriceRejectionReason.OUT_OF_ORDER,
                        f"tick_ts={tick.timestamp} <= current_ts={current.timestamp}",
                    )
                deviation = abs(tick.price - current.price) / current.price
                if deviation > self._max_deviation:
                    return self._reject(
                        tick, PriceRejectionReason.DEVIATION_TOO_LARGE,
                        f"deviation={deviation:.6f} > max={self._max_deviation:.6f} "
                        f"current={current.price} new={tick.price}",
                    )
            self._latest[tick.symbol] = tick

        logger.info(
            "价格已接受 input={symbol=%s price=%s ts=%s} age=%.3fs",
            tick.symbol, tick.price, tick.timestamp, age,
        )
        return PriceUpdateResult(status=PriceStatus.ACCEPTED, accepted_tick=tick)

    def get_valid_price(self, symbol: str, *, now: float | None = None):
        """返回 (tick, age)；不存在或已陈旧时返回 None（陈旧价不参与任何判定）。"""
        if now is None:
            now = self._clock()
        with self._lock:
            tick = self._latest.get(symbol)
        if tick is None:
            return None
        age = now - tick.timestamp
        if age > self._max_age:
            logger.warning(
                "价格已陈旧，不参与判定 symbol=%s ts=%s age=%.3fs max_age=%.3fs",
                symbol, tick.timestamp, age, self._max_age,
            )
            return None
        return tick, age
