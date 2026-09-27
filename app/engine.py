"""风控引擎门面：组合存储、行情、健康度与结算。

并发安全：价格推送、健康度判定、清算都在 LocalStore 的同一把锁内
完成关键的"检查 + 落账"，因此并发清算请求被串行化。

幂等：以 liquidation_id 为幂等键。重复或并发提交同一笔清算，
只有第一笔真正扣款，后续请求直接返回首次结果，不重复扣款/减债。
"""

from __future__ import annotations

import logging
from decimal import Decimal

from app.clock import Clock
from app.config import DEFAULT_CONFIG, EngineConfig
from app.errors import PositionNotFound
from app.models import (
    HealthRating,
    HealthReport,
    LiquidationOutcome,
    LiquidationResult,
    Position,
    PriceDecision,
    PriceTick,
    RejectReason,
)
from app.oracle import PriceOracle
from app.risk import close_amounts, evaluate_health
from app.settlement import SettlementService
from app.storage import LocalStore

logger = logging.getLogger("risk_engine")


class RiskEngine:
    def __init__(
        self,
        store: LocalStore | None = None,
        clock: Clock | None = None,
        config: EngineConfig = DEFAULT_CONFIG,
    ) -> None:
        self._store = store or LocalStore()
        self._clock = clock or Clock()
        self._config = config
        self._oracle = PriceOracle(self._store, self._clock, config)
        self._settlement = SettlementService(self._store)
        self.pool_account = "POOL"
        self._store.ensure_account(self.pool_account)

    # ---- 账户初始化辅助 ----
    def deposit(self, party: str, symbol: str, amount: Decimal) -> None:
        self._store.credit(party, symbol, amount)

    # ---- 行情 ----
    def push_price(self, tick: PriceTick) -> PriceDecision:
        decision = self._oracle.push(tick)
        if decision.accepted:
            logger.info(
                "[价格接受] symbol=%s price=%s ts=%s detail=%s",
                tick.symbol, tick.price, tick.timestamp, decision.detail,
            )
        else:
            logger.warning(
                "[价格拒绝] symbol=%s price=%s ts=%s reason=%s detail=%s",
                tick.symbol, tick.price, tick.timestamp,
                decision.reason.value if decision.reason else None,
                decision.detail,
            )
        return decision

    def price_view(self, symbol: str):
        return self._oracle.view_for(symbol)

    # ---- 仓位 ----
    def open_position(self, position: Position) -> None:
        if position.collateral_amount < 0 or position.debt_amount < 0:
            raise ValueError("仓位数量不能为负")
        self._store.upsert_position(position)
        self._store.ensure_account(position.owner)
        logger.info(
            "[仓位开立] id=%s owner=%s collateral=%s %s debt=%s %s",
            position.position_id, position.owner,
            position.collateral_amount, position.collateral_symbol,
            position.debt_amount, position.debt_symbol,
        )

    def health(self, position_id: str) -> HealthReport:
        with self._store.lock:
            position = self._store.get_position(position_id)
            coll_view = self._oracle.view_for(position.collateral_symbol)
            debt_view = self._oracle.view_for(position.debt_symbol)
            report = evaluate_health(
                position, coll_view, debt_view, self._clock.now()
            )
        logger.info(
            "[健康度] id=%s rating=%s hf=%s 依据=%s",
            position_id, report.rating.value, report.health_factor, report.detail,
        )
        return report

    # ---- 清算 ----
    def liquidate(
        self,
        liquidation_id: str,
        position_id: str,
        liquidator: str,
        repay_debt_amount: Decimal,
    ) -> LiquidationResult:
        if repay_debt_amount <= 0:
            raise ValueError("repay_debt_amount 必须为正数")

        # 整个"幂等检查 + 健康度判定 + 结算 + 记账"在同一把锁内原子完成
        with self._store.lock:
            # 1) 幂等：已处理过的清算直接返回首次结果，不重复扣款/减债。
            #    重放返回 DUPLICATE 以便调用方区分"本次执行"与"命中幂等"；
            #    账目字段沿用首次结果，绝不再次落账。
            existing = self._store.get_liquidation(liquidation_id)
            if existing is not None:
                replay = LiquidationResult(
                    liquidation_id=existing.liquidation_id,
                    position_id=existing.position_id,
                    outcome=LiquidationOutcome.DUPLICATE,
                    detail=(
                        f"清算单 {liquidation_id} 已处理过（首次结果="
                        f"{existing.outcome.value}），本次为幂等重放，未重复扣款或减债"
                    ),
                    repay_debt_amount=existing.repay_debt_amount,
                    collateral_seized=existing.collateral_seized,
                    liquidator_bonus=existing.liquidator_bonus,
                    pool_penalty=existing.pool_penalty,
                    before=existing.before,
                    after=existing.after,
                )
                logger.info(
                    "[清算幂等命中] liquidation_id=%s position=%s first=%s",
                    liquidation_id, position_id, existing.outcome.value,
                )
                return replay

            position = self._store.get_position(position_id)

            # 2) 取价格并判定健康度（陈旧/缺失价格 -> UNKNOWN -> 拒绝）
            coll_view = self._oracle.view_for(position.collateral_symbol)
            debt_view = self._oracle.view_for(position.debt_symbol)
            report = evaluate_health(
                position, coll_view, debt_view, self._clock.now()
            )
            logger.info(
                "[清算输入] liquidation_id=%s position=%s liquidator=%s repay=%s %s | "
                "判定: rating=%s hf=%s threshold=%s 依据=%s",
                liquidation_id, position_id, liquidator,
                repay_debt_amount, position.debt_symbol,
                report.rating.value, report.health_factor, report.threshold,
                report.detail,
            )

            if report.rating is not HealthRating.LIQUIDATABLE:
                result = LiquidationResult(
                    liquidation_id=liquidation_id,
                    position_id=position_id,
                    outcome=(
                        LiquidationOutcome.SKIPPED_HEALTHY
                        if report.rating is HealthRating.HEALTHY
                        else LiquidationOutcome.REJECTED
                    ),
                    detail=f"仓位不可清算（{report.rating.value}）：{report.detail}",
                )
                # 记录幂等结果：同一清算单即使重放也保持"未清算"结论
                self._store.save_liquidation(result)
                logger.warning("[清算拒绝] %s", result.detail)
                return result

            # 3) 部分清算上限：偿还量不得超过 债务 * max_close_factor
            max_repay = position.debt_amount * position.max_close_factor
            if repay_debt_amount > max_repay:
                result = LiquidationResult(
                    liquidation_id=liquidation_id,
                    position_id=position_id,
                    outcome=LiquidationOutcome.REJECTED,
                    detail=(
                        f"单笔偿还 {repay_debt_amount} 超过部分清算上限 {max_repay}"
                        f"（债务 {position.debt_amount} * {position.max_close_factor}）"
                    ),
                )
                self._store.save_liquidation(result)
                logger.warning("[清算拒绝] %s", result.detail)
                return result

            # 偿还量也不得超过剩余债务
            repay = min(repay_debt_amount, position.debt_amount)

            # 4) 计算抵押品划转、激励与罚金
            seized, bonus, penalty = close_amounts(
                position,
                repay,
                report.collateral_price,
                report.debt_price,
                self._config.liquidator_bonus_rate,
                self._config.pool_penalty_rate,
            )

            # 5) 原子结算（内部做余额校验，失败抛错且不产生中间状态，
            #    此时尚未保存清算单，允许调用方修正后重试）
            before, after = self._settlement.settle_partial_liquidation(
                position,
                liquidator,
                self.pool_account,
                repay,
                seized,
                bonus,
                penalty,
            )

            # 6) 结算成功后才扣减仓位债务与抵押品
            position.debt_amount -= repay
            position.collateral_amount -= seized + penalty
            self._store.upsert_position(position)

            result = LiquidationResult(
                liquidation_id=liquidation_id,
                position_id=position_id,
                outcome=LiquidationOutcome.EXECUTED,
                detail="部分清算执行成功",
                repay_debt_amount=repay,
                collateral_seized=seized,
                liquidator_bonus=bonus,
                pool_penalty=penalty,
                before=before,
                after=after,
            )
            # 7) 落幂等表：任何并发/重复提交都只读到这一份已执行结果
            self._store.save_liquidation(result)

            logger.info(
                "[清算完成] liquidation_id=%s repay=%s seized=%s bonus=%s penalty=%s\n"
                "  结算前: 清算人=%s 借款方=%s 资金池=%s\n"
                "  结算后: 清算人=%s 借款方=%s 资金池=%s",
                liquidation_id, repay, seized, bonus, penalty,
                before[0].balances, before[1].balances, before[2].balances,
                after[0].balances, after[1].balances, after[2].balances,
            )
            return result
