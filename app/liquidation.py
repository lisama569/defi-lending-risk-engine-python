"""风控与清算引擎：健康度判定、幂等清算、三方守恒结算。

健康度：health_factor = 抵押物价值 / 债务；health_factor < 阈值才可清算。
清算经济（以债务资产 USD 计价，抵押物按 oracle 有效价格换算数量）：
  seized_value      = repaid_debt * (1 + liquidation_bonus)   # 清算方激励含在抵押物里
  liquidator_reward = seized_value 中 (1+bonus-fee) 部分
  protocol_penalty  = seized_value 中 fee 部分（归协议资金池罚金）
三方账目（清算方 / 借款方 / 资金池）在现金、抵押物、债务三个维度各自守恒。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from app.errors import LiquidationRejectionReason
from app.models import (
    HealthReport,
    LiquidationResult,
    LiquidationStatus,
    Position,
    Settlement,
)

logger = logging.getLogger("risk_engine.liquidation")

CASH_ASSET = "USD"
POOL_ACCOUNT = "pool"
POOL_DEBT_MEMO = "DEBT"  # 资金池应收债务的备忘科目


def _pool_account() -> str:
    return POOL_ACCOUNT


def _borrower_account(user: str) -> str:
    return f"borrower:{user}"


def _collateral_asset(symbol: str) -> str:
    return f"{symbol}_COLLATERAL"


class LiquidationEngine:
    def __init__(
        self,
        position_store,
        price_feed,
        ledger,
        *,
        liquidation_threshold: float = 1.0,
        liquidation_bonus: float = 0.05,
        protocol_fee_ratio: float = 0.02,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._positions = position_store
        self._prices = price_feed
        self._ledger = ledger
        self._threshold = float(liquidation_threshold)
        self._bonus = float(liquidation_bonus)
        self._fee = float(protocol_fee_ratio)
        self._clock = clock or time.time
        self._idempotency: dict[str, LiquidationResult] = {}
        self._lock = threading.RLock()

    # ---------------- 健康度 ----------------

    def evaluate_health(self, position: Position, *, now: float | None = None) -> HealthReport:
        if now is None:
            now = self._clock()
        valid = self._prices.get_valid_price(position.collateral_symbol, now=now)
        if valid is None:
            logger.warning(
                "健康度判定无有效价格 user=%s collateral=%s debt=%s -> 不可清算",
                position.user, position.collateral_symbol, position.debt_amount,
            )
            return HealthReport(
                user=position.user,
                health_factor=None,
                collateral_value=0.0,
                debt_amount=position.debt_amount,
                price_used=None,
                price_timestamp=None,
                liquidatable=False,
            )
        tick, age = valid
        collateral_value = position.collateral_amount * tick.price
        if position.debt_amount <= 1e-12:
            hf: float | None = None
            liquidatable = False
        else:
            hf = collateral_value / position.debt_amount
            liquidatable = hf < self._threshold
        logger.info(
            "健康度判定 user=%s collateral_amount=%s price=%s(ts=%s,age=%.3fs) "
            "collateral_value=%.6f debt=%.6f health_factor=%s threshold=%s -> %s",
            position.user, position.collateral_amount, tick.price, tick.timestamp, age,
            collateral_value, position.debt_amount,
            f"{hf:.6f}" if hf is not None else "inf", self._threshold,
            "LIQUIDATABLE" if liquidatable else "HEALTHY",
        )
        return HealthReport(
            user=position.user,
            health_factor=hf,
            collateral_value=collateral_value,
            debt_amount=position.debt_amount,
            price_used=tick.price,
            price_timestamp=tick.timestamp,
            liquidatable=liquidatable,
        )

    # ---------------- 系统状态（守恒校验/日志） ----------------

    def open_position(self, position: Position) -> None:
        """开仓/初始化：登记仓位，并把抵押物镜像贷记到借款方账本科目。

        此后“仓位内抵押物 + 账本中三方抵押物科目”之和恒等于初始抵押物数量。
        """
        with self._lock:
            self._positions.upsert(position)
            self._ledger.credit(
                _borrower_account(position.user),
                _collateral_asset(position.collateral_symbol),
                position.collateral_amount,
            )

    def system_state(self, user: str, liquidator: str) -> dict[str, float]:
        p = self._positions.get(user)
        cash = (
            self._ledger.balance_of(liquidator, CASH_ASSET)
            + self._ledger.balance_of(_borrower_account(user), CASH_ASSET)
            + self._ledger.balance_of(_pool_account(), CASH_ASSET)
        )
        if p is None:
            return {"cash_total": cash, "collateral_total": 0.0, "debt_remaining": 0.0}
        coll = _collateral_asset(p.collateral_symbol)
        collateral = (
            self._ledger.balance_of(liquidator, coll)
            + self._ledger.balance_of(_borrower_account(user), coll)
            + self._ledger.balance_of(_pool_account(), coll)
        )
        return {
            "cash_total": cash,
            "collateral_total": collateral,
            "debt_remaining": p.debt_amount,
        }

    # ---------------- 清算 ----------------

    def _reject(
        self,
        request_id: str,
        reason: LiquidationRejectionReason,
        detail: str,
        health: HealthReport | None = None,
    ) -> LiquidationResult:
        logger.warning(
            "清算被拒绝 request_id=%s reason=%s detail=%s", request_id, reason.value, detail
        )
        return LiquidationResult(
            request_id=request_id,
            status=LiquidationStatus.REJECTED,
            reason=reason,
            health_before=health,
        )

    def liquidate(
        self,
        request_id: str,
        user: str,
        repay_debt: float,
        liquidator: str = "liquidator",
        *,
        now: float | None = None,
    ) -> LiquidationResult:
        """提交一笔（部分）清算。同一 request_id 并发/重复提交结果幂等。"""
        if now is None:
            now = self._clock()
        with self._lock:
            # 幂等：已处理过的请求直接回放首次结果（标记为 DUPLICATE，不再扣款/减债）
            existing = self._idempotency.get(request_id)
            if existing is not None:
                logger.info("清算幂等命中 request_id=%s status=%s", request_id, existing.status.value)
                return LiquidationResult(
                    request_id=existing.request_id,
                    status=LiquidationStatus.DUPLICATE,
                    reason=existing.reason,
                    health_before=existing.health_before,
                    settlement=existing.settlement,
                )

            position = self._positions.get(user)
            if position is None:
                result = self._reject(request_id, LiquidationRejectionReason.POSITION_NOT_FOUND, f"user={user}")
                self._idempotency[request_id] = result
                return result

            health = self.evaluate_health(position, now=now)
            if health.price_used is None:
                result = self._reject(request_id, LiquidationRejectionReason.STALE_PRICE,
                                      "无新鲜有效价格，不参与判定", health)
                self._idempotency[request_id] = result
                return result
            if position.debt_amount <= 1e-12:
                result = self._reject(request_id, LiquidationRejectionReason.NOTHING_TO_LIQUIDATE,
                                      "债务已为 0", health)
                self._idempotency[request_id] = result
                return result
            if not health.liquidatable:
                result = self._reject(
                    request_id, LiquidationRejectionReason.POSITION_HEALTHY,
                    f"health_factor={health.health_factor:.6f} >= threshold={self._threshold}",
                    health,
                )
                self._idempotency[request_id] = result
                return result

            if repay_debt <= 0:
                result = self._reject(request_id, LiquidationRejectionReason.NOTHING_TO_LIQUIDATE,
                                      f"repay_debt={repay_debt}", health)
                self._idempotency[request_id] = result
                return result

            # 部分清算上限：同时受存量债务与可用抵押物约束
            collateral_value = position.collateral_amount * health.price_used
            repay_by_collateral = collateral_value / (1.0 + self._bonus)
            actual_repay = min(repay_debt, position.debt_amount, repay_by_collateral)
            if actual_repay <= 1e-9:
                result = self._reject(
                    request_id, LiquidationRejectionReason.INSUFFICIENT_COLLATERAL,
                    f"抵押物价值 {collateral_value:.6f} 无法覆盖任何清算", health,
                )
                self._idempotency[request_id] = result
                return result
            seized_value = actual_repay * (1.0 + self._bonus)
            collateral_seized = seized_value / health.price_used
            reward_value = actual_repay * (1.0 + self._bonus - self._fee)
            penalty_value = actual_repay * self._fee

            coll_asset = _collateral_asset(position.collateral_symbol)
            borrower = _borrower_account(user)
            entries = [
                (liquidator, CASH_ASSET, -actual_repay),            # 清算方支付现金
                (_pool_account(), CASH_ASSET, actual_repay),        # 资金池收回现金
                (_pool_account(), POOL_DEBT_MEMO, actual_repay),    # 备忘：应收债务减少（贷方+）
                (liquidator, coll_asset, reward_value / health.price_used),   # 清算方拿抵押物（含激励）
                (_pool_account(), coll_asset, penalty_value / health.price_used),  # 罚金归资金池
                (borrower, coll_asset, -collateral_seized),         # 借款方抵押物减少
            ]

            before_state = self.system_state(user, liquidator)
            try:
                # 两段式：先全量预校验（账本 + 仓位），任一不过都不写入
                self._ledger.preview(entries)
                before_pos, _after_pos = self._positions.preview_settlement(
                    user, actual_repay, collateral_seized
                )
            except Exception as exc:  # 余额不足等：保持原状，不产生半完成状态
                result = self._reject(
                    request_id, LiquidationRejectionReason.NOTHING_TO_LIQUIDATE,
                    f"结算预校验失败，操作未执行: {exc}", health,
                )
                self._idempotency[request_id] = result
                return result

            # 全部预校验通过后才提交（引擎串行锁下提交不会再失败）
            self._ledger.apply(entries)
            self._positions.commit_settlement(user, actual_repay, collateral_seized)

            settlement = Settlement(
                repaid_debt=actual_repay,
                collateral_seized=collateral_seized,
                liquidator_reward=reward_value / health.price_used,
                protocol_penalty=penalty_value / health.price_used,
            )
            result = LiquidationResult(
                request_id=request_id,
                status=LiquidationStatus.EXECUTED,
                health_before=health,
                settlement=settlement,
            )
            self._idempotency[request_id] = result

            after_state = self.system_state(user, liquidator)
            logger.info(
                "清算已执行 request_id=%s user=%s liquidator=%s repay=%.6f seized_coll=%.6f "
                "(reward=%.6f penalty=%.6f)\n  结算前仓位: %s\n  结算后仓位: %s\n"
                "  结算前系统: %s\n  结算后系统: %s",
                request_id, user, liquidator, actual_repay, collateral_seized,
                settlement.liquidator_reward, settlement.protocol_penalty,
                before_pos, self._positions.get(user), before_state, after_state,
            )
            return result
