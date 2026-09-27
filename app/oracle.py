"""行情存储：价格推送的去重、乱序、过期与偏离保护。

判定顺序（先出现的原因优先生效，便于日志解释）：
1. INVALID_PRICE       —— 价格为空 / 零 / 负；
2. STALE_ON_ARRIVAL    —— 到达引擎时，报价自身年龄已超过陈旧阈值；
3. DUPLICATE           —— 时间戳与当前有效报价完全相同；
4. OUT_OF_ORDER        —— 时间戳早于当前有效报价；
5. DEVIATION_GUARD     —— 相对上一笔有效价格偏离比例超过阈值。
评估时若最新价格年龄过大，则标记为陈旧（STALE_ON_EVALUATION），
陈旧价格不会被健康度/清算使用。
"""

from __future__ import annotations

from decimal import Decimal

from app.clock import Clock
from app.config import EngineConfig
from app.models import PriceDecision, PriceTick, PriceView, RejectReason
from app.storage import LocalStore


class PriceOracle:
    def __init__(self, store: LocalStore, clock: Clock, config: EngineConfig) -> None:
        self._store = store
        self._clock = clock
        self._config = config
        # symbol -> 最新有效报价（其自身锁由存储统一持有）
        self._prices: dict[str, PriceTick] = {}

    def _reject(
        self,
        tick: PriceTick,
        current: PriceTick | None,
        reason: RejectReason,
        detail: str,
    ) -> PriceDecision:
        return PriceDecision(
            accepted=False, incoming=tick, current=current, reason=reason, detail=detail
        )

    def push(self, tick: PriceTick) -> PriceDecision:
        # 行情簿与账本共用同一把锁：价格更新与清算判定相互串行
        with self._store.lock:
            current = self._prices.get(tick.symbol)
            now = self._clock.now()

            # 1. 价格有效性
            if tick.price is None or tick.price <= 0:
                return self._reject(
                    tick, current, RejectReason.INVALID_PRICE,
                    f"价格必须为正数，收到 {tick.price}",
                )

            # 2. 到达时已陈旧（报价时间戳距当前过远，或明显是未来时间也视为无效）
            age = now - tick.timestamp
            if age > self._config.price_staleness_seconds:
                return self._reject(
                    tick, current, RejectReason.STALE_ON_ARRIVAL,
                    f"报价年龄 {age:.1f}s 超过阈值 "
                    f"{self._config.price_staleness_seconds:.0f}s",
                )
            if tick.timestamp > now:
                return self._reject(
                    tick, current, RejectReason.INVALID_PRICE,
                    f"报价时间戳 {tick.timestamp} 晚于当前 {now}，属于未来报价",
                )

            # 3 & 4. 与当前有效报价比较时间戳
            if current is not None:
                if tick.timestamp == current.timestamp:
                    return self._reject(
                        tick, current, RejectReason.DUPLICATE,
                        f"时间戳 {tick.timestamp} 与已生效价格重复",
                    )
                if tick.timestamp < current.timestamp:
                    return self._reject(
                        tick, current, RejectReason.OUT_OF_ORDER,
                        f"时间戳 {tick.timestamp} 早于已生效价格 {current.timestamp}",
                    )

                # 5. 偏离保护：|new - old| / old
                diff = abs(tick.price - current.price)
                deviation = diff / current.price
                if deviation > self._config.max_price_deviation:
                    return self._reject(
                        tick, current, RejectReason.DEVIATION_GUARD,
                        f"价格偏离 {deviation:.4%} 超过阈值 "
                        f"{self._config.max_price_deviation:.4%}："
                        f"{current.price} -> {tick.price}",
                    )

            # 全部校验通过，覆盖为新的有效价格
            self._prices[tick.symbol] = tick
            return PriceDecision(
                accepted=True,
                incoming=tick,
                current=tick,
                reason=None,
                detail=(
                    "首个有效价格已接受"
                    if current is None
                    else f"价格已更新 {current.price} -> {tick.price}"
                ),
            )

    def latest(self, symbol: str) -> PriceTick | None:
        with self._store.lock:
            return self._prices.get(symbol)

    def view_for(self, symbol: str) -> PriceView | None:
        """返回判定用价格视图；最新价格年龄过大时显式标记 stale。"""
        with self._store.lock:
            tick = self._prices.get(symbol)
            if tick is None:
                return None
            age = self._clock.now() - tick.timestamp
            stale = age > self._config.price_staleness_seconds
            return PriceView(
                symbol=tick.symbol,
                price=tick.price,
                timestamp=tick.timestamp,
                stale=stale,
            )
