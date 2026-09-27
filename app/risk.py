"""健康度判定与部分清算数量计算。

health_factor = collateral_value_usd / debt_value_usd
- health_factor >= liquidation_threshold：HEALTHY，禁止清算；
- health_factor <  liquidation_threshold：LIQUIDATABLE；
- 任一价格缺失或陈旧：UNKNOWN，不允许清算（宁可不动，不可误判）。
"""

from __future__ import annotations

from decimal import Decimal

from app.errors import EngineError
from app.models import HealthRating, HealthReport, Position, PriceView


def _stale_report(position: Position, detail: str, evaluated_at: float) -> HealthReport:
    return HealthReport(
        position_id=position.position_id,
        healthy=None,
        rating=HealthRating.UNKNOWN,
        collateral_price=Decimal("0"),
        debt_price=Decimal("0"),
        collateral_value_usd=Decimal("0"),
        debt_value_usd=Decimal("0"),
        health_factor=None,
        threshold=position.liquidation_threshold,
        detail=detail,
        evaluated_at=evaluated_at,
    )


def evaluate_health(
    position: Position,
    collateral_view: PriceView | None,
    debt_view: PriceView | None,
    evaluated_at: float,
) -> HealthReport:
    if collateral_view is None:
        return _stale_report(
            position,
            f"抵押品 {position.collateral_symbol} 从无有效价格，无法判定",
            evaluated_at,
        )
    if debt_view is None:
        return _stale_report(
            position,
            f"债务币种 {position.debt_symbol} 从无有效价格，无法判定",
            evaluated_at,
        )
    if collateral_view.stale or debt_view.stale:
        stale_symbols = [
            v.symbol for v in (collateral_view, debt_view) if v.stale
        ]
        return _stale_report(
            position,
            f"价格已陈旧，不得参与判定: {', '.join(stale_symbols)}",
            evaluated_at,
        )

    collateral_value = position.collateral_amount * collateral_view.price
    debt_value = position.debt_amount * debt_view.price

    if debt_value == 0:
        # 无债务视为绝对健康
        health_factor = None
        healthy = True
        rating = HealthRating.HEALTHY
        detail = "无债务，视为健康仓位"
    else:
        health_factor = collateral_value / debt_value
        healthy = health_factor >= position.liquidation_threshold
        rating = HealthRating.HEALTHY if healthy else HealthRating.LIQUIDATABLE
        detail = (
            f"健康因子 {health_factor:.6f} "
            f"{'>=' if healthy else '<'} 清算阈值 {position.liquidation_threshold}"
        )

    return HealthReport(
        position_id=position.position_id,
        healthy=healthy,
        rating=rating,
        collateral_price=collateral_view.price,
        debt_price=debt_view.price,
        collateral_value_usd=collateral_value,
        debt_value_usd=debt_value,
        health_factor=health_factor,
        threshold=position.liquidation_threshold,
        detail=detail,
        evaluated_at=evaluated_at,
    )


def close_amounts(
    position: Position,
    repay_debt: Decimal,
    collateral_price: Decimal,
    debt_price: Decimal,
    liquidator_bonus_rate: Decimal,
    pool_penalty_rate: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    """根据拟偿还的债务数量，计算抵押品划转数量及激励/罚金。

    清算人按债务的美元价值获得等值抵押品，外加激励；罚金部分同样以
    抵押品数量计价并入资金池。全部数量均为抵押品币种单位。

    返回 (collateral_seized, liquidator_bonus, pool_penalty)：
    - collateral_seized：从借款方划转给清算人的抵押品（含激励）；
    - liquidator_bonus：其中属于激励的部分（解释用）；
    - pool_penalty：从借款方额外划转给资金池的罚金。
    """
    if repay_debt <= 0:
        raise EngineError("拟偿还债务必须为正数")
    if collateral_price <= 0 or debt_price <= 0:
        raise EngineError("价格必须为正数")

    repay_value_usd = repay_debt * debt_price
    base_collateral = repay_value_usd / collateral_price
    liquidator_bonus = base_collateral * liquidator_bonus_rate
    pool_penalty = base_collateral * pool_penalty_rate
    collateral_seized = base_collateral + liquidator_bonus

    return collateral_seized, liquidator_bonus, pool_penalty
